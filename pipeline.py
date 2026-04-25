"""Trailer pipeline — granular per-phase functions with human-in-the-loop gates.

State is owned by `outputs/<run_id>/state.json`. Each phase (storyboard / keyframes /
shots / stitch) can be invoked independently so the UI can show checkpoints and
let the user regenerate individual items before moving on.

State shape:
{
  "run_id": "20260422_153012_title",
  "created_at": "2026-04-22T15:30:12",
  "concept": "...",
  "params": {...},
  "status": "new" | "storyboard_ready" | "keyframes_partial" | "keyframes_ready"
            | "shots_partial" | "shots_ready" | "stitching" | "done" | "failed",
  "story": null | {...},                      # editable by user after phase 1
  "references": ["references/user_ref_01.jpg", ...],
  "keyframes": [
    {
      "idx": 0, "path": null | "keyframes/shot_01.png",
      "status": "pending"|"generating"|"ready"|"failed",
      "error": null | "...", "prompt_override": null | "...",
      "updated_at": null | "..."
    }, ...
  ],
  "shots":  [ same shape, path is shots/shot_NN.mp4 ],
  "final":  null | "trailer.mp4"
}
"""

import asyncio
import json
import math
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import anchors as anchors_mod
import assets as assets_mod
import audio as audio_mod
import costs
from errors import (
    ExternalServiceError,
    TrailerError,
    TrailerNotFound,
    TrailerNotReady,
    TrailerUserError,
)
import facelock as facelock_mod
import logger
import music as music_mod
import nano_banana
import review
import seedance
import storyboard
import taste as taste_mod
import textutils
import video

ROOT = Path(__file__).parent
OUTPUT_ROOT = ROOT / "outputs"
OUTPUT_ROOT.mkdir(exist_ok=True)

# Per-run asyncio locks to serialize state read-modify-write sequences.
# Without this, concurrent variant renders can clobber each other's state updates.
_run_locks: dict[str, asyncio.Lock] = {}
_MAX_LOCKS = 200


def _get_lock(run_id: str) -> asyncio.Lock:
    if run_id not in _run_locks:
        if len(_run_locks) >= _MAX_LOCKS:
            idle = [k for k, v in _run_locks.items() if not v.locked() and k != run_id]
            for k in idle[:len(idle) // 2]:
                del _run_locks[k]
        _run_locks[run_id] = asyncio.Lock()
    return _run_locks[run_id]


# ─── Run management ───────────────────────────────────────────────────────

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


def validate_run_id(run_id: str) -> str:
    """Reject anything that could escape OUTPUT_ROOT or is otherwise suspicious.
    Called at the API boundary and from all path-building helpers as defense-in-depth.
    Returns the run_id unchanged on success; raises ValueError on malformed input."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return run_id


def _slug(s: str, max_len: int = 40) -> str:
    # Canonical in textutils.slug; wrapper keeps the "trailer" fallback so
    # generated run_ids match the original shape.
    return textutils.slug(s, max_len=max_len) if (s or "").strip() else "trailer"


def _run_dir(run_id: str) -> Path:
    validate_run_id(run_id)
    d = (OUTPUT_ROOT / run_id).resolve()
    if not d.is_relative_to(OUTPUT_ROOT.resolve()):
        raise FileNotFoundError(f"run not found: {run_id}")
    if not d.exists():
        raise FileNotFoundError(f"run not found: {run_id}")
    return d


def _now() -> str:
    # Canonical in textutils.now_iso; kept as pipeline._now so external callers
    # (server.py, director.py) that already import it keep working.
    return textutils.now_iso()


def _save_state(run_id: str, state: dict):
    """Atomic write + shadow backup.

    Flow: serialize → write `.tmp` → rename current `state.json` to `.bak` →
    rename `.tmp` to `state.json`. Keeping the last-known-good as `.bak` gives
    get_state() a recovery path when the primary is corrupted.
    """
    validate_run_id(run_id)
    d = OUTPUT_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)
    primary = d / "state.json"
    tmp = d / "state.json.tmp"
    backup = d / "state.json.bak"
    # Serialize first. If this raises we haven't touched anything on disk yet.
    payload = json.dumps(state, indent=2)
    tmp.write_text(payload)
    # Shadow the current primary before replacing it. Best-effort: a missing
    # primary (first save) or a permission error shouldn't block the write.
    if primary.exists():
        try:
            primary.replace(backup)
        except Exception as e:
            print(f"[state] could not snapshot backup for {run_id}: {e}", file=sys.stderr)
    tmp.replace(primary)
    _list_runs_cache["data"] = None


def _migrate_shot(shot: dict, num_variants: int = 1) -> dict:
    """Normalize old-style shots (flat path/status) to new-style with `variants` list.
    Non-destructive: adds fields without removing existing ones (backward-compat reads).

    Defensive: repairs malformed variants (missing fields, non-dict entries) instead
    of assuming well-formed input. Safe to call repeatedly (idempotent)."""
    if "variants" not in shot:
        if shot.get("path"):
            shot["variants"] = [{
                "idx": 0,
                "path": shot["path"],
                "seed": None,
                "status": shot.get("status", "ready"),
                "error": shot.get("error"),
                "updated_at": shot.get("updated_at"),
            }]
        else:
            shot["variants"] = []
        shot["primary_variant"] = 0

    # Coerce variants to a list; drop non-dict entries.
    if not isinstance(shot["variants"], list):
        shot["variants"] = []
    shot["variants"] = [v for v in shot["variants"] if isinstance(v, dict)]

    # Repair each variant in-place — add any missing required field with a safe default.
    for i, v in enumerate(shot["variants"]):
        v.setdefault("idx", i)
        v.setdefault("path", None)
        v.setdefault("seed", None)
        v.setdefault("status", "ready" if v.get("path") else "pending")
        v.setdefault("error", None)
        v.setdefault("updated_at", None)

    # Ensure at least num_variants slots exist
    while len(shot["variants"]) < num_variants:
        shot["variants"].append({
            "idx": len(shot["variants"]),
            "path": None, "seed": None,
            "status": "pending", "error": None, "updated_at": None,
        })

    # Clamp primary_variant to a valid index so downstream code can always deref.
    pv = shot.get("primary_variant", 0)
    if not isinstance(pv, int) or pv < 0 or pv >= len(shot["variants"]):
        pv = 0
    shot["primary_variant"] = pv

    # Keep flat path/status in sync with primary variant (so older code still works)
    primary = shot["variants"][pv] if shot["variants"] else None
    if primary:
        shot["path"] = primary.get("path")
        shot["status"] = primary.get("status", shot.get("status", "pending"))
        shot["error"] = primary.get("error")
    return shot


def get_state(run_id: str) -> dict:
    d = _run_dir(run_id)
    p = d / "state.json"
    backup = d / "state.json.bak"
    if not p.exists():
        # Primary is missing. If we have a backup from the prior save, promote it.
        # This handles the crash-during-save window between `tmp.replace(primary)`
        # steps where the primary was moved to .bak but the .tmp rename didn't run.
        if backup.exists():
            print(f"[state] primary state.json missing for {run_id}; restoring from .bak", file=sys.stderr)
            try:
                backup.replace(p)
            except Exception as e:
                raise TrailerError(f"state.json missing and .bak restore failed for {run_id}: {e}")
        else:
            raise FileNotFoundError(f"state not found for run: {run_id}")
    try:
        state = json.loads(p.read_text())
    except json.JSONDecodeError as primary_err:
        # Primary is present but corrupt. Try the shadow backup before giving up.
        if backup.exists():
            try:
                state = json.loads(backup.read_text())
                print(
                    f"[state] primary state.json corrupt for {run_id} ({primary_err}); "
                    f"recovered from .bak — promoting backup to primary",
                    file=sys.stderr,
                )
                # Promote the backup so the next save produces a new backup rather
                # than silently overwriting the good copy.
                backup.replace(p)
            except Exception as backup_err:
                raise TrailerError(
                    f"corrupted state.json for run {run_id}: {primary_err}; "
                    f".bak also failed: {backup_err}"
                )
        else:
            raise TrailerError(f"corrupted state.json for run {run_id}: {primary_err}")
    # Lazy migration on read — keep all existing runs working without explicit upgrade.
    num_variants = state.get("params", {}).get("variants_per_scene", 1)
    for shot in state.get("shots") or []:
        _migrate_shot(shot, num_variants=num_variants)
    return state


def clone_run(run_id: str, *, new_title: Optional[str] = None) -> str:
    """Duplicate a run's concept, storyboard, references, assets — but not the
    rendered keyframes/shots/trailer. User gets a fresh run with the same creative
    starting point but can change ratio, re-render, etc."""
    src = _run_dir(run_id)
    state = get_state(run_id)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    title = new_title or (state.get("story", {}).get("title") or state.get("params", {}).get("title") or "clone")
    new_id = f"{ts}_{_slug(title)}_clone"
    dst = OUTPUT_ROOT / new_id
    if dst.exists():
        raise FileExistsError(f"target run exists: {new_id}")
    dst.mkdir(parents=True)

    # Copy references directory
    refs = src / "references"
    if refs.exists():
        shutil.copytree(refs, dst / "references")
    # Copy generated assets (so asset discovery doesn't re-run)
    a = src / "assets"
    if a.exists():
        shutil.copytree(a, dst / "assets")

    # Build a fresh state — preserve concept/story/params/refs/assets, clear renders
    new_state = {
        "run_id": new_id,
        "created_at": _now(),
        "cloned_from": run_id,
        "concept": state.get("concept", ""),
        "params": {**state.get("params", {}), "title": new_title or state.get("params", {}).get("title", "")},
        "status": "storyboard_ready" if state.get("story") else "new",
        "story": state.get("story"),
        "cast": list(state.get("cast") or []),
        "locations": list(state.get("locations") or []),
        "props": list(state.get("props") or []),
        "references": list(state.get("references") or []),
        "assets": [
            # Preserve choices user made (uploaded/generated/skipped)
            {**a, "status": a.get("status", "pending")} for a in (state.get("assets") or [])
        ],
        "asset_discovery_status": state.get("asset_discovery_status"),
        "asset_discovery_reasoning": state.get("asset_discovery_reasoning"),
        "keyframes": [
            {"idx": i, "path": None, "status": "pending", "error": None,
             "prompt_override": None, "updated_at": None,
             "composition_ref": (state.get("keyframes") or [])[i].get("composition_ref") if i < len(state.get("keyframes") or []) else None}
            for i in range(len((state.get("story") or {}).get("shots") or []))
        ],
        "shots": [
            {"idx": i, "path": None, "status": "pending", "error": None,
             "prompt_override": None, "updated_at": None,
             "video_ref": (state.get("shots") or [])[i].get("video_ref") if i < len(state.get("shots") or []) else None,
             "variants": [], "primary_variant": 0}
            for i in range(len((state.get("story") or {}).get("shots") or []))
        ],
        "cut_plan": None,
        "final": None,
        "rip_mode": state.get("rip_mode", False),
        "source_video": state.get("source_video"),  # reused
        "title_card": None,
        "music": None,
    }
    _save_state(new_id, new_state)
    if (src / "storyboard.json").exists():
        shutil.copy(src / "storyboard.json", dst / "storyboard.json")
    logger.info(new_id, "clone", f"cloned from {run_id}")
    return new_id


def archive_run(run_id: str, output_path: Optional[Path] = None) -> Path:
    """Zip the entire run directory to a single archive. Returns archive path."""
    import zipfile
    src = _run_dir(run_id)
    if output_path is None:
        output_path = src / f"{run_id}_archive.zip"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file() and p != output_path:
                z.write(p, arcname=str(p.relative_to(src)))
    logger.info(run_id, "archive", f"✓ zipped to {output_path.name} ({output_path.stat().st_size // 1024} KB)")
    return output_path


_list_runs_cache: dict = {"data": None, "ts": 0.0}
_LIST_RUNS_TTL = 5.0  # seconds


def list_runs(*, bust_cache: bool = False) -> list[dict]:
    """Summaries for all runs, newest first. Cached for 5s to avoid
    re-reading all state.json files on every poll."""
    import time
    now = time.monotonic()
    if not bust_cache and _list_runs_cache["data"] is not None and (now - _list_runs_cache["ts"]) < _LIST_RUNS_TTL:
        return _list_runs_cache["data"]

    runs = []
    if not OUTPUT_ROOT.exists():
        _list_runs_cache["data"] = runs
        _list_runs_cache["ts"] = now
        return runs
    for d in sorted(OUTPUT_ROOT.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir():
            continue
        sp = d / "state.json"
        if not sp.exists():
            continue
        try:
            s = json.loads(sp.read_text())
        except Exception as e:
            print(f"[list_runs] skipping {d.name}: corrupted state.json: {e}")
            continue
        runs.append(
            {
                "run_id": d.name,
                "title": (s.get("story") or {}).get("title") or s.get("params", {}).get("title") or "",
                "status": s.get("status", "unknown"),
                "created_at": s.get("created_at"),
                "num_shots": s.get("params", {}).get("num_shots"),
                "ratio": s.get("params", {}).get("ratio"),
                "final_ready": bool(s.get("final")),
            }
        )
    _list_runs_cache["data"] = runs
    _list_runs_cache["ts"] = now
    return runs


def create_run(
    *,
    concept: str,
    num_shots: int = 6,
    shot_duration: int = 5,
    ratio: str = "16:9",
    style_intent: str = "",
    title: str = "",
    crossfade: bool = False,
    reference_files: Optional[list[tuple[str, bytes]]] = None,
    cast_slugs: Optional[list[str]] = None,
    location_slugs: Optional[list[str]] = None,
    prop_slugs: Optional[list[str]] = None,
) -> str:
    """Create a fresh run directory + state.json. Returns run_id.

    `reference_files` is a list of (filename, bytes) for identity ref images.

    `cast_slugs` is a list of library `characters` slugs. For each slug we
    copy the library item's images into this run's references/ and record the
    character's {slug, name, description} in `state.cast` so the storyboard
    writer knows to use ONLY these characters — no invented recurring cast.

    `location_slugs` and `prop_slugs` work the same way for locations and props:
    library items are injected as references and recorded in state so Claude
    uses them consistently across shots.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{ts}_{_slug(title)}" if title else ts
    d = OUTPUT_ROOT / run_id
    d.mkdir(parents=True, exist_ok=False)

    refs_dir = d / "references"
    refs_dir.mkdir(exist_ok=True)
    stored_refs = []
    for i, (name, data) in enumerate(reference_files or []):
        ext = Path(name).suffix.lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            ext = ".png"
        dst = refs_dir / f"user_ref_{i+1:02d}{ext}"
        dst.write_bytes(data)
        stored_refs.append(f"references/{dst.name}")

    # Inject library characters. Each slug becomes a cast entry + the library
    # item's files land in references/ with the `characters_<slug>_` prefix
    # that the cast-coverage UI uses to reverse-engineer library provenance.
    import library as library_mod_local
    cast: list[dict] = []
    if cast_slugs:
        for slug in cast_slugs:
            slug = (slug or "").strip()
            if not slug:
                continue
            try:
                item = library_mod_local.get_item("characters", slug)
            except FileNotFoundError:
                logger.warn(run_id, "cast", f"library character {slug!r} not found — skipping")
                continue
            try:
                copied = library_mod_local.inject_into_run(
                    run_root=d, kind="characters", slug=slug, target_dir="references",
                )
                stored_refs.extend(copied)
            except Exception as e:
                logger.error(run_id, "cast", f"failed to inject {slug!r}: {e}")
                continue
            cast.append({
                "slug": slug,
                "name": item.get("name") or slug,
                "description": item.get("description") or "",
                "ref_paths": copied,
            })

    locations: list[dict] = []
    if location_slugs:
        for slug in location_slugs:
            slug = (slug or "").strip()
            if not slug:
                continue
            try:
                item = library_mod_local.get_item("locations", slug)
            except FileNotFoundError:
                logger.warn(run_id, "locations", f"library location {slug!r} not found — skipping")
                continue
            try:
                copied = library_mod_local.inject_into_run(
                    run_root=d, kind="locations", slug=slug, target_dir="references",
                )
                stored_refs.extend(copied)
            except Exception as e:
                logger.error(run_id, "locations", f"failed to inject {slug!r}: {e}")
                continue
            locations.append({
                "slug": slug,
                "name": item.get("name") or slug,
                "description": item.get("description") or "",
                "ref_paths": copied,
            })

    run_props: list[dict] = []
    if prop_slugs:
        for slug in prop_slugs:
            slug = (slug or "").strip()
            if not slug:
                continue
            try:
                item = library_mod_local.get_item("props", slug)
            except FileNotFoundError:
                logger.warn(run_id, "props", f"library prop {slug!r} not found — skipping")
                continue
            try:
                copied = library_mod_local.inject_into_run(
                    run_root=d, kind="props", slug=slug, target_dir="references",
                )
                stored_refs.extend(copied)
            except Exception as e:
                logger.error(run_id, "props", f"failed to inject {slug!r}: {e}")
                continue
            run_props.append({
                "slug": slug,
                "name": item.get("name") or slug,
                "description": item.get("description") or "",
                "ref_paths": copied,
            })

    state = {
        "run_id": run_id,
        "created_at": _now(),
        "concept": concept,
        "params": {
            "num_shots": num_shots,
            "shot_duration": shot_duration,
            "ratio": ratio,
            "style_intent": style_intent,
            "title": title,
            "crossfade": crossfade,
        },
        "status": "new",
        "story": None,
        "cast": cast,
        "locations": locations,
        "props": run_props,
        "references": stored_refs,
        "keyframes": [],
        "shots": [],
        "cut_plan": None,
        "final": None,
    }
    _save_state(run_id, state)
    return run_id


# ─── Phase 1: storyboard ──────────────────────────────────────────────────

async def run_storyboard(run_id: str, *, n_options: int = 1) -> dict:
    """Call Claude, store storyboard, initialise per-shot keyframe/shot slots.
    If n_options > 1, stores all options in state.storyboard_options and leaves
    state.story null until the user picks one via pick_storyboard_option()."""
    state = get_state(run_id)
    params = state["params"]
    if n_options > 1:
        logger.info(run_id, "storyboard", f"asking Claude for {n_options} distinct {params['num_shots']}-shot storyboard options…")
    else:
        logger.info(run_id, "storyboard", f"asking Claude for a {params['num_shots']}-shot {params['ratio']} storyboard…")

    try:
        story = await asyncio.to_thread(
            storyboard.generate_storyboard,
            concept=state["concept"],
            num_shots=params["num_shots"],
            shot_duration=params["shot_duration"],
            ratio=params["ratio"],
            style_intent=params.get("style_intent", ""),
            n_options=n_options,
            run_id=run_id,
            genre=params.get("genre", "neutral"),
            cast=state.get("cast") or None,
            locations=state.get("locations") or None,
            props=state.get("props") or None,
        )
    except Exception as e:
        logger.error(run_id, "storyboard", f"✗ Claude failed: {e}")
        raise

    # Multi-option path: store options, don't materialize shots yet
    if n_options > 1 and isinstance(story, dict) and "options" in story:
        opts = story["options"]
        async with _get_lock(run_id):
            state = get_state(run_id)
            state["storyboard_options"] = opts
            state["status"] = "storyboard_options_ready"
            _save_state(run_id, state)
        titles = ", ".join(f"'{o.get('title', '?')}'" for o in opts[:3])
        logger.success(run_id, "storyboard", f"✓ {len(opts)} options: {titles}")
        return story

    async with _get_lock(run_id):
        state = get_state(run_id)
        n = len(story["shots"])
        state["story"] = story
        state["keyframes"] = [
            {"idx": i, "path": None, "status": "pending", "error": None,
             "prompt_override": None, "updated_at": None}
            for i in range(n)
        ]
        state["shots"] = [
            {"idx": i, "path": None, "status": "pending", "error": None,
             "prompt_override": None, "updated_at": None}
            for i in range(n)
        ]
        state["status"] = "storyboard_ready"
        _save_state(run_id, state)
        (_run_dir(run_id) / "storyboard.json").write_text(json.dumps(story, indent=2))
    _validate_featured_names(run_id, story, state)
    logger.success(run_id, "storyboard", f"✓ {story.get('title', '(no title)')} — {len(story.get('shots', []))} shots")
    return story


def _best_match(name: str, candidates: set[str]) -> Optional[str]:
    """Return the best substring/prefix match from candidates, or None."""
    nl = name.strip().lower()
    for c in candidates:
        if nl == c:
            return c
    for c in candidates:
        if nl in c or c in nl:
            return c
    return None


def _validate_featured_names(run_id: str, story: dict, state: dict):
    """Auto-correct featured names that are near-misses against defined cast/locations/props.
    Exact matches pass through; substring matches are auto-corrected in-place; unresolvable
    names are warned about."""
    cast_names = {(c.get("name") or "").strip().lower() for c in (state.get("cast") or []) if c.get("name")}
    loc_names = {(l.get("name") or "").strip().lower() for l in (state.get("locations") or []) if l.get("name")}
    prop_names = {(p.get("name") or "").strip().lower() for p in (state.get("props") or []) if p.get("name")}
    corrected = 0
    for i, shot in enumerate(story.get("shots") or []):
        for key, defined in [("featured_characters", cast_names), ("featured_locations", loc_names), ("featured_props", prop_names)]:
            arr = shot.get(key) or []
            for j, name in enumerate(arr):
                if not defined:
                    continue
                nl = name.strip().lower()
                if nl in defined:
                    continue
                match = _best_match(name, defined)
                if match:
                    original_cased = next((c.get("name") for c in (state.get("cast" if key == "featured_characters" else ("locations" if key == "featured_locations" else "props")) or [])
                                           if (c.get("name") or "").strip().lower() == match), match)
                    arr[j] = original_cased
                    corrected += 1
                    logger.info(run_id, "storyboard", f"shot {i+1}: auto-corrected '{name}' → '{original_cased}' in {key}")
                else:
                    label = key.replace("featured_", "")
                    logger.warn(run_id, "storyboard", f"shot {i+1}: '{name}' in {key} not found in defined {label} — ref images won't match")
    if corrected:
        logger.info(run_id, "storyboard", f"auto-corrected {corrected} featured name(s)")


def _record_taste(signal_type: str, **fields):
    """Wrapper so taste signaling never breaks a pipeline call."""
    try:
        taste_mod.record(signal_type, **fields)
    except Exception as e:
        print(f"[taste] failed to record {signal_type}: {e}", file=__import__('sys').stderr)


async def pick_storyboard_option(run_id: str, option_idx: int) -> dict:
    """After run_storyboard(n_options=3) returns, user picks one. Materializes
    keyframe/shot slots and clears storyboard_options."""
    async with _get_lock(run_id):
        state = get_state(run_id)
        opts = state.get("storyboard_options") or []
        if option_idx < 0 or option_idx >= len(opts):
            raise IndexError(f"option idx {option_idx} out of range (have {len(opts)})")
        chosen = opts[option_idx]
        n = len(chosen["shots"])
        num_variants = max(1, state.get("params", {}).get("variants_per_scene", 1))

        state["story"] = chosen
        state["storyboard_options"] = None
        state["storyboard_chosen_idx"] = option_idx
        state["keyframes"] = [
            {"idx": i, "path": None, "status": "pending", "error": None,
             "prompt_override": None, "updated_at": None}
            for i in range(n)
        ]
        state["shots"] = [
            {"idx": i, "path": None, "status": "pending", "error": None,
             "prompt_override": None, "updated_at": None,
             "variants": [
                 {"idx": v, "path": None, "seed": None, "status": "pending", "error": None, "updated_at": None}
                 for v in range(num_variants)
             ],
             "primary_variant": 0}
            for i in range(n)
        ]
        state["status"] = "storyboard_ready"
        _save_state(run_id, state)
        (_run_dir(run_id) / "storyboard.json").write_text(json.dumps(chosen, indent=2))
    _validate_featured_names(run_id, chosen, state)
    logger.info(run_id, "storyboard", f"picked option {option_idx + 1}: '{chosen.get('title', '?')}'")
    _record_taste("storyboard_pick", run_id=run_id, option_idx=option_idx, title=chosen.get("title"))
    return chosen


async def update_storyboard(run_id: str, edited_story: dict) -> dict:
    """User-edited storyboard. Preserves shot count (N stays equal to original)
    and enforces the aspect ratio set at run creation (ratio is global)."""
    if not isinstance(edited_story, dict):
        raise ValueError("storyboard must be a JSON object")
    shots = edited_story.get("shots") or []
    if not isinstance(shots, list):
        raise ValueError("shots must be a list")
    async with _get_lock(run_id):
        state = get_state(run_id)
        original_n = len(state.get("story", {}).get("shots") or [])
        new_n = len(shots)
        if original_n and new_n != original_n:
            raise ValueError(
                f"edited storyboard must preserve shot count (was {original_n}, got {new_n})"
            )
        run_ratio = state.get("params", {}).get("ratio")
        for i, s in enumerate(shots):
            if not isinstance(s, dict):
                raise ValueError(f"shot {i} must be an object")
            # Strip per-shot ratio if it diverges from run's global ratio — stitching
            # assumes uniform ratio and a mismatch silently breaks the final concat.
            if run_ratio and s.get("ratio") and s["ratio"] != run_ratio:
                raise ValueError(f"shot {i} ratio {s['ratio']!r} does not match run ratio {run_ratio!r}")
        state["story"] = edited_story
        _save_state(run_id, state)
        (_run_dir(run_id) / "storyboard.json").write_text(json.dumps(edited_story, indent=2))
    _validate_featured_names(run_id, edited_story, state)
    logger.info(run_id, "storyboard", "storyboard edited by user")
    return edited_story


# ─── Phase 1.5: asset discovery ──────────────────────────────────────────

async def run_asset_discovery(run_id: str) -> dict:
    """Ask Claude what concrete assets the storyboard needs. Persists to state.assets
    as a list of items with status='pending'. Returns the full assets list (may be [])."""
    state = get_state(run_id)
    story = state.get("story")
    if not story:
        raise TrailerNotReady("storyboard must exist before asset discovery")

    async with _get_lock(run_id):
        state = get_state(run_id)
        state["asset_discovery_status"] = "generating"
        _save_state(run_id, state)
    logger.info(run_id, "assets", "scanning storyboard for logos / products / named locations…")

    ref_count = len(state.get("references") or [])
    cast_names = [c["name"] for c in (state.get("cast") or []) if c.get("name")]
    location_names = [l["name"] for l in (state.get("locations") or []) if l.get("name")]
    prop_names = [p["name"] for p in (state.get("props") or []) if p.get("name")]

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                assets_mod.discover_assets,
                story,
                existing_reference_count=ref_count,
                cast_names=cast_names,
                location_names=location_names,
                prop_names=prop_names,
                run_id=run_id,
            ),
            timeout=120,
        )
    except asyncio.TimeoutError:
        async with _get_lock(run_id):
            state = get_state(run_id)
            state["asset_discovery_status"] = "failed"
            state["asset_discovery_error"] = "timed out after 120s"
            _save_state(run_id, state)
        logger.error(run_id, "assets", "✗ asset scan timed out after 120s")
        raise ExternalServiceError("asset discovery timed out")
    except Exception as e:
        async with _get_lock(run_id):
            state = get_state(run_id)
            state["asset_discovery_status"] = "failed"
            state["asset_discovery_error"] = str(e)[:500]
            _save_state(run_id, state)
        logger.error(run_id, "assets", f"✗ asset scan failed: {e}")
        raise

    existing_assets = {
        (a.get("name", "").strip().lower(), a.get("type", "")): a
        for a in (state.get("assets") or [])
        if a.get("status") in ("uploaded", "generated")
    }

    items: list[dict] = []
    for i, a in enumerate(result.get("assets", [])):
        key = (a.get("name", "").strip().lower(), a.get("type", "prop"))
        prev = existing_assets.get(key)
        if prev and prev.get("path"):
            prev["shots"] = a.get("shots") or prev.get("shots", [])
            prev["description"] = a.get("description") or prev.get("description", "")
            prev["suggested_generation_prompt"] = a.get("suggested_generation_prompt") or prev.get("suggested_generation_prompt", "")
            items.append(prev)
            logger.info(run_id, "assets", f"preserved existing {prev['status']} asset: {prev['name']}")
        else:
            items.append({
                "id": f"asset_{i+1:02d}",
                "name": a.get("name", ""),
                "type": a.get("type", "prop"),
                "shots": a.get("shots") or [],
                "description": a.get("description", ""),
                "suggested_generation_prompt": a.get("suggested_generation_prompt", ""),
                "status": "pending",
                "path": None,
                "filename": None,
                "generation_prompt": None,
                "updated_at": None,
            })

    async with _get_lock(run_id):
        state = get_state(run_id)
        state["assets"] = items
        state["asset_discovery_status"] = "ready"
        state["asset_discovery_reasoning"] = result.get("reasoning", "")
        state.pop("asset_discovery_error", None)
        _save_state(run_id, state)
    if items:
        names = ", ".join(a["name"] for a in items[:5])
        logger.success(run_id, "assets", f"✓ found {len(items)} asset(s): {names}")
    else:
        logger.success(run_id, "assets", "✓ no external assets needed")
    return {"assets": items, "reasoning": result.get("reasoning", "")}


