"""Cross-run asset library.

Lives at `library/` at the project root. Four kinds:
  - characters  — portraits + descriptions; reusable across runs as identity refs
  - locations   — environmental refs for rip-o-matic or keyframe anchoring
  - music       — analyzed tracks (BPM + beats cached) for instant retime
  - looks       — user-saved LUTs (future) or curated named grade copies

Structure:
  library/
    index.json                      # cached listing for fast UI
    characters/
      elena/
        meta.json                   # {name, description, tags, files}
        portrait_01.jpg
        portrait_02.jpg
    locations/
      stadium/
        meta.json
        wide.jpg
    music/
      tension_build/
        meta.json                   # name, analysis (bpm/beats/duration), tags
        track.mp3

Items are referenced by slug (url-safe filename). Runs can save into the library
(promote_from_run) or pull from it (inject_into_run).
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path
from typing import Optional

import textutils

ROOT = Path(__file__).parent
LIBRARY_ROOT = ROOT / "library"
KINDS = ("characters", "locations", "props", "music", "looks")

_lock = threading.Lock()


# Thin wrappers so call-sites inside this module can stay terse.
# Canonical implementations live in textutils.
_now = textutils.now_iso


def _slug(s: str, max_len: int = 48) -> str:
    return textutils.slug(s, max_len=max_len)


def _kind_dir(kind: str) -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown library kind: {kind} (valid: {KINDS})")
    d = LIBRARY_ROOT / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _item_dir(kind: str, slug: str) -> Path:
    if not slug or "/" in slug or "\\" in slug or slug in (".", ".."):
        raise ValueError(f"invalid library slug: {slug!r}")
    return _kind_dir(kind) / slug


def _meta_path(kind: str, slug: str) -> Path:
    return _item_dir(kind, slug) / "meta.json"


# ─── Read ────────────────────────────────────────────────────────────────

def list_items(kind: Optional[str] = None) -> dict[str, list[dict]]:
    """Return {kind: [items]}. If kind is provided, only that kind."""
    out: dict[str, list[dict]] = {}
    kinds = [kind] if kind else list(KINDS)
    for k in kinds:
        items: list[dict] = []
        d = _kind_dir(k)
        for item_dir in sorted(d.iterdir()) if d.exists() else []:
            if not item_dir.is_dir(): continue
            meta_p = item_dir / "meta.json"
            if not meta_p.exists(): continue
            try:
                meta = json.loads(meta_p.read_text())
            except Exception as e:
                print(f"[library] skipping {item_dir.name}: corrupted meta.json: {e}", file=sys.stderr)
                continue
            # Expose file paths relative to library root for serving
            files = [f"{k}/{item_dir.name}/{p.name}" for p in item_dir.iterdir() if p.is_file() and p.name != "meta.json"]
            items.append({**meta, "slug": item_dir.name, "kind": k, "files": files})
        out[k] = items
    return out


def get_item(kind: str, slug: str) -> dict:
    meta_p = _meta_path(kind, slug)
    if not meta_p.exists():
        raise FileNotFoundError(f"library item not found: {kind}/{slug}")
    try:
        meta = json.loads(meta_p.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RuntimeError(f"corrupted meta.json for {kind}/{slug}: {e}")
    item_dir = _item_dir(kind, slug)
    files = [f"{kind}/{slug}/{p.name}" for p in item_dir.iterdir() if p.is_file() and p.name != "meta.json"]
    return {**meta, "slug": slug, "kind": kind, "files": files}


def get_absolute_file_paths(kind: str, slug: str) -> list[Path]:
    """Return absolute paths to this item's files (for copying into a run)."""
    item_dir = _item_dir(kind, slug)
    if not item_dir.exists():
        raise FileNotFoundError(f"library item not found: {kind}/{slug}")
    return [p for p in item_dir.iterdir() if p.is_file() and p.name != "meta.json"]


# ─── Write ───────────────────────────────────────────────────────────────

