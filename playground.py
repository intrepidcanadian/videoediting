"""Seedance playground — one-off clip generation, no storyboard / phase state.

For when you just want to prompt Seedance and get a video back. Kept separate
from the run pipeline so these clips don't show up in the Runs list and don't
share state with any trailer-in-progress.

Storage layout (parallel to outputs/):

    outputs_playground/
      index.jsonl                         # one JSON line per clip, append-only
      <clip_id>/
        clip.mp4                          # the rendered video
        refs/ref_01.png, ref_02.png       # optional reference images
        meta.json                         # {prompt, ratio, duration, …}

A clip's `status` goes through: queued → generating → ready | failed.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import costs
import logger
import nano_banana
import seedance
import textutils

ROOT = Path(__file__).parent
PLAYGROUND_ROOT = ROOT / "outputs_playground"
INDEX_PATH = PLAYGROUND_ROOT / "index.jsonl"

_lock = threading.Lock()

# clip_id shape: clip_<YYYYMMDD_HHMMSS>_<slug> — mirrors run_id so the traversal
# regex guards apply identically.
_CLIP_ID_RE = re.compile(r"^clip_[0-9]{8}_[0-9]{6}_[a-zA-Z0-9_-]{1,40}$")


def _validate_clip_id(clip_id: str) -> str:
    if not isinstance(clip_id, str) or not _CLIP_ID_RE.match(clip_id):
        raise ValueError(f"invalid clip_id: {clip_id!r}")
    return clip_id


def _clip_dir(clip_id: str) -> Path:
    _validate_clip_id(clip_id)
    d = (PLAYGROUND_ROOT / clip_id).resolve()
    if not d.is_relative_to(PLAYGROUND_ROOT.resolve()):
        raise FileNotFoundError(f"clip not found: {clip_id}")
    return d


def _meta_path(clip_id: str) -> Path:
    return _clip_dir(clip_id) / "meta.json"


def _make_clip_id(prompt: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    slug = textutils.slug(prompt[:60], max_len=30) or "clip"
    return f"clip_{ts}_{slug}"


def _save_meta(clip_id: str, meta: dict) -> None:
    """Atomic write with a shadow backup — mirrors _save_state in pipeline.
    The primary is meta.json; a prior version lives at meta.json.bak so we can
    recover if the write is interrupted."""
    d = _clip_dir(clip_id)
    d.mkdir(parents=True, exist_ok=True)
    primary = d / "meta.json"
    tmp = d / "meta.json.tmp"
    backup = d / "meta.json.bak"
    tmp.write_text(json.dumps(meta, indent=2))
    if primary.exists():
        try: primary.replace(backup)
        except Exception as e: print(f"[playground] backup shadow failed for {clip_id}: {e}", file=sys.stderr)
    tmp.replace(primary)


def get_meta(clip_id: str) -> dict:
    p = _meta_path(clip_id)
    if not p.exists():
        raise FileNotFoundError(f"clip not found: {clip_id}")
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        # Try backup
        bak = p.with_suffix(".json.bak")
        if bak.exists():
            try:
                data = json.loads(bak.read_text())
                bak.replace(p)
                return data
            except Exception:
                pass
        raise RuntimeError(f"corrupted meta.json for {clip_id}: {e}")


def list_clips() -> list[dict]:
    """Return all clips, newest first. Skips clips whose meta failed to load."""
    out: list[dict] = []
    if not PLAYGROUND_ROOT.exists():
        return out
    for d in sorted(PLAYGROUND_ROOT.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir() or not _CLIP_ID_RE.match(d.name):
            continue
        try:
            meta = get_meta(d.name)
        except Exception as e:
            print(f"[playground] skipping {d.name}: {e}", file=sys.stderr)
            continue
        out.append(meta)
    return out


def delete_clip(clip_id: str) -> None:
    d = _clip_dir(clip_id)
    if not d.exists():
        raise FileNotFoundError(f"clip not found: {clip_id}")
    shutil.rmtree(d)


async def generate_clip(
    *,
    prompt: str,
    reference_images: Optional[list[tuple[str, bytes]]] = None,
    reference_videos: Optional[list[tuple[str, bytes]]] = None,
    ratio: str = "16:9",
    duration: int = 5,
    quality: str = "standard",
    generate_audio: bool = False,
) -> str:
    """Create a playground clip. Returns the clip_id immediately; the render
    runs as a background task and status is readable via get_meta().

    Reference videos are normalized (trimmed to ≤15s + re-encoded) before the
    Seedance call, which is why normalization happens in `_render` — we don't
    want the HTTP handler to block on ffmpeg."""
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    if duration < 3 or duration > 12:
        raise ValueError("duration must be 3–12 seconds")
    if ratio not in ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"):
        raise ValueError(f"unsupported ratio: {ratio}")
    if quality not in seedance.QUALITY_TIERS:
        raise ValueError(f"quality must be one of {list(seedance.QUALITY_TIERS)}")

    clip_id = _make_clip_id(prompt)
    d = _clip_dir(clip_id)
    d.mkdir(parents=True, exist_ok=True)

    # Persist reference images so we can replay / reference them later.
    ref_paths: list[Path] = []
    ref_rel: list[str] = []
    if reference_images:
        refs_dir = d / "refs"
        refs_dir.mkdir(exist_ok=True)
        for i, (name, data) in enumerate(reference_images[:3]):   # Seedance/Nano Banana cap at small N
            ext = Path(name).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                ext = ".png"
            dst = refs_dir / f"ref_{i+1:02d}{ext}"
            dst.write_bytes(data)
            ref_paths.append(dst)
            ref_rel.append(f"refs/{dst.name}")

    # Persist reference videos. Raw bytes land here; `_render` normalizes
    # (trim + downscale) into `refs/vref_NN_norm.mp4` before calling Seedance.
    vref_raw_paths: list[Path] = []
    vref_rel: list[str] = []
    if reference_videos:
        refs_dir = d / "refs"
        refs_dir.mkdir(exist_ok=True)
        for i, (name, data) in enumerate(reference_videos[:3]):  # Seedance cap: 3 ref videos per task
            ext = Path(name).suffix.lower()
            if ext not in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
                ext = ".mp4"
            dst = refs_dir / f"vref_{i+1:02d}_raw{ext}"
            dst.write_bytes(data)
            vref_raw_paths.append(dst)
            vref_rel.append(f"refs/{dst.name}")

    meta = {
        "clip_id": clip_id,
        "kind": "video",                       # disambiguates from image renders
        "prompt": prompt.strip(),
        "ratio": ratio,
        "duration": duration,
        "quality": quality,
        "generate_audio": bool(generate_audio),
        "references": ref_rel,
        "video_references": vref_rel,
        "video_path": None,
        "status": "queued",
        "error": None,
        "created_at": textutils.now_iso(),
        "updated_at": textutils.now_iso(),
        "cost_usd": None,
    }
    _save_meta(clip_id, meta)

    # Fire the render as an independent task. The caller is the HTTP handler,
    # which wants to return fast so the UI can poll.
    asyncio.create_task(
        _render(clip_id, ref_paths, vref_raw_paths),
        name=f"playground:{clip_id}",
    )
    return clip_id


# ─── Image generation (Nano Banana / Gemini 2.5 Flash Image) ─────────────

async def generate_image(
    *,
    prompt: str,
    reference_images: Optional[list[tuple[str, bytes]]] = None,
) -> str:
    """Create a playground still image via Nano Banana. Returns the clip_id
    immediately; the render runs in the background and the UI polls for it.

    Unlike the video flow, image renders are fast (~5-15s) and take only
    reference images (no video refs, no duration, no audio). The rest of the
    meta schema is shared so the same UI card can display both kinds."""
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")

    clip_id = _make_clip_id(prompt)
    d = _clip_dir(clip_id)
    d.mkdir(parents=True, exist_ok=True)

    # Persist reference images. Nano Banana tolerates up to ~8 refs per call but
    # quality drops past 4; we cap at 4 to match the practical sweet spot.
    ref_paths: list[Path] = []
    ref_rel: list[str] = []
    if reference_images:
        refs_dir = d / "refs"
        refs_dir.mkdir(exist_ok=True)
        for i, (name, data) in enumerate(reference_images[:4]):
            ext = Path(name).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                ext = ".png"
            dst = refs_dir / f"ref_{i+1:02d}{ext}"
            dst.write_bytes(data)
            ref_paths.append(dst)
            ref_rel.append(f"refs/{dst.name}")

    meta = {
        "clip_id": clip_id,
        "kind": "image",                       # marks this as a Nano Banana render
        "prompt": prompt.strip(),
        "references": ref_rel,
        "video_references": [],                # kept for UI symmetry
        "image_path": None,                    # populated on success
        "video_path": None,
        "status": "queued",
        "error": None,
        "created_at": textutils.now_iso(),
        "updated_at": textutils.now_iso(),
        "cost_usd": None,
    }
    _save_meta(clip_id, meta)

    asyncio.create_task(
        _render_image(clip_id, ref_paths),
        name=f"playground-image:{clip_id}",
    )
    return clip_id


async def _render_image(clip_id: str, ref_paths: list[Path]) -> None:
    """Background: call Nano Banana, update meta.status as it progresses.

    Uses the `nano_banana_keyframe` rule target so the same deterministic prompt
    normalization we apply to real trailer keyframes is applied here too —
    keeps playground images predictable and consistent with trailer output."""
    try:
        meta = get_meta(clip_id)
    except Exception as e:
        print(f"[playground] could not load meta for {clip_id}: {e}", file=sys.stderr)
        return

    d = _clip_dir(clip_id)
    output_path = d / "clip.png"
    meta["status"] = "generating"
    meta["updated_at"] = textutils.now_iso()
    _save_meta(clip_id, meta)
    logger.info(clip_id, "playground", f"Nano Banana rendering: '{meta['prompt'][:80]}…'")

    t0 = time.time()
    try:
        await nano_banana.generate_keyframe(
            prompt=meta["prompt"],
            reference_paths=ref_paths,
            output_path=output_path,
            rules_target="nano_banana_keyframe",
            run_id=clip_id,
        )
    except Exception as e:
        elapsed = time.time() - t0
        with _lock:
            meta = get_meta(clip_id)
            meta["status"] = "failed"
            meta["error"] = str(e)[:400]
            meta["updated_at"] = textutils.now_iso()
            meta["elapsed_s"] = round(elapsed, 1)
            _save_meta(clip_id, meta)
        logger.error(clip_id, "playground", f"✗ image render failed after {elapsed:.1f}s: {e}")
        return

    elapsed = time.time() - t0
    try:
        costs.log_image(clip_id, model=nano_banana.NANO_BANANA_MODEL, phase="playground")
    except Exception as e:
        print(f"[playground] image cost log failed for {clip_id}: {e}", file=sys.stderr)

    with _lock:
        meta = get_meta(clip_id)
        meta["status"] = "ready"
        meta["image_path"] = "clip.png"
        meta["updated_at"] = textutils.now_iso()
        meta["elapsed_s"] = round(elapsed, 1)
        try:
            cost_file = costs._log_path(clip_id) if hasattr(costs, "_log_path") else None
            if cost_file and cost_file.exists():
                rows = []
                for l in cost_file.read_text().splitlines():
                    if not l.strip():
                        continue
                    try:
                        rows.append(json.loads(l))
                    except Exception:
                        continue
                if rows:
                    meta["cost_usd"] = round(sum(r.get("cost_usd", 0) for r in rows), 4)
        except Exception:
            pass
        _save_meta(clip_id, meta)
    logger.success(clip_id, "playground", f"✓ image rendered in {elapsed:.0f}s → {output_path.stat().st_size // 1024} KB")


async def _render(clip_id: str, ref_paths: list[Path], vref_raw_paths: list[Path]) -> None:
    """Background: normalize any ref videos, call Seedance, update meta as
    it progresses."""
    try:
        meta = get_meta(clip_id)
    except Exception as e:
        print(f"[playground] could not load meta for {clip_id}: {e}", file=sys.stderr)
        return

    d = _clip_dir(clip_id)
    output_path = d / "clip.mp4"
    meta["status"] = "generating"
    meta["updated_at"] = textutils.now_iso()
    _save_meta(clip_id, meta)
    logger.info(clip_id, "playground", f"Seedance rendering: '{meta['prompt'][:80]}…'")

    # Normalize video refs for Seedance: trim to ≤15s, downscale to 720p,
    # baseline H.264. Failure on a single ref is non-fatal — we just skip it
    # and log a warning, since losing one ref is better than dropping the clip.
    import video as video_mod
    normalized_vrefs: list[Path] = []
    for i, raw in enumerate(vref_raw_paths):
        try:
            norm = d / "refs" / f"vref_{i+1:02d}_norm.mp4"
            await video_mod.normalize_video_ref(raw, norm)
            normalized_vrefs.append(norm)
            logger.info(clip_id, "playground", f"normalized video ref {i+1}: {norm.stat().st_size // 1024} KB")
        except Exception as e:
            logger.warn(clip_id, "playground", f"video ref {i+1} normalization failed (skipping): {e}")

    t0 = time.time()
    try:
        await seedance.render_shot(
            prompt=meta["prompt"],
            reference_images=ref_paths,
            output_path=output_path,
            ratio=meta["ratio"],
            duration=int(meta["duration"]),
            generate_audio=bool(meta.get("generate_audio")),
            run_id=clip_id,
            quality=meta.get("quality") or "standard",
            reference_video_paths=normalized_vrefs or None,
        )
    except Exception as e:
        elapsed = time.time() - t0
        for nv in normalized_vrefs:
            try: nv.unlink(missing_ok=True)
            except Exception: pass
        with _lock:
            meta = get_meta(clip_id)
            meta["status"] = "failed"
            meta["error"] = str(e)[:400]
            meta["updated_at"] = textutils.now_iso()
            meta["elapsed_s"] = round(elapsed, 1)
            _save_meta(clip_id, meta)
        logger.error(clip_id, "playground", f"✗ render failed after {elapsed:.1f}s: {e}")
        return

    elapsed = time.time() - t0
    try:
        # Per-clip cost tracking — log_video reads the Seedance price table.
        costs.log_video(clip_id, model=seedance.resolve_model(meta.get("quality")), phase="playground")
    except Exception as e:
        print(f"[playground] cost log failed for {clip_id}: {e}", file=sys.stderr)

    # Clean up normalized video refs — no longer needed after render
    for nv in normalized_vrefs:
        try:
            nv.unlink(missing_ok=True)
        except Exception:
            pass

    with _lock:
        meta = get_meta(clip_id)
        meta["status"] = "ready"
        meta["video_path"] = "clip.mp4"
        meta["updated_at"] = textutils.now_iso()
        meta["elapsed_s"] = round(elapsed, 1)
        # Attempt to read the clip-specific cost from the jsonl we just wrote.
        try:
            cost_file = costs._log_path(clip_id) if hasattr(costs, "_log_path") else None
            if cost_file and cost_file.exists():
                rows = []
                for l in cost_file.read_text().splitlines():
                    if not l.strip():
                        continue
                    try:
                        rows.append(json.loads(l))
                    except Exception:
                        continue
                if rows:
                    meta["cost_usd"] = round(sum(r.get("cost_usd", 0) for r in rows), 4)
        except Exception:
            pass
        _save_meta(clip_id, meta)
    logger.success(clip_id, "playground", f"✓ rendered in {elapsed:.0f}s → {output_path.stat().st_size // 1024} KB")