def _asset_slot(state: dict, asset_id: str) -> dict:
    for a in state.get("assets") or []:
        if a.get("id") == asset_id:
            return a
    raise KeyError(f"asset not found: {asset_id}")


async def upload_asset(run_id: str, asset_id: str, *, filename: str, data: bytes) -> dict:
    """Store a user-uploaded asset file."""
    run_dir = _run_dir(run_id)
    assets_dir = run_dir / "assets"
    assets_dir.mkdir(exist_ok=True)

    ext = Path(filename).suffix.lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        ext = ".png"
    dst = assets_dir / f"{asset_id}{ext}"

    async with _get_lock(run_id):
        dst.write_bytes(data)
        state = get_state(run_id)
        slot = _asset_slot(state, asset_id)
        slot["status"] = "uploaded"
        slot["path"] = f"assets/{dst.name}"
        slot["filename"] = filename
        slot["updated_at"] = _now()
        _save_state(run_id, state)
    logger.success(run_id, "assets", f"✓ uploaded '{filename}' for asset '{slot.get('name', asset_id)}'")
    return slot


async def generate_asset(run_id: str, asset_id: str, *, prompt_override: Optional[str] = None) -> dict:
    """Generate an asset via Nano Banana. Uses the suggested_generation_prompt unless
    the user provided an override. Resulting PNG is saved to outputs/<run>/assets/."""
    async with _get_lock(run_id):
        state = get_state(run_id)
        slot = _asset_slot(state, asset_id)
        prompt = prompt_override or slot.get("generation_prompt") or slot.get("suggested_generation_prompt") or slot.get("description") or slot.get("name", "")
        if not prompt:
            raise TrailerUserError(f"no generation prompt for asset {asset_id}")

        slot["status"] = "generating"
        slot["updated_at"] = _now()
        if prompt_override:
            slot["generation_prompt"] = prompt_override
        _save_state(run_id, state)
    logger.info(run_id, "assets", f"Nano Banana generating asset '{slot.get('name', asset_id)}'…")

    run_dir = _run_dir(run_id)
    assets_dir = run_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    out = assets_dir / f"{asset_id}.png"

    try:
        await nano_banana.generate_keyframe(
            prompt, reference_paths=None, output_path=out,
            rules_target="nano_banana_asset", run_id=run_id,
        )
    except Exception as e:
        async with _get_lock(run_id):
            state = get_state(run_id)
            slot = _asset_slot(state, asset_id)
            slot["status"] = "failed"
            slot["error"] = str(e)[:400]
            slot["updated_at"] = _now()
            _save_state(run_id, state)
        logger.error(run_id, "assets", f"✗ asset '{slot.get('name', asset_id)}' failed: {e}")
        raise

    async with _get_lock(run_id):
        state = get_state(run_id)
        slot = _asset_slot(state, asset_id)
        slot["status"] = "generated"
        slot["path"] = f"assets/{out.name}"
        slot["filename"] = None
        slot["generation_prompt"] = prompt
        slot.pop("error", None)
        slot["updated_at"] = _now()
        _save_state(run_id, state)
    logger.success(run_id, "assets", f"✓ asset '{slot.get('name', asset_id)}' generated")
    return slot