def save_item(
    kind: str,
    *,
    name: str,
    description: str = "",
    tags: Optional[list[str]] = None,
    files: Optional[list[tuple[str, bytes]]] = None,
    extra: Optional[dict] = None,
    slug: Optional[str] = None,
) -> dict:
    """Create or update a library item. Returns the stored meta."""
    slug = slug or _slug(name)
    item_dir = _item_dir(kind, slug)
    item_dir.mkdir(parents=True, exist_ok=True)

    meta_p = _meta_path(kind, slug)
    existing = {}
    if meta_p.exists():
        try: existing = json.loads(meta_p.read_text())
        except Exception as e:
            print(f"[library] ignoring corrupted meta for {slug}: {e}", file=sys.stderr)
            existing = {}

    meta = {
        **existing,
        "name": name,
        "description": description,
        "tags": list(tags or existing.get("tags") or []),
        "updated_at": _now(),
    }
    if "created_at" not in meta:
        meta["created_at"] = _now()
    if extra:
        meta.update(extra)

    # Write files (append if slugs collide)
    for fname, data in files or []:
        # Keep original extension but slugify the name
        p = Path(fname)
        safe = _slug(p.stem) + p.suffix.lower()
        dst = item_dir / safe
        # Don't overwrite existing files with same name unless identical
        if dst.exists() and dst.read_bytes() == data:
            continue
        # Uniquify
        i = 1
        while dst.exists() and i < 100:
            dst = item_dir / f"{p.stem}_{i}{p.suffix.lower()}"
            i += 1
        if dst.exists():
            import hashlib
            h = hashlib.sha256(data).hexdigest()[:8]
            dst = item_dir / f"{p.stem}_{h}{p.suffix.lower()}"
        if dst.exists():
            continue
        dst.write_bytes(data)

    with _lock:
        tmp = meta_p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2))
        tmp.replace(meta_p)

    return {**meta, "slug": slug, "kind": kind}


def delete_item(kind: str, slug: str) -> None:
    d = _item_dir(kind, slug)
    if not d.exists():
        raise FileNotFoundError(f"library item not found: {kind}/{slug}")
    shutil.rmtree(d)


# ─── Promote / inject (between runs and library) ─────────────────────────

def promote_from_run(
    *,
    run_root: Path,
    kind: str,
    name: str,
    file_rel_paths: list[str],
    description: str = "",
    tags: Optional[list[str]] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Copy files from a run into the library as a new item.
    `file_rel_paths` are paths relative to the run's root (e.g. `references/ref_01.jpg`).
    """
    files: list[tuple[str, bytes]] = []
    for rel in file_rel_paths:
        src = (run_root / rel).resolve()
        if not src.exists() or src.is_symlink() or not src.is_relative_to(run_root.resolve()):
            continue
        files.append((src.name, src.read_bytes()))
    return save_item(
        kind,
        name=name,
        description=description,
        tags=tags,
        files=files,
        extra=extra,
    )


# Directories inside a run that a library item is allowed to be injected into.
# Restricting this prevents a malicious target_dir from writing anywhere under the run,
# and keeps injected assets in predictable locations the pipeline already knows about.
_ALLOWED_INJECT_TARGETS = {"references", "assets", "audio", "music", "looks"}


def inject_into_run(
    *,
    run_root: Path,
    kind: str,
    slug: str,
    target_dir: str = "references",
) -> list[str]:
    """Copy all files from a library item into a run directory. Returns the list
    of paths (relative to run_root) where files were copied."""
    # Validate target_dir — reject traversal, absolute paths, and anything outside allowlist.
    if not isinstance(target_dir, str) or not target_dir:
        raise ValueError("target_dir must be a non-empty string")
    if target_dir not in _ALLOWED_INJECT_TARGETS:
        raise ValueError(
            f"target_dir must be one of {sorted(_ALLOWED_INJECT_TARGETS)}, got {target_dir!r}"
        )
    srcs = get_absolute_file_paths(kind, slug)
    run_root_resolved = run_root.resolve()
    target = (run_root / target_dir).resolve()
    # Defense in depth: even if the allowlist check passed, confirm containment.
    if not target.is_relative_to(run_root_resolved):
        raise ValueError(f"target escapes run root: {target_dir!r}")
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for src in srcs:
        # Uniquify within target
        dst = target / f"{kind}_{slug}_{src.name}"
        i = 1
        while dst.exists():
            dst = target / f"{kind}_{slug}_{src.stem}_{i}{src.suffix}"
            i += 1
        dst.write_bytes(src.read_bytes())
        copied.append(str(dst.relative_to(run_root_resolved)))
    return copied