async def skip_asset(run_id: str, asset_id: str) -> dict:
    async with _get_lock(run_id):
        state = get_state(run_id)
        slot = _asset_slot(state, asset_id)
        slot["status"] = "skipped"
        slot["path"] = None
        slot["updated_at"] = _now()
        _save_state(run_id, state)
    return slot


async def generate_all_assets(run_id: str) -> list[dict]:
    """Generate all pending/discovered assets in parallel via asyncio.gather."""
    state = get_state(run_id)
    pending = [
        a["id"] for a in (state.get("assets") or [])
        if a.get("status") in ("pending", "failed")
    ]
    if not pending:
        return []
    logger.info(run_id, "assets", f"batch-generating {len(pending)} assets in parallel…")
    results = await asyncio.gather(
        *(generate_asset(run_id, aid) for aid in pending),
        return_exceptions=True,
    )
    completed = []
    for aid, result in zip(pending, results):
        if isinstance(result, Exception):
            logger.warn(run_id, "assets", f"batch: asset {aid} failed: {result}")
        else:
            completed.append(result)
    logger.info(run_id, "assets", f"batch complete: {len(completed)}/{len(pending)} succeeded")
    return completed


# ─── Phase 2: keyframes ───────────────────────────────────────────────────

def _build_keyframe_prompt(
    shot: dict, char_sheet: str, world_sheet: str, ratio: str,
    num_refs: int = 0,
    locations_sheet: str = "",
    props_sheet: str = "",
) -> str:
    aspect = {
        "16:9": "cinematic widescreen 16:9", "9:16": "vertical 9:16",
        "1:1": "square 1:1", "4:3": "classic 4:3", "3:4": "3:4 portrait",
        "21:9": "ultrawide anamorphic 21:9",
    }.get(ratio, ratio)

    ref_block = ""
    if num_refs > 0:
        ref_block = (
            f"[REFERENCES] I am attaching {num_refs} reference image"
            f"{'s' if num_refs != 1 else ''}, each labeled with its purpose. "
            "Use them literally: preserve the face / build / wardrobe shown in identity references, "
            "match the framing in composition references, carry over the lighting + palette from "
            "continuity references, and include any named assets exactly as shown. Do not reinvent "
            "the character from the [CHARACTERS] text — the attached identity image is authoritative."
        )

    blocks = [
        ref_block,
        f"[ASPECT] {aspect}",
        f"[WORLD] {world_sheet}" if world_sheet else "",
        f"[CHARACTERS] {char_sheet}" if char_sheet else "",
        f"[LOCATIONS] {locations_sheet}" if locations_sheet else "",
        f"[PROPS] {props_sheet}" if props_sheet else "",
        f"[BEAT] {shot['beat']}",
        f"[FRAME] {shot['keyframe_prompt']}",
        "Render as a single cinematic film still. Photoreal, shallow depth of field, film grain, considered color grade. No text, no subtitles, no watermarks.",
    ]
    return "\n".join(b for b in blocks if b)


def _build_locations_sheet(locations: Optional[list[dict]]) -> str:
    if not locations:
        return ""
    parts = []
    for loc in locations:
        name = (loc.get("name") or "").strip()
        desc = (loc.get("description") or "").strip()
        if name and desc:
            parts.append(f"{name}: {desc}")
        elif name:
            parts.append(name)
    return "; ".join(parts) if parts else ""


def _build_props_sheet(props: Optional[list[dict]]) -> str:
    if not props:
        return ""
    parts = []
    for p in props:
        name = (p.get("name") or "").strip()
        desc = (p.get("description") or "").strip()
        if name and desc:
            parts.append(f"{name}: {desc}")
        elif name:
            parts.append(name)
    return "; ".join(parts) if parts else ""


def _merge_props_sheets(lib_props: str, story_props: str) -> str:
    """Combine library and storyboard prop sheets, deduplicating by prop name.

    Library props take priority (user-curated descriptions). Story props are
    only included if their name doesn't already appear in the library set."""
    if not lib_props:
        return story_props
    if not story_props:
        return lib_props
    lib_names = set()
    for entry in lib_props.split(";"):
        name = entry.split(":")[0].strip().lower()
        if name:
            lib_names.add(name)
    extra = []
    for entry in story_props.split(";"):
        name = entry.split(":")[0].strip().lower()
        if name and name not in lib_names:
            extra.append(entry.strip())
    if not extra:
        return lib_props
    return "; ".join([lib_props] + extra)


def _build_motion_prompt(
    shot: dict, char_sheet: str, world_sheet: str,
    locations_sheet: str = "",
    props_sheet: str = "",
) -> str:
    blocks = [
        f"[MOTION] {shot['motion_prompt']}",
        f"[WORLD] {world_sheet}" if world_sheet else "",
        f"[CHARACTERS] {char_sheet}" if char_sheet else "",
        f"[LOCATIONS] {locations_sheet}" if locations_sheet else "",
        f"[PROPS] {props_sheet}" if props_sheet else "",
        "Cinematic motion. Maintain character identity and lighting from the reference image. Photoreal, film grain, motion blur on fast movement. No on-screen text, no captions.",
    ]
    return "\n".join(b for b in blocks if b)


async def claim_keyframe_slot(
    run_id: str,
    idx: int,
    *,
    prompt_override: Optional[str] = None,
) -> bool:
    """Atomically claim a keyframe slot for rendering. Returns False if another
    request has already marked it 'generating' — caller should report that back
    instead of firing a duplicate background task."""
    async with _get_lock(run_id):
        state = get_state(run_id)
        story = state.get("story")
        if not story:
            raise TrailerNotReady("storyboard not generated yet")
        if idx < 0 or idx >= len(story["shots"]):
            raise IndexError(f"shot index {idx} out of range")
        if idx >= len(state.get("keyframes") or []):
            raise IndexError(f"keyframe slot {idx} out of range")
        slot = state["keyframes"][idx]
        if slot.get("status") == "generating":
            return False
        slot["status"] = "generating"
        slot["error"] = None
        slot["updated_at"] = _now()
        if prompt_override is not None:
            slot["prompt_override"] = prompt_override or None
        state["status"] = "keyframes_partial"
        _save_state(run_id, state)
        return True


async def claim_shot_slot(
    run_id: str,
    idx: int,
    variant_idx: int = 0,
) -> bool:
    """Atomically claim a shot variant for rendering. Returns False if already claimed."""
    async with _get_lock(run_id):
        state = get_state(run_id)
        if idx < 0 or idx >= len(state.get("shots") or []):
            raise IndexError(f"shot index {idx} out of range")
        num_variants = max(1, state.get("params", {}).get("variants_per_scene", 1))
        _migrate_shot(state["shots"][idx], num_variants=num_variants)
        if variant_idx < 0 or variant_idx >= num_variants:
            raise IndexError(f"variant index {variant_idx} out of range (0..{num_variants-1})")
        shot_slot = state["shots"][idx]
        variant = shot_slot["variants"][variant_idx]
        if variant.get("status") == "generating":
            return False
        variant["status"] = "generating"
        variant["error"] = None
        variant["updated_at"] = _now()
        shot_slot["status"] = "generating"
        shot_slot["error"] = None
        shot_slot["updated_at"] = _now()
        _save_state(run_id, state)
        return True


def _pre_render_ref_check(
    run_id: str, shot_idx: int,
    featured_set: Optional[set[str]],
    resolved_refs: list[Path],
    name_to_refs: dict[str, list[str]],
    kind_label: str,
    run_dir: Path,
) -> None:
    """Warn before render if a featured item has no valid ref files on disk."""
    if featured_set is None or not featured_set:
        return
    for name in featured_set:
        ref_paths = name_to_refs.get(name, [])
        if not ref_paths:
            logger.warn(run_id, "keyframes", f"shot {shot_idx+1}: featured {kind_label} '{name}' has no ref files — identity may drift")
            continue
        missing = [p for p in ref_paths if not (run_dir / p).exists()]
        if len(missing) == len(ref_paths):
            logger.warn(run_id, "keyframes", f"shot {shot_idx+1}: all ref files for {kind_label} '{name}' missing from disk — identity will drift")


async def run_keyframe(
    run_id: str,
    idx: int,
    *,
    prompt_override: Optional[str] = None,
    chain_previous: bool = True,
) -> Path:
    """Generate (or regenerate) keyframe at index `idx`.

    `chain_previous`: pass shot N-1's keyframe as an additional ref. Keeps visual
    continuity but means regenerating shot K silently re-uses the OLD shot K-1
    frame (which is what you usually want — you only regen K because shot K-1 was
    already good).
    """
    async with _get_lock(run_id):
        state = get_state(run_id)
        story = state.get("story")
        if not story:
            raise TrailerNotReady("storyboard not generated yet")
        if idx < 0 or idx >= len(story["shots"]):
            raise IndexError(f"shot index {idx} out of range")
        if idx >= len(state.get("keyframes") or []):
            raise IndexError(f"keyframe slot {idx} out of range — state has {len(state.get('keyframes') or [])} slots")

        shot = story["shots"][idx]
        ratio = state["params"]["ratio"]
        run_dir = _run_dir(run_id)
        keyframes_dir = run_dir / "keyframes"
        keyframes_dir.mkdir(exist_ok=True)

        # Mark generating
        kf_slot = state["keyframes"][idx]
        kf_slot["status"] = "generating"
        kf_slot["error"] = None
        kf_slot["updated_at"] = _now()
        if prompt_override is not None:
            kf_slot["prompt_override"] = prompt_override or None
        state["status"] = "keyframes_partial"
        _save_state(run_id, state)
    logger.info(run_id, "keyframes", f"Nano Banana rendering keyframe {idx+1}/{len(story['shots'])} ({shot.get('beat', '')})…")

    try:
        all_ref_paths = state.get("references", [])

        # Classify references by provenance: cast entries, locations, and props
        # each store their injected paths in ref_paths. Anything not claimed is
        # a plain user upload (default: character identity for backward compat).
        char_name_to_refs: dict[str, list[str]] = {}
        char_ref_set = set()
        for c in state.get("cast") or []:
            cname = (c.get("name") or "").strip().lower()
            crefs = c.get("ref_paths") or []
            for p in crefs:
                char_ref_set.add(p)
            if cname:
                char_name_to_refs[cname] = crefs
        loc_name_to_refs: dict[str, list[str]] = {}
        for loc in state.get("locations") or []:
            name = (loc.get("name") or "").strip().lower()
            if name:
                loc_name_to_refs[name] = loc.get("ref_paths") or []
        prop_name_to_refs: dict[str, list[str]] = {}
        for pr in state.get("props") or []:
            name = (pr.get("name") or "").strip().lower()
            if name:
                prop_name_to_refs[name] = pr.get("ref_paths") or []
        all_loc_refs = {p for paths in loc_name_to_refs.values() for p in paths}
        all_prop_refs = {p for paths in prop_name_to_refs.values() for p in paths}

        # Determine which characters/locations/props are featured in THIS shot.
        # None → field absent (old storyboard), include all refs for backward compat.
        # []   → explicitly empty, include NO refs for that category.
        _raw_fc = shot.get("featured_characters")
        _raw_fl = shot.get("featured_locations")
        _raw_fp = shot.get("featured_props")
        featured_chars = {n.strip().lower() for n in _raw_fc} if _raw_fc is not None else None
        featured_locs = {n.strip().lower() for n in _raw_fl} if _raw_fl is not None else None
        featured_props_set = {n.strip().lower() for n in _raw_fp} if _raw_fp is not None else None

        shot_char_refs: list[Path] = []
        shot_loc_refs: list[Path] = []
        shot_prop_refs: list[Path] = []
        plain_refs: list[Path] = []
        for rp in all_ref_paths:
            abs_rp = run_dir / rp
            if rp in char_ref_set:
                for cname, cpaths in char_name_to_refs.items():
                    if rp in cpaths and (featured_chars is None or cname in featured_chars):
                        shot_char_refs.append(abs_rp)
                        break
            elif rp in all_loc_refs:
                for lname, lpaths in loc_name_to_refs.items():
                    if rp in lpaths and (featured_locs is None or lname in featured_locs):
                        shot_loc_refs.append(abs_rp)
                        break
            elif rp in all_prop_refs:
                for pname, ppaths in prop_name_to_refs.items():
                    if rp in ppaths and (featured_props_set is None or pname in featured_props_set):
                        shot_prop_refs.append(abs_rp)
                        break
            else:
                plain_refs.append(abs_rp)

        # Build references list + parallel labels. Order matters for Gemini: put the
        # most trustworthy-identity ref FIRST. User character refs > composition/prev.
        refs: list[Path] = []
        labels: list[str] = []

        # 1a. Plain user uploads — strongest identity signal (assumed characters)
        for r in plain_refs[:2]:
            refs.append(r)
            labels.append("character identity — preserve face, hair, build, and wardrobe EXACTLY")

        # 1b. Library character refs (only those featured in this shot when available)
        for r in shot_char_refs[:2]:
            refs.append(r)
            labels.append("character identity — preserve face, hair, build, and wardrobe EXACTLY")

        # 1c. Library location refs (only those featured in this shot)
        for r in shot_loc_refs[:1]:
            refs.append(r)
            labels.append("location reference — match the setting, architecture, and environment shown")

        # 1d. Library prop refs (only those featured in this shot)
        for r in shot_prop_refs[:1]:
            refs.append(r)
            labels.append("prop reference — include this object with exact appearance as shown")

        # 2. Shot-specific assets (named props/logos/locations)
        for asset in state.get("assets") or []:
            if asset.get("status") not in ("uploaded", "generated"):
                continue
            if idx not in (asset.get("shots") or []):
                continue
            ap = asset.get("path")
            if not ap:
                continue
            abs_ap = run_dir / ap
            if abs_ap.exists():
                refs.append(abs_ap)
                a_type = asset.get("type", "asset").upper()
                a_name = asset.get("name", "")
                a_desc = asset.get("description", "")
                label = f"{a_type}: '{a_name}'"
                if a_desc:
                    label += f" ({a_desc})"
                label += " — include in the scene with exact appearance"
                labels.append(label)

        # 3. Rip-o-matic: composition reference (source segment's first frame)
        comp_ref = kf_slot.get("composition_ref")
        if comp_ref:
            cp = run_dir / comp_ref
            if cp.exists():
                refs.append(cp)
                labels.append("composition — match this framing, subject scale, and camera angle (but replace the subject with the character above)")

        # 4. Continuity: previous keyframe (only if no composition ref — they'd conflict)
        if chain_previous and idx > 0 and not comp_ref:
            prev_kf = keyframes_dir / f"shot_{idx:02d}.png"
            if prev_kf.exists():
                refs.append(prev_kf)
                labels.append("continuity — match lighting, color palette, and character appearance from this prior shot")

        # 5. Prop/location continuity: if a featured prop or location also appeared
        #    in an earlier NON-adjacent shot, include that shot's keyframe so the
        #    model sees how the prop/location actually rendered in context.
        _used_continuity = {prev_kf} if (chain_previous and idx > 0 and not comp_ref and (keyframes_dir / f"shot_{idx:02d}.png").exists()) else set()
        all_shots = story.get("shots") or []
        for _cat_set, _cat_label in [
            (featured_props_set, "prop continuity — this earlier shot shows the same prop; preserve its appearance"),
            (featured_locs, "location continuity — this earlier shot shows the same location; preserve the environment"),
        ]:
            if not _cat_set:
                continue
            for prev_idx in range(idx - 1, -1, -1):
                if prev_idx == idx - 1 and not comp_ref and chain_previous:
                    continue
                prev_shot = all_shots[prev_idx] if prev_idx < len(all_shots) else None
                if not prev_shot:
                    continue
                _prev_key = "featured_props" if "prop" in _cat_label else "featured_locations"
                _prev_featured = prev_shot.get(_prev_key) or []
                _prev_set = {n.strip().lower() for n in _prev_featured}
                if _cat_set & _prev_set:
                    _cont_kf = keyframes_dir / f"shot_{prev_idx + 1:02d}.png"
                    if _cont_kf.exists() and _cont_kf not in _used_continuity:
                        refs.append(_cont_kf)
                        labels.append(_cat_label)
                        _used_continuity.add(_cont_kf)
                        break

        # Cap at 8 references (Nano Banana supports 8; room for 2 chars + 1 loc +
        # 1 prop + 1 comp/continuity + 2 prop/loc continuity + 1 asset)
        refs = refs[:8]
        labels = labels[:8]

        # Pre-render validation: warn about featured items with no valid ref files
        _pre_render_ref_check(run_id, idx, featured_chars, shot_char_refs, char_name_to_refs, "character", run_dir)
        _pre_render_ref_check(run_id, idx, featured_locs, shot_loc_refs, loc_name_to_refs, "location", run_dir)
        _pre_render_ref_check(run_id, idx, featured_props_set, shot_prop_refs, prop_name_to_refs, "prop", run_dir)

        lib_props = _build_props_sheet(state.get("props"))
        story_props = story.get("prop_sheet", "")
        combined_props = _merge_props_sheets(lib_props, story_props)
        base_prompt = _build_keyframe_prompt(
            shot, story.get("character_sheet", ""), story.get("world_sheet", ""), ratio,
            num_refs=len(refs),
            locations_sheet=_build_locations_sheet(state.get("locations")),
            props_sheet=combined_props,
        )
        full_prompt = prompt_override if prompt_override else (
            kf_slot.get("prompt_override") or base_prompt
        )

        # Build traceability record: which refs/assets were used for this keyframe
        refs_used: list[dict] = []
        for ri, rpath in enumerate(refs):
            entry: dict = {"path": str(rpath.relative_to(run_dir)) if rpath.is_relative_to(run_dir) else str(rpath)}
            if ri < len(labels):
                entry["label"] = labels[ri]
            for asset in state.get("assets") or []:
                ap = asset.get("path")
                if ap and (run_dir / ap).resolve() == rpath.resolve():
                    entry["asset_id"] = asset.get("id")
                    entry["asset_name"] = asset.get("name")
                    break
            for c in state.get("cast") or []:
                if entry.get("path") in (c.get("ref_paths") or []):
                    entry["cast_slug"] = c.get("slug")
                    entry["cast_name"] = c.get("name")
                    break
            for loc in state.get("locations") or []:
                if entry.get("path") in (loc.get("ref_paths") or []):
                    entry["location_slug"] = loc.get("slug")
                    entry["location_name"] = loc.get("name")
                    break
            for pr in state.get("props") or []:
                if entry.get("path") in (pr.get("ref_paths") or []):
                    entry["prop_slug"] = pr.get("slug")
                    entry["prop_name"] = pr.get("name")
                    break
            refs_used.append(entry)

        kf_path = keyframes_dir / f"shot_{idx+1:02d}.png"
        await nano_banana.generate_keyframe(
            full_prompt, refs, output_path=kf_path, reference_labels=labels,
            run_id=run_id,
        )
        try:
            costs.log_image(run_id, model=nano_banana.NANO_BANANA_MODEL, phase="keyframes")
        except Exception as e:
            logger.warn(run_id, "costs", f"cost logging failed (keyframes): {e}")

        async with _get_lock(run_id):
            state = get_state(run_id)
            if idx >= len(state.get("keyframes") or []):
                raise IndexError(f"keyframe {idx} disappeared after generation — result discarded")
            state["keyframes"][idx] = {
                "idx": idx,
                "path": f"keyframes/{kf_path.name}",
                "status": "ready",
                "error": None,
                "prompt_override": kf_slot.get("prompt_override"),
                "composition_ref": kf_slot.get("composition_ref"),
                "refs_used": refs_used,
                "updated_at": _now(),
            }
            # Cascade: variants rendered against the old keyframe are stale
            shot = state["shots"][idx] if idx < len(state["shots"]) else None
            if shot:
                any_ready = False
                for v in shot.get("variants") or []:
                    if v.get("status") == "ready":
                        v["stale"] = True
                        v["stale_reason"] = "keyframe regenerated — variant was rendered against the previous version"
                        any_ready = True
                if any_ready:
                    shot["stale"] = True
            # Invalidate contact sheets so cut plan uses fresh frames
            if state.get("contact_sheets"):
                for cs in state["contact_sheets"]:
                    if cs.get("idx") == idx:
                        cs["stale"] = True
            _roll_up_status(state)
            _save_state(run_id, state)
        logger.success(run_id, "keyframes", f"✓ keyframe {idx+1} ready")
        return kf_path
    except Exception as e:
        async with _get_lock(run_id):
            state = get_state(run_id)
            if idx < len(state.get("keyframes") or []):
                state["keyframes"][idx] = {
                    **state["keyframes"][idx],
                    "status": "failed",
                    "error": str(e)[:400],
                    "updated_at": _now(),
                }
                _roll_up_status(state)
                _save_state(run_id, state)
            else:
                logger.warn(run_id, "keyframes", f"keyframe {idx+1} disappeared before failure could be recorded")
        logger.error(run_id, "keyframes", f"✗ keyframe {idx+1} failed: {e}")
        raise


async def lock_face_on_keyframe(
    run_id: str,
    idx: int,
    *,
    face_reference: Optional[Path] = None,
    reference_idx: int = 0,
) -> Path:
    """Post-generation face lock on one keyframe. Uses Nano Banana multi-ref edit
    to replace the face with the one from a reference image while preserving
    composition / pose / lighting / everything else."""
    state = get_state(run_id)
    if idx < 0 or idx >= len(state.get("keyframes") or []):
        raise IndexError(f"keyframe index {idx} out of range")
    kf_slot = state["keyframes"][idx]
    if kf_slot.get("status") != "ready" or not kf_slot.get("path"):
        raise TrailerNotReady(f"keyframe {idx+1} not ready — generate it first")

    run_dir = _run_dir(run_id)
    source = run_dir / kf_slot["path"]

    ref_path: Optional[Path] = face_reference
    if ref_path is None:
        refs = state.get("references") or []
        if reference_idx < len(refs):
            cand = run_dir / refs[reference_idx]
            if cand.exists(): ref_path = cand
    if ref_path is None:
        for a in state.get("assets") or []:
            if a.get("type") == "character" and a.get("status") in ("uploaded", "generated") and a.get("path"):
                cand = run_dir / a["path"]
                if cand.exists():
                    ref_path = cand; break
    if ref_path is None:
        raise TrailerNotReady(
            "no face reference available — upload a character ref or let asset discovery generate one"
        )

    # Backup
    from datetime import datetime as _dt
    backup = source.with_name(f"{source.stem}_prefacelock_{_dt.now().strftime('%H%M%S')}.png")
    try: backup.write_bytes(source.read_bytes())
    except Exception as e:
        print(f"[facelock] backup creation failed: {e}", file=sys.stderr)
        backup = None

    async with _get_lock(run_id):
        state = get_state(run_id)
        if idx >= len(state.get("keyframes") or []):
            raise IndexError(f"keyframe {idx} disappeared during face-lock setup")
        state["keyframes"][idx]["status"] = "generating"
        state["keyframes"][idx]["error"] = None
        state["keyframes"][idx]["updated_at"] = _now()
        _save_state(run_id, state)
    logger.info(run_id, "keyframes", f"face-locking keyframe {idx+1} against {ref_path.name}…")

    try:
        await facelock_mod.lock_identity(source, ref_path, source, run_id=run_id)
    except Exception as e:
        if backup and backup.exists():
            try: source.write_bytes(backup.read_bytes())
            except Exception as restore_err: logger.error(run_id, "keyframes", f"[facelock] backup restore also failed: {restore_err}")
            try: backup.unlink(missing_ok=True)
            except Exception: pass
        async with _get_lock(run_id):
            state = get_state(run_id)
            if idx < len(state.get("keyframes") or []):
                state["keyframes"][idx]["status"] = "ready"
                state["keyframes"][idx]["error"] = f"face lock failed: {e}"[:400]
                _save_state(run_id, state)
            else:
                logger.warn(run_id, "keyframes", f"keyframe {idx+1} disappeared before face-lock failure could be recorded")
        logger.error(run_id, "keyframes", f"✗ face lock failed on kf {idx+1}: {e}")
        raise

    try: costs.log_image(run_id, model=nano_banana.NANO_BANANA_MODEL, phase="facelock")
    except Exception as e: print(f"[costs] facelock cost log failed: {e}", file=sys.stderr)

    # Cascade stale to any rendered shot variants
    async with _get_lock(run_id):
        state = get_state(run_id)
        if idx >= len(state.get("keyframes") or []):
            raise IndexError(f"keyframe {idx} disappeared after face-lock — result discarded")
        state["keyframes"][idx].update({
            "status": "ready", "error": None,
            "face_locked": True,
            "face_lock_ref": str(ref_path.relative_to(run_dir)),
            "updated_at": _now(),
        })
        shot = state["shots"][idx] if idx < len(state["shots"]) else None
        if shot:
            for v in shot.get("variants") or []:
                if v.get("status") == "ready":
                    v["stale"] = True
                    v["stale_reason"] = "keyframe face-locked — variant rendered against previous face"
            if any(v.get("stale") for v in shot.get("variants") or []):
                shot["stale"] = True
        _save_state(run_id, state)
    if backup and backup.exists():
        try: backup.unlink(missing_ok=True)
        except Exception: pass
    logger.success(run_id, "keyframes", f"✓ keyframe {idx+1} face-locked")
    return source


async def build_animatic(run_id: str) -> Path:
    """Build a fast Ken-Burns preview from keyframes + music + VO. No Seedance
    calls — this is the pre-render animatic for iteration speed.

    Requires all keyframes ready. Music and VO are optional (used if attached)."""
    state = get_state(run_id)
    story = state.get("story") or {}
    kfs = state.get("keyframes") or []
    if not kfs or not all(k.get("status") == "ready" and k.get("path") for k in kfs):
        raise TrailerNotReady("all keyframes must be ready before building an animatic")

    run_dir = _run_dir(run_id)
    keyframe_paths = [run_dir / k["path"] for k in kfs]
    # Derive per-shot durations from storyboard
    shots = story.get("shots") or []
    durations = [float(shots[i].get("duration_s", 5)) if i < len(shots) else 5.0 for i in range(len(keyframe_paths))]
    ratio = state.get("params", {}).get("ratio", "16:9")

    # Optional audio
    music_meta = state.get("music") or {}
    vo_meta = (state.get("audio") or {}).get("vo") or {}
    music_path = None
    if music_meta.get("path"):
        mp = run_dir / music_meta["path"]
        if mp.exists(): music_path = mp

    vo_lines = []
    if vo_meta.get("status") == "ready":
        script_lines = (vo_meta.get("script") or {}).get("lines") or []
        audio_paths = vo_meta.get("lines_audio") or []
        for i, rel in enumerate(audio_paths):
            if not rel: continue
            line_path = run_dir / rel
            if not line_path.exists(): continue
            start = (script_lines[i].get("suggested_start_s") if i < len(script_lines) else 0) or 0
            vo_lines.append({"path": line_path, "start_s": start})

    out = run_dir / "animatic.mp4"
    logger.info(run_id, "animatic", f"building Ken-Burns animatic from {len(keyframe_paths)} keyframes" + (" + music" if music_path else "") + (f" + {len(vo_lines)} VO" if vo_lines else ""))
    try:
        await video.build_animatic(
            keyframe_paths, durations, out,
            ratio=ratio, music_path=music_path, vo_lines=vo_lines,
        )
    except Exception as e:
        logger.error(run_id, "animatic", f"✗ {e}")
        raise

    async with _get_lock(run_id):
        state = get_state(run_id)
        state["animatic"] = "animatic.mp4"
        state["animatic_generated_at"] = _now()
        _save_state(run_id, state)
    logger.success(run_id, "animatic", f"✓ animatic.mp4 ready ({out.stat().st_size // 1024} KB) — preview before committing to Seedance")
    return out


async def lock_face_on_all_keyframes(run_id: str, *, reference_idx: int = 0) -> dict:
    """Batch-lock every ready keyframe, sequentially."""
    state = get_state(run_id)
    kfs = state.get("keyframes") or []
    ready = [i for i, k in enumerate(kfs) if k.get("status") == "ready" and k.get("path")]
    if not ready:
        raise TrailerNotReady("no ready keyframes to lock")
    logger.info(run_id, "keyframes", f"face-locking {len(ready)} keyframe(s)…")
    locked = failed = 0
    for i in ready:
        try:
            await lock_face_on_keyframe(run_id, i, reference_idx=reference_idx)
            locked += 1
        except Exception as e:
            failed += 1
            logger.warn(run_id, "keyframes", f"keyframe {i+1} lock failed: {e}")
    logger.success(run_id, "keyframes", f"✓ batch done — {locked} locked, {failed} failed")
    return {"locked": locked, "failed": failed, "total": len(ready)}


async def edit_keyframe(run_id: str, idx: int, *, edit_prompt: str) -> Path:
    """Nano Banana EDIT mode — takes the current keyframe, applies a surgical change,
    saves the result back over the keyframe path.

    Unlike regeneration, this keeps character identity + composition stable and only
    changes what you ask. Good for "make his hair longer", "remove the car",
    "change the time of day to dusk", etc.

    The previous keyframe is backed up as shot_NN_prev_TIMESTAMP.png in case you
    want to revert.
    """
    state = get_state(run_id)
    story = state.get("story")
    if not story:
        raise TrailerNotReady("storyboard not generated yet")
    if idx < 0 or idx >= len(story["shots"]):
        raise IndexError(f"shot index {idx} out of range")
    if idx >= len(state.get("keyframes") or []):
        raise IndexError(f"keyframe slot {idx} out of range")

    kf_slot = state["keyframes"][idx]
    if kf_slot["status"] != "ready" or not kf_slot.get("path"):
        raise TrailerNotReady(f"keyframe {idx+1} must be ready before you can edit it")

    run_dir = _run_dir(run_id)
    kf_path = run_dir / kf_slot["path"]
    if not kf_path.exists():
        raise TrailerError(f"keyframe file missing: {kf_path}")

    # Back up the previous version (so user can revert if the edit goes sideways)
    backup = kf_path.with_name(f"{kf_path.stem}_prev_{datetime.now().strftime('%H%M%S')}.png")
    try:
        backup.write_bytes(kf_path.read_bytes())
    except Exception as e:
        logger.warn(run_id, "keyframes", f"backup failed for keyframe {idx+1} before edit: {e}")
        backup = None

    async with _get_lock(run_id):
        state = get_state(run_id)
        if idx >= len(state.get("keyframes") or []):
            raise IndexError(f"keyframe {idx} disappeared during edit setup")
        state["keyframes"][idx]["status"] = "generating"
        state["keyframes"][idx]["error"] = None
        state["keyframes"][idx]["updated_at"] = _now()
        _save_state(run_id, state)
    logger.info(run_id, "keyframes", f"Nano Banana EDIT on keyframe {idx+1}: '{edit_prompt[:80]}'…")

    try:
        await nano_banana.edit_image(kf_path, edit_prompt, output_path=kf_path, run_id=run_id)
    except Exception as e:
        # Restore backup on failure
        restore_failed = False
        if backup and backup.exists():
            try: kf_path.write_bytes(backup.read_bytes())
            except Exception as restore_err:
                logger.error(run_id, "keyframes", f"backup restore failed for keyframe {idx+1}: {restore_err}")
                restore_failed = True
            try: backup.unlink(missing_ok=True)
            except Exception: pass
        error_msg = str(e)[:300]
        if restore_failed:
            error_msg += " [backup restore also failed — keyframe may be corrupted]"
        async with _get_lock(run_id):
            state = get_state(run_id)
            if idx < len(state.get("keyframes") or []):
                state["keyframes"][idx]["status"] = "ready"
                state["keyframes"][idx]["error"] = error_msg[:400]
                state["keyframes"][idx]["updated_at"] = _now()
                _save_state(run_id, state)
            else:
                logger.warn(run_id, "keyframes", f"keyframe {idx+1} disappeared before edit failure could be recorded")
        logger.error(run_id, "keyframes", f"✗ edit failed on keyframe {idx+1}: {e}")
        raise

    async with _get_lock(run_id):
        state = get_state(run_id)
        if idx >= len(state.get("keyframes") or []):
            raise IndexError(f"keyframe {idx} disappeared after edit — result discarded")
        state["keyframes"][idx] = {
            **state["keyframes"][idx],
            "status": "ready",
            "error": None,
            "last_edit": edit_prompt[:200],
            "updated_at": _now(),
        }
        # Cascade: any rendered variant of this shot is now out of sync with its keyframe.
        # Mark them "stale" so the UI shows a warning + one-click re-render.
        shot = state["shots"][idx] if idx < len(state["shots"]) else None
        if shot:
            for v in shot.get("variants") or []:
                if v.get("status") == "ready":
                    v["stale"] = True
                    v["stale_reason"] = "keyframe edited — variant was rendered against the previous version"
            if shot.get("status") == "ready":
                shot["stale"] = True
        _save_state(run_id, state)
    if backup and backup.exists():
        try: backup.unlink(missing_ok=True)
        except Exception: pass
    logger.success(run_id, "keyframes", f"✓ keyframe {idx+1} edited (shot marked stale)")
    _record_taste("keyframe_edit", run_id=run_id, shot_idx=idx, edit_prompt=edit_prompt[:200])
    return kf_path


async def run_all_keyframes(run_id: str) -> None:
    """Generate every pending or failed keyframe, sequentially (for identity chain)."""
    state = get_state(run_id)
    n = len(state["keyframes"])
    for i in range(n):
        state = get_state(run_id)
        if i >= len(state["keyframes"]):
            break
        if state["keyframes"][i]["status"] == "ready":
            continue
        await run_keyframe(run_id, i)


# ─── Phase 3: shots (Seedance) ────────────────────────────────────────────

async def run_shot(
    run_id: str,
    idx: int,
    *,
    prompt_override: Optional[str] = None,
    variant_idx: int = 0,
    api_key: Optional[str] = None,
) -> Path:
    """Render (or regenerate) shot `idx`, variant `variant_idx`, via Seedance.
    Variant 0 is the primary take. Extra variants (1, 2, ...) use deterministic seeds
    derived from (idx, variant_idx) so re-rendering gives the same variant back."""
    async with _get_lock(run_id):
        state = get_state(run_id)
        story = state.get("story")
        if not story:
            raise TrailerNotReady("storyboard not generated yet")
        if idx < 0 or idx >= len(story["shots"]):
            raise IndexError(f"shot index {idx} out of range")
        if idx >= len(state.get("shots") or []):
            raise IndexError(f"shot slot {idx} out of range")
        if idx >= len(state.get("keyframes") or []):
            raise IndexError(f"keyframe slot {idx} out of range")

        num_variants = max(1, state.get("params", {}).get("variants_per_scene", 1))
        if variant_idx < 0 or variant_idx >= num_variants:
            raise IndexError(f"variant index {variant_idx} out of range (0..{num_variants-1})")

        _migrate_shot(state["shots"][idx], num_variants=num_variants)

        kf_info = state["keyframes"][idx]
        if kf_info["status"] != "ready" or not kf_info.get("path"):
            raise TrailerNotReady(f"keyframe {idx+1} not ready — generate it first")

        run_dir = _run_dir(run_id)
        kf_path = run_dir / kf_info["path"]
        if not kf_path.exists():
            raise TrailerError(f"keyframe {idx+1} file missing on disk: {kf_info['path']} — regenerate it")
        # Classify references by type — same logic as run_keyframe
        all_ref_paths = state.get("references", [])
        char_name_to_refs: dict[str, list[str]] = {}
        char_ref_set = set()
        for c in state.get("cast") or []:
            cname = (c.get("name") or "").strip().lower()
            crefs = c.get("ref_paths") or []
            for p in crefs:
                char_ref_set.add(p)
            if cname:
                char_name_to_refs[cname] = crefs
        loc_name_to_refs: dict[str, list[str]] = {}
        for loc in state.get("locations") or []:
            name = (loc.get("name") or "").strip().lower()
            if name:
                loc_name_to_refs[name] = loc.get("ref_paths") or []
        prop_name_to_refs: dict[str, list[str]] = {}
        for pr in state.get("props") or []:
            name = (pr.get("name") or "").strip().lower()
            if name:
                prop_name_to_refs[name] = pr.get("ref_paths") or []
        all_loc_refs = {p for paths in loc_name_to_refs.values() for p in paths}
        all_prop_refs = {p for paths in prop_name_to_refs.values() for p in paths}

        shot = story["shots"][idx]
        _raw_fc = shot.get("featured_characters")
        _raw_fl = shot.get("featured_locations")
        _raw_fp = shot.get("featured_props")
        featured_chars = {n.strip().lower() for n in _raw_fc} if _raw_fc is not None else None
        featured_locs = {n.strip().lower() for n in _raw_fl} if _raw_fl is not None else None
        featured_props_set = {n.strip().lower() for n in _raw_fp} if _raw_fp is not None else None
        plain_char_refs: list[Path] = []
        shot_loc_refs: list[Path] = []
        shot_prop_refs: list[Path] = []
        for rp in all_ref_paths:
            if rp in all_loc_refs:
                for lname, lpaths in loc_name_to_refs.items():
                    if rp in lpaths and (featured_locs is None or lname in featured_locs):
                        shot_loc_refs.append(run_dir / rp)
                        break
                continue
            if rp in all_prop_refs:
                for pname, ppaths in prop_name_to_refs.items():
                    if rp in ppaths and (featured_props_set is None or pname in featured_props_set):
                        shot_prop_refs.append(run_dir / rp)
                        break
                continue
            if rp in char_ref_set:
                matched = False
                for cname, cpaths in char_name_to_refs.items():
                    if rp in cpaths and (featured_chars is None or cname in featured_chars):
                        plain_char_refs.append(run_dir / rp)
                        matched = True
                        break
                if not matched:
                    continue
            else:
                plain_char_refs.append(run_dir / rp)
        ratio = state["params"]["ratio"]
        shot_duration = int(shot.get("duration_s", state["params"]["shot_duration"]))

        # Video references — carried through every regen of this shot. Supports
        # legacy single video_ref and the new video_refs list (up to 3 per Ark).
        shot_slot = state["shots"][idx]
        video_ref_paths: list[Path] = []
        vrefs_meta = shot_slot.get("video_refs") or []
        if not vrefs_meta and shot_slot.get("video_ref"):
            vrefs_meta = [shot_slot["video_ref"]]
        for m in vrefs_meta:
            if not m or not m.get("path"):
                continue
            cand = run_dir / m["path"]
            if cand.exists():
                video_ref_paths.append(cand)
        video_ref_path: Optional[Path] = video_ref_paths[0] if video_ref_paths else None
        vref = shot_slot.get("video_ref") or (vrefs_meta[0] if vrefs_meta else None)

        # Deterministic seed per (shot, variant) so variants are stable across re-renders
        variant_seed = 1_000_003 * (idx + 1) + 7919 * (variant_idx + 1)
        variant_slot = shot_slot["variants"][variant_idx]
        variant_slot["seed"] = variant_seed
        # Variant-level status
        shot_slot["variants"][variant_idx]["status"] = "generating"
        shot_slot["variants"][variant_idx]["error"] = None
        shot_slot["variants"][variant_idx]["updated_at"] = _now()
        # Shot-level aggregate status (derived; set here to show progress)
        shot_slot["status"] = "generating"
        shot_slot["error"] = None
        shot_slot["updated_at"] = _now()
        if prompt_override is not None:
            shot_slot["prompt_override"] = prompt_override or None
        state["status"] = "shots_partial"
        _save_state(run_id, state)
    vref_note = " (with attached camera ref)" if video_ref_path else ""
    variant_note = f" variant {variant_idx+1}/{num_variants}" if num_variants > 1 else ""
    logger.info(run_id, "shots", f"Seedance rendering shot {idx+1}/{len(story['shots'])}{variant_note} [{shot_duration}s]{vref_note}…")

    try:
        if prompt_override:
            motion = prompt_override
        elif shot_slot.get("prompt_override"):
            motion = shot_slot["prompt_override"]
        else:
            m_lib_props = _build_props_sheet(state.get("props"))
            m_story_props = story.get("prop_sheet", "")
            m_combined_props = _merge_props_sheets(m_lib_props, m_story_props)
            motion = _build_motion_prompt(
                shot, story.get("character_sheet", ""), story.get("world_sheet", ""),
                locations_sheet=_build_locations_sheet(state.get("locations")),
                props_sheet=m_combined_props,
            )

        refs = [kf_path] + plain_char_refs[:2] + shot_loc_refs[:1] + shot_prop_refs[:1]
        out_fname = f"shot_{idx+1:02d}_v{variant_idx}.mp4" if num_variants > 1 else f"shot_{idx+1:02d}.mp4"
        out_path = run_dir / "shots" / out_fname
        quality = state.get("params", {}).get("quality") or "standard"
        await seedance.render_shot(
            prompt=motion,
            reference_images=refs,
            output_path=out_path,
            ratio=ratio,
            duration=shot_duration,
            generate_audio=False,
            reference_video_path=video_ref_path,
            reference_video_paths=video_ref_paths,
            seed=variant_seed,
            api_key=api_key,
            run_id=run_id,
            quality=quality,
        )
        try:
            costs.log_video(run_id, model=seedance.resolve_model(quality), phase="shots")
        except Exception as e:
            logger.warn(run_id, "costs", f"cost logging failed (shots): {e}")

        async with _get_lock(run_id):
            state = get_state(run_id)
            if idx >= len(state.get("shots") or []):
                raise IndexError(f"shot {idx} disappeared during render (storyboard may have been regenerated)")
            _migrate_shot(state["shots"][idx], num_variants=num_variants)
            variants = state["shots"][idx]["variants"]
            if variant_idx < 0 or variant_idx >= len(variants):
                raise IndexError(f"variant_idx {variant_idx} out of range (shot {idx+1} has {len(variants)} variants)")
            state["shots"][idx]["variants"][variant_idx] = {
                "idx": variant_idx,
                "path": f"shots/{out_fname}",
                "seed": variant_seed,
                "status": "ready",
                "error": None,
                "stale": False,
                "stale_reason": None,
                "updated_at": _now(),
            }
            # Clear stale flag on shot if all variants fresh
            _shot_refresh = state["shots"][idx]
            if all(not v.get("stale") for v in _shot_refresh.get("variants") or []):
                _shot_refresh.pop("stale", None)
            # Keep `primary_variant` pointing at the first ready variant if not set yet
            if "primary_variant" not in state["shots"][idx]:
                state["shots"][idx]["primary_variant"] = variant_idx
            # Re-migrate to sync flat fields with primary
            _migrate_shot(state["shots"][idx], num_variants=num_variants)
            state["shots"][idx]["prompt_override"] = shot_slot.get("prompt_override")
            state["shots"][idx]["video_ref"] = vref
            _roll_up_variant_status(state["shots"][idx], num_variants)
            _roll_up_status(state)
            _save_state(run_id, state)
        logger.success(run_id, "shots", f"✓ shot {idx+1}{f' variant {variant_idx+1}' if num_variants > 1 else ''} ready ({out_path.stat().st_size // 1024} KB)")
        return out_path
    except Exception as e:
        # Ark charges per task submission regardless of outcome
        try:
            costs.log_video(run_id, model=seedance.resolve_model(quality), phase="shots_failed")
        except Exception as e2:
            print(f"[costs] shots_failed cost log failed: {e2}", file=sys.stderr)
        async with _get_lock(run_id):
            state = get_state(run_id)
            if idx >= len(state.get("shots") or []):
                logger.warn(run_id, "shots", f"shot {idx+1} disappeared before failure could be recorded")
            else:
                _migrate_shot(state["shots"][idx], num_variants=num_variants)
                if variant_idx < len(state["shots"][idx].get("variants") or []):
                    state["shots"][idx]["variants"][variant_idx]["status"] = "failed"
                    state["shots"][idx]["variants"][variant_idx]["error"] = str(e)[:400]
                    state["shots"][idx]["variants"][variant_idx]["updated_at"] = _now()
                _roll_up_variant_status(state["shots"][idx], num_variants)
                _roll_up_status(state)
                _save_state(run_id, state)
        logger.error(run_id, "shots", f"✗ shot {idx+1}{f' variant {variant_idx+1}' if num_variants > 1 else ''} failed: {e}")
        raise


def _roll_up_variant_status(shot: dict, num_variants: int):
    """Derive shot-level aggregate status from its variants."""
    variants = shot.get("variants") or []
    statuses = [v.get("status", "pending") for v in variants[:num_variants]]
    if not statuses:
        shot["status"] = "pending"
        return
    if all(s == "ready" for s in statuses):
        shot["status"] = "ready"
    elif any(s == "generating" for s in statuses):
        shot["status"] = "generating"
    elif any(s == "ready" for s in statuses):
        shot["status"] = "ready"  # at least one variant renders → usable
    elif any(s == "failed" for s in statuses):
        shot["status"] = "failed"
    else:
        shot["status"] = "pending"
    # Keep flat path in sync
    primary = shot.get("primary_variant", 0)
    if 0 <= primary < len(variants):
        shot["path"] = variants[primary].get("path")
        shot["error"] = variants[primary].get("error")


async def attach_video_ref(
    run_id: str,
    idx: int,
    *,
    filename: str,
    data: bytes,
    slot_idx: Optional[int] = None,
) -> dict:
    """Store + normalize an uploaded video to outputs/<run>/video_refs/shot_NN_sM.mp4.
    Trims to 15s max and downscales for payload sanity. Returns the stored meta.

    `slot_idx`: which video_refs slot (0-2, up to 3 per shot per Ark docs).
    Default: append to end (first free slot). Also keeps legacy `video_ref`
    pointing at slot 0 for backward compatibility.
    """
    state = get_state(run_id)
    if idx < 0 or idx >= len(state.get("shots", [])):
        raise IndexError(f"shot index {idx} out of range")
    if slot_idx is not None and (slot_idx < 0 or slot_idx > 2):
        raise ValueError("slot_idx must be 0, 1, or 2 (Ark supports up to 3 video refs)")

    run_dir = _run_dir(run_id)
    refs_dir = run_dir / "video_refs"
    refs_dir.mkdir(exist_ok=True)

    # Write to unique temp paths so slot assignment can happen atomically under
    # the lock after the (slow) normalize step. Prevents TOCTOU where concurrent
    # attaches pick the same slot or exceed the 3-ref limit.
    import uuid as _uuid
    tmp_id = _uuid.uuid4().hex[:12]
    suffix = Path(filename).suffix.lower() or ".mp4"
    raw_tmp = refs_dir / f"shot_{idx+1:02d}_tmp_{tmp_id}_raw{suffix}"
    raw_tmp.write_bytes(data)
    out_tmp = refs_dir / f"shot_{idx+1:02d}_tmp_{tmp_id}.mp4"
    try:
        info = await video.normalize_video_ref(raw_tmp, out_tmp)
    except Exception:
        try: raw_tmp.unlink(missing_ok=True)
        except Exception: pass
        try: out_tmp.unlink(missing_ok=True)
        except Exception: pass
        raise
    try: raw_tmp.unlink(missing_ok=True)
    except Exception as e: print(f"[cleanup] raw video unlink failed: {e}", file=sys.stderr)

    async with _get_lock(run_id):
        state = get_state(run_id)
        if idx >= len(state.get("shots") or []):
            try: out_tmp.unlink(missing_ok=True)
            except Exception: pass
            raise IndexError(f"shot {idx} disappeared during video ref upload")
        shot = state["shots"][idx]
        vrefs = shot.get("video_refs") or []
        if not vrefs and shot.get("video_ref"):
            vrefs = [shot["video_ref"]]
        # Resolve slot now that we hold the lock and see current state
        final_slot = slot_idx if slot_idx is not None else len(vrefs)
        if final_slot < 0 or final_slot > 2:
            try: out_tmp.unlink(missing_ok=True)
            except Exception: pass
            raise ValueError(f"no free video ref slot (have {len(vrefs)}/3) — detach one first")

        final_out = refs_dir / f"shot_{idx+1:02d}_s{final_slot}.mp4"
        # If an existing file is in the final slot, remove it first
        if final_out.exists():
            try: final_out.unlink()
            except Exception as e: print(f"[cleanup] pre-rename unlink failed: {e}", file=sys.stderr)
        try:
            out_tmp.replace(final_out)
        except Exception:
            try: out_tmp.unlink(missing_ok=True)
            except Exception: pass
            raise

        meta = {
            "path": f"video_refs/{final_out.name}",
            "filename": filename,
            "duration": round(info["duration"], 2),
            "trimmed_from": round(info["trimmed_from"], 2) if info.get("trimmed_from") else None,
            "size": info.get("size"),
            "width": info.get("width"),
            "height": info.get("height"),
            "attached_at": _now(),
        }
        # Pad list up to final_slot
        while len(vrefs) <= final_slot:
            vrefs.append(None)
        vrefs[final_slot] = meta
        # Prune trailing Nones
        while vrefs and vrefs[-1] is None:
            vrefs.pop()
        shot["video_refs"] = vrefs
        # Keep legacy single-ref field in sync with slot 0 for any older code paths
        shot["video_ref"] = vrefs[0] if vrefs else None
        _save_state(run_id, state)
    logger.success(run_id, "shots", f"shot {idx+1}: attached video ref to slot {final_slot+1} ({meta['duration']}s)")
    return meta


async def detach_video_ref_slot(run_id: str, idx: int, slot_idx: int) -> None:
    """Remove a specific video_refs slot. Legacy detach_video_ref clears all."""
    async with _get_lock(run_id):
        state = get_state(run_id)
        if idx < 0 or idx >= len(state.get("shots", [])):
            raise IndexError(f"shot index {idx} out of range")
        shot = state["shots"][idx]
        vrefs = shot.get("video_refs") or []
        if not vrefs and shot.get("video_ref"):
            vrefs = [shot["video_ref"]]
        if slot_idx < 0 or slot_idx >= len(vrefs):
            return
        target = vrefs[slot_idx]
        if target and target.get("path"):
            try: (_run_dir(run_id) / target["path"]).unlink(missing_ok=True)
            except Exception as e: print(f"[cleanup] video ref slot unlink failed: {e}", file=sys.stderr)
        vrefs.pop(slot_idx)
        shot["video_refs"] = vrefs
        shot["video_ref"] = vrefs[0] if vrefs else None
        _save_state(run_id, state)


async def detach_video_ref(run_id: str, idx: int) -> None:
    async with _get_lock(run_id):
        state = get_state(run_id)
        if idx < 0 or idx >= len(state.get("shots", [])):
            raise IndexError(f"shot index {idx} out of range")
        shot = state["shots"][idx]
        run_dir = _run_dir(run_id)
        all_refs = list(shot.get("video_refs") or [])
        legacy = shot.get("video_ref")
        if legacy and legacy not in all_refs:
            all_refs.append(legacy)
        for vref in all_refs:
            if vref and vref.get("path"):
                try: (run_dir / vref["path"]).unlink(missing_ok=True)
                except Exception as e: print(f"[cleanup] video ref unlink failed: {e}", file=sys.stderr)
        shot["video_ref"] = None
        shot["video_refs"] = []
        _save_state(run_id, state)


async def sweep_shot_prompts(run_id: str, shot_idx: int, *, n: int = 3) -> dict:
    """Generate n distinct motion prompts for one shot via Claude, then render
    each as a variant. Consumes `params.variants_per_scene` slots (expands if
    needed). Each variant's `prompt_override` holds its distinct motion prompt."""
    state = get_state(run_id)
    story = state.get("story")
    if not story:
        raise TrailerNotReady("storyboard not generated yet")
    if shot_idx < 0 or shot_idx >= len(story.get("shots", [])):
        raise IndexError(f"shot index {shot_idx} out of range")

    shot = story["shots"][shot_idx]
    logger.info(run_id, "sweep", f"Claude writing {n} distinct motion prompts for shot {shot_idx+1}…")
    try:
        variants = await asyncio.to_thread(
            storyboard.sweep_motion_prompts,
            shot=shot, story=story, n=n, run_id=run_id,
        )
    except Exception as e:
        logger.error(run_id, "sweep", f"✗ prompt sweep failed: {e}")
        raise

    if len(variants) < n:
        logger.warn(run_id, "sweep", f"Claude returned {len(variants)}/{n} variants — rendering what we got")

    # Ensure the shot has n variant slots + store each distinct prompt.
    # Re-validate inside the lock — the storyboard may have been regenerated
    # during the Claude call above, shifting or removing the shot slot.
    async with _get_lock(run_id):
        state = get_state(run_id)
        story = state.get("story")
        if not story:
            raise TrailerNotReady("storyboard was cleared during sweep")
        if shot_idx < 0 or shot_idx >= len(story.get("shots", [])):
            raise IndexError(f"shot index {shot_idx} out of range after sweep")
        if shot_idx >= len(state.get("shots") or []):
            raise IndexError(f"shot slot {shot_idx} out of range after sweep")
        state["params"]["variants_per_scene"] = max(state["params"].get("variants_per_scene", 1), n)
        _migrate_shot(state["shots"][shot_idx], num_variants=n)
        for i, v in enumerate(variants[:n]):
            slot = state["shots"][shot_idx]["variants"][i]
            slot["sweep_prompt"] = v.get("motion_prompt")
            slot["sweep_camera_verb"] = v.get("camera_verb")
            slot["sweep_why_different"] = v.get("why_different")
            slot["status"] = "pending"
            slot["path"] = None
            slot["error"] = None
        _save_state(run_id, state)

    # Render all n variants concurrently, each with its distinct prompt
    logger.info(run_id, "sweep", f"rendering {len(variants)} variant(s) with distinct prompts…")
    num_keys = max(1, len(seedance.KEYS) or 1)
    sem = asyncio.Semaphore(num_keys)

    async def _one(variant_idx: int, motion_prompt: str):
        async with sem:
            try:
                await run_shot(run_id, shot_idx, prompt_override=motion_prompt, variant_idx=variant_idx)
            except Exception as e:
                logger.warn(run_id, "sweep", f"sweep variant {variant_idx+1} failed: {e}")

    await asyncio.gather(*(_one(i, v.get("motion_prompt", "")) for i, v in enumerate(variants[:n])))

    logger.success(run_id, "sweep", f"✓ sweep complete — {n} takes rendered; pick the winner")
    return {"variants": variants, "shot_idx": shot_idx}


async def run_all_shots(run_id: str) -> None:
    """Render every pending or failed shot/variant concurrently (bounded by key pool size).
    In rip-o-matic mode with variants_per_scene > 1, each scene gets multiple takes.
    Keys are pre-assigned round-robin so each concurrent slot uses a distinct API key."""
    state = get_state(run_id)
    n_shots = len(state["shots"])
    num_variants = max(1, state.get("params", {}).get("variants_per_scene", 1))
    num_keys = max(1, len(seedance.KEYS) or 1)
    sem = asyncio.Semaphore(num_keys)

    # Build work items and pre-assign keys round-robin for deterministic distribution
    work: list[tuple[int, int, str]] = []
    for shot_idx in range(n_shots):
        for variant_idx in range(num_variants):
            key = seedance.KEYS[len(work) % num_keys] if seedance.KEYS else None
            work.append((shot_idx, variant_idx, key))

    async def _one(shot_idx: int, variant_idx: int, api_key: str | None):
        st = get_state(run_id)
        if shot_idx >= len(st["shots"]):
            return
        shot = st["shots"][shot_idx]
        if shot.get("variants") and variant_idx < len(shot["variants"]):
            if shot["variants"][variant_idx].get("status") == "ready":
                return
        async with sem:
            try:
                await run_shot(run_id, shot_idx, variant_idx=variant_idx, api_key=api_key)
            except Exception as e:
                logger.warn(run_id, "shots", f"shot {shot_idx+1} variant {variant_idx+1} failed in batch: {e}")

    await asyncio.gather(*[_one(si, vi, k) for si, vi, k in work])


async def set_primary_variant(run_id: str, shot_idx: int, variant_idx: int) -> dict:
    """Pick which variant of a shot is the 'primary' — the one that gets used by
    the cut plan and stitch by default. Users pick the best take this way."""
    async with _get_lock(run_id):
        state = get_state(run_id)
        if shot_idx < 0 or shot_idx >= len(state["shots"]):
            raise IndexError(f"shot index {shot_idx} out of range")
        shot = state["shots"][shot_idx]
        variants = shot.get("variants") or []
        if variant_idx < 0 or variant_idx >= len(variants):
            raise IndexError(f"variant index {variant_idx} out of range")
        if variants[variant_idx].get("status") != "ready":
            raise TrailerNotReady("only ready variants can be made primary")
        shot["primary_variant"] = variant_idx
        shot["path"] = variants[variant_idx].get("path")
        shot["status"] = variants[variant_idx].get("status")
        _save_state(run_id, state)
    logger.info(run_id, "shots", f"shot {shot_idx+1}: primary variant set to {variant_idx+1}")
    _record_taste("variant_pick", run_id=run_id, shot_idx=shot_idx, variant_idx=variant_idx)
    return shot


# ─── Phase 3.5: review / cut plan (vision) ───────────────────────────────

async def run_continuity_check(run_id: str) -> dict:
    """Claude vision compares every adjacent shot pair and flags continuity breaks.
    Runs on contact sheets already extracted for the cut plan (or extracts them)."""
    state = get_state(run_id)
    story = state.get("story")
    if not story:
        raise TrailerNotReady("storyboard required")
    shots = state.get("shots") or []
    if not all(s.get("status") == "ready" for s in shots):
        raise TrailerNotReady("all shots must be ready before continuity check")

    sheets = await run_contact_sheets(run_id)
    run_dir = _run_dir(run_id)
    contact_sheets_abs = []
    for s in sheets:
        abs_frames = [{"path": run_dir / f["path"], "t": f["t"]} for f in s.get("frames", [])]
        contact_sheets_abs.append({"idx": s["idx"], "frames": abs_frames})

    logger.info(run_id, "continuity", f"Claude watching {len(shots)-1} adjacent pair(s)…")
    state = get_state(run_id)
    state["continuity_status"] = "checking"
    _save_state(run_id, state)
    try:
        result = await asyncio.to_thread(review.check_continuity, contact_sheets_abs, story, run_id=run_id)
    except Exception as e:
        state = get_state(run_id)
        state["continuity_status"] = "failed"
        state["continuity_error"] = str(e)[:400]
        _save_state(run_id, state)
        logger.error(run_id, "continuity", f"✗ {e}")
        raise

    pairs = result.get("pairs") or []
    by_sev = {"ok": 0, "minor": 0, "major": 0, "unsure": 0}
    for p in pairs:
        by_sev[p.get("severity", "unsure")] = by_sev.get(p.get("severity", "unsure"), 0) + 1
    result["summary"] = by_sev
    result["checked_at"] = _now()

    state = get_state(run_id)
    state["continuity"] = result
    state["continuity_status"] = "ready"
    state.pop("continuity_error", None)
    _save_state(run_id, state)
    flagged = by_sev["major"] + by_sev["minor"]
    logger.success(run_id, "continuity", f"✓ checked {len(pairs)} pair(s) — {by_sev['major']} major, {by_sev['minor']} minor, {by_sev['ok']} clean")
    return result


async def run_contact_sheets(run_id: str, *, n_frames: int = 5) -> list[dict]:
    """Extract contact-sheet frames for every ready shot. Returns
    [{'idx': i, 'frames': [{'path': '...', 't': 0.25}, ...]}, ...] (paths relative
    to the run dir so they survive in state.json).
    """
    state = get_state(run_id)
    run_dir = _run_dir(run_id)
    shots = state.get("shots") or []

    sheets: list[dict] = []
    for si, s in enumerate(shots):
        if s["status"] != "ready" or not s.get("path"):
            continue
        video_path = run_dir / s["path"]
        if not video_path.exists():
            continue
        shot_idx = s.get("idx", si)
        sheet_dir = run_dir / "contact_sheets" / f"shot_{shot_idx+1:02d}"
        # Re-extract if frame count changed, no frames exist, or video is newer than frames
        existing = sorted(sheet_dir.glob("frame_*.png")) if sheet_dir.exists() else []
        video_mtime = video_path.stat().st_mtime
        frames_stale = existing and any(p.stat().st_mtime < video_mtime for p in existing)
        if existing and len(existing) == n_frames and not frames_stale:
            frames = []
            for p in existing:
                # Parse "frame_NN_T.TTs.png" → timestamp
                try:
                    t = float(p.stem.split("_")[-1].rstrip("s"))
                except (ValueError, IndexError) as e:
                    logger.warn(run_id, "contact_sheets", f"failed to parse timestamp from {p.name}: {e}")
                    t = 0.0
                frames.append({"path": str(p.relative_to(run_dir)), "t": t})
        else:
            extracted = await asyncio.to_thread(
                video.extract_frames, video_path, sheet_dir, n_frames=n_frames
            )
            frames = [
                {"path": str(Path(f["path"]).relative_to(run_dir)), "t": f["t"]}
                for f in extracted
            ]
        sheets.append({"idx": shot_idx, "frames": frames})

    # Persist paths so the UI can render thumbnails
    async with _get_lock(run_id):
        state = get_state(run_id)
        state["contact_sheets"] = sheets
        _save_state(run_id, state)
    return sheets


async def run_cut_plan(run_id: str) -> dict:
    """Extract contact sheets (if missing), then ask Claude for an edit plan."""
    state = get_state(run_id)
    story = state.get("story")
    if not story:
        raise TrailerNotReady("storyboard missing")

    shots = state.get("shots") or []
    if not all(s["status"] == "ready" for s in shots):
        raise TrailerNotReady("all shots must be ready before generating a cut plan")

    logger.info(run_id, "review", f"extracting contact sheets from {len(shots)} shots…")
    # Phase 1: contact sheets
    sheets = await run_contact_sheets(run_id)
    logger.info(run_id, "review", f"Claude is watching the rushes ({len(shots)} shots, typically 30-60s)…")

    # Phase 2: Claude vision pass
    run_dir = _run_dir(run_id)
    contact_sheets_abs = []
    for s in sheets:
        abs_frames = [
            {"path": run_dir / f["path"], "t": f["t"]} for f in s.get("frames", [])
        ]
        contact_sheets_abs.append({"idx": s["idx"], "frames": abs_frames})

    # Actual rendered durations (ffprobe each shot — more reliable than storyboard value)
    shot_durations = []
    for s in shots:
        p = run_dir / s["path"]
        try:
            d = video._probe_duration_sync(p) if p.exists() else float(s.get("duration_s", 5))
        except (RuntimeError, ValueError, OSError) as e:
            logger.warn(run_id, "cut_plan", f"probe failed for {p.name}: {e}")
            d = float(s.get("duration_s", 5))
        shot_durations.append(d or 5.0)

    async with _get_lock(run_id):
        state = get_state(run_id)
        state["cut_plan_status"] = "generating"
        _save_state(run_id, state)

    try:
        plan = await asyncio.to_thread(
            review.analyze_shots,
            contact_sheets_abs,
            story,
            shot_durations=shot_durations,
            run_id=run_id,
        )
    except Exception as e:
        async with _get_lock(run_id):
            state = get_state(run_id)
            state["cut_plan_status"] = "failed"
            state["cut_plan_error"] = str(e)[:500]
            _save_state(run_id, state)
        logger.error(run_id, "review", f"✗ cut plan failed: {e}")
        raise

    plan["generated_at"] = _now()
    plan["approved"] = False
    plan["shot_durations"] = shot_durations

    async with _get_lock(run_id):
        state = get_state(run_id)
        timeline = _build_source_rhythm_timeline(state)
        if timeline:
            plan["timeline"] = timeline
            logger.info(run_id, "review", f"timeline built from source rhythm — {len(timeline['entries'])} slices summing to {timeline['total_duration']:.1f}s")
        state["cut_plan"] = plan
        state["cut_plan_status"] = "ready"
        state.pop("cut_plan_error", None)
        _save_state(run_id, state)
    flagged = sum(1 for s in plan.get("shots", []) if s.get("regenerate_recommended"))
    logger.success(run_id, "review", f"✓ cut plan ready — {len(plan.get('shots', []))} shots analyzed" + (f", {flagged} flagged for regen" if flagged else ""))
    return plan


def _build_source_rhythm_timeline(state: dict) -> Optional[dict]:
    """For a rip-o-matic run, produce a slice timeline that reconstructs the source's
    cut rhythm using our rendered variants. Each source cut window becomes one slice
    drawn from the covering scene's primary (or a fallback) variant, at the
    time-proportional position within that variant's duration.

    Returns None if we're not in rip mode or don't have enough info."""
    source = state.get("source_video") or {}
    cuts = source.get("cut_timeline") or []
    segments = source.get("segments") or []
    if not cuts or not segments:
        return None
    duration = source.get("duration") or 0.0
    if duration <= 0:
        return None

    # Build full boundary list including 0.0 and the source's end
    boundaries = sorted(set([0.0] + [float(c) for c in cuts] + [float(duration)]))

    entries: list[dict] = []
    for i in range(len(boundaries) - 1):
        source_start = round(boundaries[i], 3)
        source_end = round(boundaries[i + 1], 3)
        window = source_end - source_start
        if window < 0.20:  # skip sub-200ms noise cuts
            continue

        # Find covering scene
        scene_idx = None
        for j, seg in enumerate(segments):
            if seg["start"] <= source_start < seg["end"]:
                scene_idx = j
                break
        if scene_idx is None:
            # Edge case: source_start past the last seg — stick to last scene
            scene_idx = len(segments) - 1

        shot = state["shots"][scene_idx] if scene_idx < len(state.get("shots", [])) else None
        if not shot:
            continue
        variants = shot.get("variants") or []
        # Prefer primary variant; fall back to first ready
        variant_idx = shot.get("primary_variant", 0)
        if variant_idx >= len(variants) or variants[variant_idx].get("status") != "ready":
            variant_idx = next(
                (vi for vi, v in enumerate(variants) if v.get("status") == "ready"),
                variant_idx,
            )

        # Proportional slice: where in the variant's timeline does this source window land?
        seg = segments[scene_idx]
        scene_duration = max(0.1, seg["end"] - seg["start"])
        frac_in = max(0.0, min(1.0, (source_start - seg["start"]) / scene_duration))
        frac_out = max(0.0, min(1.0, (source_end - seg["start"]) / scene_duration))
        # Variant's Seedance-rendered duration = the scene's source duration (approx)
        variant_dur = scene_duration
        slice_in = round(frac_in * variant_dur, 3)
        slice_out = round(frac_out * variant_dur, 3)
        if slice_out - slice_in < 0.20:
            continue

        entries.append({
            "source_start": source_start,
            "source_end": source_end,
            "shot_idx": scene_idx,
            "variant_idx": variant_idx,
            "slice_in": slice_in,
            "slice_out": slice_out,
            "duration": round(slice_out - slice_in, 3),
            "reasoning": f"source cut window {source_start:.2f}–{source_end:.2f}s → scene {scene_idx+1} take {variant_idx+1}",
        })

    # If alternate variants exist, rotate between them across consecutive slices WITHIN
    # the same scene — gives the final trailer the intercut energy without needing
    # separate Claude analysis for each slice.
    by_scene: dict[int, list[int]] = {}
    for i, e in enumerate(entries):
        by_scene.setdefault(e["shot_idx"], []).append(i)
    for scene_idx, slice_idxs in by_scene.items():
        if scene_idx >= len(state.get("shots", [])):
            continue
        shot = state["shots"][scene_idx]
        ready_variants = [vi for vi, v in enumerate(shot.get("variants") or []) if v.get("status") == "ready"]
        if len(ready_variants) > 1 and len(slice_idxs) > 1:
            for k, slice_i in enumerate(slice_idxs):
                entries[slice_i]["variant_idx"] = ready_variants[k % len(ready_variants)]
                entries[slice_i]["reasoning"] += f" (alt take {entries[slice_i]['variant_idx']+1})"

    return {
        "entries": entries,
        "total_duration": round(sum(e["duration"] for e in entries), 2),
        "source_cut_count": len([b for b in boundaries if 0 < b < duration]),
    }


async def auto_regen_flagged_shots(run_id: str, *, max_retries: int = 2) -> dict:
    """After a cut plan is generated, scan for shots flagged `regenerate_recommended`
    and auto-regen them. Retries up to max_retries per shot. Returns summary."""
    state = get_state(run_id)
    cut_plan = state.get("cut_plan") or {}
    flagged = [s for s in cut_plan.get("shots", []) if s.get("regenerate_recommended")]
    if not flagged:
        logger.info(run_id, "review", "no shots flagged for regeneration")
        return {"attempted": 0, "succeeded": 0, "failed": 0}

    logger.info(run_id, "review", f"auto-regen: {len(flagged)} shot(s) flagged by the reviewer")
    succeeded = 0
    failed = 0
    for p in flagged:
        idx = p.get("idx")
        if idx is None:
            continue
        logger.info(run_id, "review", f"auto-regenerating shot {idx+1} (flagged: {p.get('defects') or 'quality'})…")
        for attempt in range(max_retries):
            try:
                await run_shot(run_id, idx)
                succeeded += 1
                break
            except Exception as e:
                logger.warn(run_id, "review", f"shot {idx+1} regen attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    failed += 1
    logger.success(run_id, "review", f"auto-regen complete — {succeeded} ok, {failed} still failing")
    return {"attempted": len(flagged), "succeeded": succeeded, "failed": failed}


async def update_cut_plan(run_id: str, edited_plan: dict) -> dict:
    """User-edited cut plan (trim values, approval flag)."""
    if not isinstance(edited_plan, dict):
        raise TrailerUserError("cut plan must be a JSON object")
    shots = edited_plan.get("shots")
    if shots is not None and not isinstance(shots, list):
        raise TrailerUserError("cut_plan.shots must be a list")
    timeline = edited_plan.get("timeline")
    if timeline is not None:
        if not isinstance(timeline, dict):
            raise TrailerUserError("cut_plan.timeline must be an object")
        entries = timeline.get("entries")
        if entries is not None and not isinstance(entries, list):
            raise TrailerUserError("cut_plan.timeline.entries must be a list")
    async with _get_lock(run_id):
        state = get_state(run_id)
        if not state.get("cut_plan"):
            raise TrailerNotReady("no cut plan to update")
        preserved = {
            "generated_at": state["cut_plan"].get("generated_at"),
            "shot_durations": state["cut_plan"].get("shot_durations", []),
        }
        state["cut_plan"] = {**edited_plan, **preserved}
        _save_state(run_id, state)
        return state["cut_plan"]


# Title-card + platform-variant export + subtitle-file helpers moved to export.py.
# VO phase (script generation, TTS, deletion) moved to vo.py.
# Both re-exported here so callers that grew up importing from `pipeline` keep working.
from export import (
    generate_title_card, remove_title_card,
    export_platform_variants, build_subtitle_file,
)
from vo import (
    _vo_total_trailer_duration,
    generate_vo_script, update_vo_script, synthesize_vo, remove_vo,
)


async def compose_music(run_id: str, *, vibe: str = "") -> dict:
    """Have Claude write a music brief from the storyboard, then ElevenLabs
    composes a bespoke track. Stores it the same way as uploaded music — so
    downstream beat-snap, mix, export all work unchanged."""
    state = get_state(run_id)
    story = state.get("story")
    if not story:
        raise TrailerNotReady("storyboard required before composing music")

    total = _vo_total_trailer_duration(state)
    genre = state.get("params", {}).get("genre", "neutral")
    logger.info(run_id, "music", f"Claude writing music brief ({total:.0f}s, genre={genre})…")
    try:
        brief = await asyncio.to_thread(
            audio_mod.generate_music_brief,
            story=story, duration_s=total, vibe=vibe, genre=genre, run_id=run_id,
        )
    except Exception as e:
        logger.error(run_id, "music", f"✗ brief generation failed: {e}")
        raise

    logger.info(run_id, "music", f"brief: {brief[:200]}")
    logger.info(run_id, "music", f"ElevenLabs composing {total:.0f}s of bespoke music…")

    run_dir = _run_dir(run_id)
    music_dir = run_dir / "music"
    music_dir.mkdir(exist_ok=True)
    dst = music_dir / "track.mp3"

    try:
        await audio_mod.compose_music(brief, dst, duration_seconds=total)
    except Exception as e:
        logger.error(run_id, "music", f"✗ composition failed: {e}")
        raise

    # Analyze the composed track through the same librosa pipeline as uploads
    try:
        analysis = await asyncio.to_thread(music_mod.analyze, dst)
    except Exception as e:
        logger.warn(run_id, "music", f"analysis failed on composed track: {e}")
        analysis = {"bpm": 0, "beats": [], "downbeats": [], "energy_spikes": [], "duration": total, "dynamic_range": 0}

    meta = {
        "path": f"music/{dst.name}",
        "filename": "ai_composed.mp3",
        "analysis": analysis,
        "brief": brief,
        "composed": True,
        "attached_at": _now(),
    }
    async with _get_lock(run_id):
        state = get_state(run_id)
        state["music"] = meta
        _save_state(run_id, state)
    # Log cost (ElevenLabs Music beta: ~$0.03 per 10s)
    try:
        cost = (total / 10) * 0.03
        costs._write(run_id, {
            "ts": _now(), "provider": "elevenlabs", "model": "music-beta",
            "phase": "music_compose", "duration_s": total, "cost_usd": round(cost, 6),
        })
    except Exception as e: print(f"[costs] music_compose cost log failed: {e}", file=sys.stderr)
    logger.success(run_id, "music", f"✓ bespoke music composed ({analysis.get('bpm', 0)} BPM · {len(analysis.get('beats') or [])} beats · {dst.stat().st_size // 1024} KB)")
    return meta


async def attach_music(
    run_id: str,
    *,
    filename: str,
    data: bytes,
) -> dict:
    """Save + analyze a music track for this run. Stores in state.music."""
    run_dir = _run_dir(run_id)
    music_dir = run_dir / "music"
    music_dir.mkdir(exist_ok=True)

    suffix = Path(filename).suffix.lower() or ".mp3"
    if suffix not in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"):
        suffix = ".mp3"
    dst = music_dir / f"track{suffix}"
    dst.write_bytes(data)

    logger.info(run_id, "music", f"analyzing {filename} ({len(data)//1024} KB)…")
    try:
        analysis = await asyncio.to_thread(music_mod.analyze, dst)
    except Exception as e:
        async with _get_lock(run_id):
            state = get_state(run_id)
            state["music"] = {
                "path": f"music/{dst.name}",
                "filename": filename,
                "analysis": None,
                "error": f"analysis failed: {e}",
                "attached_at": _now(),
            }
            _save_state(run_id, state)
        logger.error(run_id, "music", f"✗ analysis failed: {e}")
        raise

    meta = {
        "path": f"music/{dst.name}",
        "filename": filename,
        "analysis": analysis,
        "attached_at": _now(),
    }
    async with _get_lock(run_id):
        state = get_state(run_id)
        state["music"] = meta
        _save_state(run_id, state)
    logger.success(run_id, "music", f"✓ {analysis['bpm']} BPM · {len(analysis['beats'])} beats · {len(analysis['energy_spikes'])} energy spikes · dynamic range {analysis['dynamic_range']} LU")
    return meta


async def detach_music(run_id: str) -> None:
    async with _get_lock(run_id):
        state = get_state(run_id)
        m = state.get("music")
        if m and m.get("path"):
            p = _run_dir(run_id) / m["path"]
            try: p.unlink(missing_ok=True)
            except Exception as e: print(f"[cleanup] music unlink failed: {e}", file=sys.stderr)
        state["music"] = None
        _save_state(run_id, state)


def score_timeline_vs_music(run_id: str) -> Optional[dict]:
    """Return a scorecard for the CURRENT cut plan timeline against the attached track."""
    state = get_state(run_id)
    music_meta = state.get("music") or {}
    analysis = music_meta.get("analysis")
    cut_plan = state.get("cut_plan") or {}
    timeline = cut_plan.get("timeline")
    if not analysis or not timeline or not timeline.get("entries"):
        return None
    return music_mod.score_current_edit(timeline["entries"], analysis)


async def refine_timeline_with_vision(run_id: str) -> dict:
    """Use Claude vision to refine the mechanical timeline. Looks at contact sheets
    of each variant and picks taste-based (vs round-robin mechanical). Mutates
    state.cut_plan.timeline in place."""
    state = get_state(run_id)
    cut_plan = state.get("cut_plan") or {}
    timeline = cut_plan.get("timeline")
    if not timeline or not timeline.get("entries"):
        raise TrailerNotReady("no timeline to refine — generate a cut plan first")

    run_dir = _run_dir(run_id)

    # Gather contact sheets for every READY variant of every shot referenced in timeline
    referenced_shots = set(e["shot_idx"] for e in timeline["entries"])
    sheets_by_shot: dict[int, dict] = {}
    sheet_dir_root = run_dir / "contact_sheets"
    shots = state.get("shots") or []
    for shot_idx in referenced_shots:
        if shot_idx < 0 or shot_idx >= len(shots):
            continue
        shot = shots[shot_idx]
        per_variant = {}
        for vi, v in enumerate(shot.get("variants") or []):
            if v.get("status") != "ready" or not v.get("path"):
                continue
            # Extract a contact sheet from this variant (re-uses existing video.extract_frames)
            sheet_dir = sheet_dir_root / f"shot_{shot_idx+1:02d}_v{vi}"
            existing = sorted(sheet_dir.glob("frame_*.png")) if sheet_dir.exists() else []
            if existing:
                frames = []
                for p in existing:
                    try:
                        t = float(p.stem.split("_")[-1].rstrip("s"))
                    except (ValueError, IndexError):
                        logger.warn(run_id, "review", f"failed to parse timestamp from {p.name}")
                        t = 0.0
                    frames.append({"path": str(p.resolve()), "t": t})
            else:
                video_path = run_dir / v["path"]
                extracted = await asyncio.to_thread(
                    video.extract_frames, video_path, sheet_dir, n_frames=5
                )
                frames = [{"path": str(f["path"].resolve()), "t": f["t"]} for f in extracted]
            per_variant[vi] = frames
        if per_variant:
            sheets_by_shot[shot_idx] = per_variant

    logger.info(run_id, "review", f"Claude refining {len(timeline['entries'])} timeline slices via vision…")
    try:
        refined = await asyncio.to_thread(
            review.refine_timeline_with_vision,
            timeline["entries"],
            sheets_by_shot,
            state["story"],
            run_id=run_id,
        )
    except Exception as e:
        logger.error(run_id, "review", f"✗ vision refine failed: {e}")
        raise

    # Save under lock to prevent concurrent state clobber
    async with _get_lock(run_id):
        state = get_state(run_id)
        cp = state.get("cut_plan") or {}
        tl = cp.get("timeline")
        if not tl:
            raise TrailerError("timeline disappeared during vision refine")
        tl["entries"] = refined
        tl["total_duration"] = round(sum(e["duration"] for e in refined), 3)
        tl["vision_refined_at"] = _now()
        _save_state(run_id, state)
    changed = sum(1 for o, n in zip(timeline["entries"], refined) if o.get("variant_idx") != n.get("variant_idx"))
    logger.success(run_id, "review", f"✓ vision refined — {changed}/{len(refined)} slices changed variant")
    return {"changed": changed, "total": len(refined)}


async def snap_timeline_to_music(run_id: str) -> dict:
    """Apply beat-snap to the current cut plan timeline. Mutates state.cut_plan.timeline."""
    async with _get_lock(run_id):
        state = get_state(run_id)
        music_meta = state.get("music") or {}
        analysis = music_meta.get("analysis")
        cut_plan = state.get("cut_plan") or {}
        timeline = cut_plan.get("timeline")
        if not analysis:
            raise TrailerNotReady("no music attached — upload a track first")
        if not timeline or not timeline.get("entries"):
            raise TrailerNotReady("no timeline to snap — generate a cut plan first")

        logger.info(run_id, "music", f"snapping {len(timeline['entries'])} timeline slices to {analysis['bpm']} BPM grid…")
        result = music_mod.snap_timeline_to_beats(timeline["entries"], analysis)
        timeline["entries"] = result["entries"]
        timeline["snap_report"] = result["report"]
        timeline["total_duration"] = round(sum(e["duration"] for e in result["entries"]), 3)
        cut_plan["timeline"] = timeline
        state["cut_plan"] = cut_plan
        _save_state(run_id, state)
    logger.success(run_id, "music", f"✓ snapped {result['report']['snapped']}/{result['report']['total']} — sync score {result['report']['sync_score']*100:.0f}%")
    return result["report"]


async def delete_cut_plan(run_id: str) -> None:
    async with _get_lock(run_id):
        state = get_state(run_id)
        state["cut_plan"] = None
        state.pop("cut_plan_status", None)
        state.pop("cut_plan_error", None)
        _save_state(run_id, state)


# ─── Phase 4: stitch ──────────────────────────────────────────────────────

async def run_stitch(run_id: str, *, crossfade: bool = False, use_cut_plan: bool = True) -> Path:
    """Stitch the trailer. If an approved cut plan exists and use_cut_plan, every
    shot is trimmed to the plan's cut_in/cut_out first. Otherwise uses raw shots.

    Failure semantics: if any step raises (ffmpeg crash, SIGKILL, disk full) we
    unlink trailer.mp4 and all intermediate files (trailer_graded.mp4,
    trailer_with_audio.mp4) before re-raising. Without this cleanup, a partial
    mp4 on disk can silently corrupt the *next* stitch attempt when ffmpeg's
    concat demuxer reads it as input."""
    state = get_state(run_id)
    shots = state.get("shots") or []
    ready = [s for s in shots if s["status"] == "ready" and s.get("path")]
    if not ready:
        raise TrailerNotReady("no rendered shots to stitch")

    cut_plan = state.get("cut_plan")
    apply_plan = bool(use_cut_plan and cut_plan and cut_plan.get("approved"))

    state["status"] = "stitching"
    _save_state(run_id, state)
    logger.info(run_id, "stitch", f"ffmpeg stitching {len(ready)} shots{' with cut-plan trims' if apply_plan else ''}{' + crossfade' if crossfade else ''}…")

    run_dir = _run_dir(run_id)
    final_path = run_dir / "trailer.mp4"
    # Delete any stale output from a previous failed attempt BEFORE we start —
    # this guarantees the concat demuxer never reads a partial file as input.
    for stale in (final_path, run_dir / "trailer_graded.mp4", run_dir / "trailer_with_audio.mp4"):
        try:
            stale.unlink(missing_ok=True)
        except Exception as e:
            logger.warn(run_id, "stitch", f"could not remove stale {stale.name}: {e}")

    # Build the list of clip paths — timeline, trimmed, or raw
    clip_paths: list[Path] = []
    try:
        timeline = (cut_plan or {}).get("timeline") if apply_plan else None
        if apply_plan and timeline and timeline.get("entries"):
            # Timeline mode: slice each variant at each timeline entry's window.
            # Reconstructs the source's cut rhythm using our rendered material.
            slices_dir = run_dir / "slices"
            slices_dir.mkdir(exist_ok=True)
            n_shots = len(state["shots"])
            for seq, entry in enumerate(timeline["entries"]):
                shot_idx = entry.get("shot_idx")
                variant_idx = entry.get("variant_idx")
                if shot_idx is None or variant_idx is None:
                    logger.warn(run_id, "stitch", f"timeline entry {seq}: missing shot_idx or variant_idx, skipping")
                    continue
                if shot_idx < 0 or shot_idx >= n_shots:
                    logger.warn(run_id, "stitch", f"timeline entry {seq}: shot_idx {shot_idx} out of range (0..{n_shots-1}), skipping")
                    continue
                shot = state["shots"][shot_idx]
                variants = shot.get("variants") or []
                if variant_idx < 0 or variant_idx >= len(variants):
                    logger.warn(run_id, "stitch", f"timeline entry {seq}: variant_idx {variant_idx} out of range for shot {shot_idx}, skipping")
                    continue
                v = variants[variant_idx]
                if v.get("status") != "ready" or not v.get("path"):
                    continue
                src = run_dir / v["path"]
                if not src.exists():
                    continue
                slice_in = entry.get("slice_in")
                slice_out = entry.get("slice_out")
                if slice_in is None or slice_out is None:
                    logger.warn(run_id, "stitch", f"timeline entry {seq}: missing slice_in/slice_out, skipping")
                    continue
                slice_path = slices_dir / f"slice_{seq:03d}.mp4"
                try:
                    await video.trim_video(
                        src, slice_path,
                        start=float(slice_in),
                        end=float(slice_out),
                    )
                except Exception as trim_err:
                    logger.warn(run_id, "stitch", f"timeline entry {seq}: trim failed: {str(trim_err)[:200]}")
                    continue
                clip_paths.append(slice_path)
            logger.info(run_id, "stitch", f"timeline mode: stitched {len(clip_paths)} slices matching source rhythm")
        elif apply_plan:
            trims_dir = run_dir / "trims"
            trims_dir.mkdir(exist_ok=True)
            plan_by_idx = {p["idx"]: p for p in cut_plan.get("shots", [])}
            for s in ready:
                src = run_dir / s["path"]
                p = plan_by_idx.get(s["idx"])
                if not p:
                    clip_paths.append(src)
                    continue
                trim_path = trims_dir / f"shot_{s['idx']+1:02d}.mp4"
                shot_dur = state.get("params", {}).get("shot_duration", 5)
                await video.trim_video(
                    src, trim_path,
                    start=float(p.get("cut_in", 0.0)),
                    end=float(p.get("cut_out", shot_dur)),
                )
                clip_paths.append(trim_path)
        else:
            clip_paths = [run_dir / s["path"] for s in ready]

        if not clip_paths:
            raise TrailerNotReady("no clips to stitch — timeline may be empty")

        # Append title card if attached
        title_card = state.get("title_card") or {}
        if title_card.get("path"):
            title_dir = run_dir / "title"
            title_clip = title_dir / "title_clip.mp4"
            try:
                if title_card.get("animated_path"):
                    # Use the animated Seedance version directly
                    title_clip = run_dir / title_card["animated_path"]
                else:
                    # Hold the still for hold_seconds
                    await video.still_to_clip(
                        run_dir / title_card["path"],
                        title_clip,
                        duration=float(title_card.get("hold_seconds") or 2.5),
                    )
                clip_paths.append(title_clip)
                logger.info(run_id, "stitch", f"appended title card ({title_clip.name})")
            except Exception as e:
                logger.warn(run_id, "stitch", f"title card append failed: {e}")

        if crossfade:
            await video.concat_with_crossfade(clip_paths, final_path)
        else:
            await video.concat_videos(clip_paths, final_path)
    except Exception as e:
        state = get_state(run_id)
        state["status"] = "failed"
        state["error"] = str(e)[:400]
        _save_state(run_id, state)
        logger.error(run_id, "stitch", f"✗ stitch failed: {e}")
        raise

    # Full audio mix: music bed + VO lines (with auto-ducking when VO is active)
    music_meta = state.get("music") or {}
    audio_meta = state.get("audio") or {}
    vo_meta = audio_meta.get("vo") or {}
    music_path = None
    if music_meta.get("path"):
        candidate = run_dir / music_meta["path"]
        if candidate.exists():
            music_path = candidate
    vo_lines = []
    if vo_meta.get("status") == "ready":
        script_lines = (vo_meta.get("script") or {}).get("lines") or []
        audio_paths = vo_meta.get("lines_audio") or []
        for i, rel in enumerate(audio_paths):
            if not rel: continue
            line_path = run_dir / rel
            if not line_path.exists(): continue
            start = (script_lines[i].get("suggested_start_s") if i < len(script_lines) else 0) or 0
            vo_lines.append({"path": line_path, "start_s": start})

    # Apply a named color look BEFORE audio mux (so grade is on the final pixels)
    look_id = state.get("params", {}).get("look") or "none"
    if look_id and look_id != "none":
        try:
            import looks as looks_mod
            flt = looks_mod.get_filter(look_id)
            if flt:
                logger.info(run_id, "stitch", f"applying look: {look_id}")
                graded = run_dir / "trailer_graded.mp4"
                await video.apply_look(final_path, graded, flt)
                graded.replace(final_path)
                logger.success(run_id, "stitch", f"✓ look applied ({look_id})")
        except Exception as e:
            logger.warn(run_id, "stitch", f"look application failed (keeping ungraded): {e}")

    if music_path or vo_lines:
        parts = []
        if music_path: parts.append("music")
        if vo_lines: parts.append(f"{len(vo_lines)} VO line(s)")
        logger.info(run_id, "stitch", f"mixing audio ({' + '.join(parts)})…")
        mixed = run_dir / "trailer_with_audio.mp4"
        try:
            await video.mix_music_and_vo(
                final_path, mixed, music_path=music_path, vo_lines=vo_lines,
            )
            mixed.replace(final_path)
            logger.success(run_id, "stitch", "✓ audio mixed")
        except Exception as e:
            logger.warn(run_id, "stitch", f"audio mix failed (keeping silent version): {e}")

    state = get_state(run_id)
    state["final"] = "trailer.mp4"
    state["status"] = "done"
    state["params"]["crossfade"] = crossfade
    state["params"]["applied_cut_plan"] = apply_plan
    _save_state(run_id, state)
    try:
        size_kb = final_path.stat().st_size // 1024
    except OSError:
        size_kb = "?"
    logger.success(run_id, "stitch", f"✓ trailer.mp4 ready ({size_kb} KB)")
    return final_path


async def _stitch_cleanup_on_failure(run_id: str, run_dir: Path) -> None:
    """Unlink any partial mp4 artifacts left behind by a crashed stitch so the
    next attempt isn't fed garbage by ffmpeg's concat demuxer."""
    for partial in (
        run_dir / "trailer.mp4",
        run_dir / "trailer_graded.mp4",
        run_dir / "trailer_with_audio.mp4",
    ):
        try:
            partial.unlink(missing_ok=True)
        except Exception as e:
            logger.warn(run_id, "stitch", f"cleanup of {partial.name} failed: {e}")


# Wrap the public run_stitch so callers always get partial-file cleanup on
# failure without adding a top-level try/finally inside the big function body.
_run_stitch_inner = run_stitch


async def run_stitch(run_id: str, *, crossfade: bool = False, use_cut_plan: bool = True) -> Path:  # noqa: F811
    run_dir = _run_dir(run_id)
    try:
        return await _run_stitch_inner(run_id, crossfade=crossfade, use_cut_plan=use_cut_plan)
    except BaseException:
        # BaseException catches CancelledError too — a killed task should still
        # leave the disk clean.
        await _stitch_cleanup_on_failure(run_id, run_dir)
        raise


# ─── status roll-up ───────────────────────────────────────────────────────

def _roll_up_status(state: dict):
    """Derive top-level status from per-item statuses. Mutates `state` in-place."""
    # Preserve transient pipeline states that shouldn't be overwritten by roll-up
    current = state.get("status", "")
    if current in ("ripping", "translating", "stitching", "failed"):
        return

    kfs = state.get("keyframes") or []
    shots = state.get("shots") or []
    final = state.get("final")

    if final:
        state["status"] = "done"
        return

    if shots:
        shot_statuses = [s["status"] for s in shots]
        if all(s == "ready" for s in shot_statuses):
            state["status"] = "shots_ready"
            return
        if any(s == "generating" for s in shot_statuses):
            state["status"] = "shots_partial"
            return
        if any(s == "ready" for s in shot_statuses):
            state["status"] = "shots_partial"
            return

    if kfs:
        kf_statuses = [k["status"] for k in kfs]
        if all(k == "ready" for k in kf_statuses):
            state["status"] = "keyframes_ready"
            return
        if any(k == "generating" for k in kf_statuses):
            state["status"] = "keyframes_partial"
            return
        if any(k == "ready" for k in kf_statuses):
            state["status"] = "keyframes_partial"
            return

    if state.get("story"):
        state["status"] = "storyboard_ready"
    else:
        state["status"] = "new"


# ─── High-level CLI helper ────────────────────────────────────────────────

async def make_trailer(
    *,
    concept: str,
    num_shots: int = 6,
    shot_duration: int = 5,
    ratio: str = "16:9",
    style_intent: str = "",
    reference_images: Optional[list[Path]] = None,
    title: str = "",
    crossfade: bool = False,
) -> Path:
    """One-shot pipeline for the CLI. No approval gates, full auto."""
    ref_files = []
    for p in reference_images or []:
        if not p.exists():
            print(f"[warning] reference image not found, skipping: {p}", file=sys.stderr)
            continue
        ref_files.append((p.name, p.read_bytes()))

    run_id = create_run(
        concept=concept, num_shots=num_shots, shot_duration=shot_duration,
        ratio=ratio, style_intent=style_intent, title=title, crossfade=crossfade,
        reference_files=ref_files,
    )
    print(f"→ run: {run_id}")

    print("→ storyboard…")
    story = await run_storyboard(run_id)
    print(f"  {story['title']} — {story['logline']}")

    print(f"→ keyframes ({len(story['shots'])} shots, sequential for identity chain)…")
    await run_all_keyframes(run_id)

    num_keys = max(1, len(seedance.KEYS))
    print(f"→ shots (concurrency {num_keys})…")
    await run_all_shots(run_id)

    st = get_state(run_id)
    failed = [s for s in st["shots"] if s["status"] != "ready"]
    if failed:
        idxs = ", ".join(str(s["idx"] + 1) for s in failed)
        raise TrailerError(f"shots failed: {idxs}. Inspect {run_id}/state.json")

    print("→ stitch…")
    final = await run_stitch(run_id, crossfade=crossfade)
    print(f"\n✓ trailer: {final}")
    return final


# ─── Rip-o-matic ──────────────────────────────────────────────────────────

# Rip-o-matic phase lives in rip.py. Re-exported here.
from rip import create_rip_run, preview_rip_segments


async def resume_trailer(output_dir: Path, *, crossfade: Optional[bool] = None) -> Path:
    run_id = output_dir.name
    state = get_state(run_id)
    use_xf = crossfade if crossfade is not None else state["params"].get("crossfade", False)

    if not state.get("story"):
        await run_storyboard(run_id)

    await run_all_keyframes(run_id)
    await run_all_shots(run_id)

    st = get_state(run_id)
    if any(s["status"] != "ready" for s in st["shots"]):
        raise TrailerError(f"shots incomplete — check {run_id}/state.json")

    return await run_stitch(run_id, crossfade=use_xf)
