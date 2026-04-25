"""outputs/ directory retention + disk hygiene.

Runs accumulate on disk: each one can be 50-200 MB (keyframes, shot variants,
slices, title card, trailer.mp4). Without cleanup, a heavy user fills their
disk in a few hundred runs.

Policies:
  - `list_candidates(older_than_days)` — find runs modified before cutoff.
  - `delete_run(run_id)` — remove a run directory and drop it from any cache.
  - `disk_usage()` — total bytes under OUTPUT_ROOT.

No scheduled cleanup runs automatically — retention is opt-in via the
`/api/retention/cleanup` endpoint so a user can never lose work they wanted.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import pipeline


def disk_usage(root: Optional[Path] = None) -> int:
    """Total bytes in `root` (defaults to OUTPUT_ROOT). Silently skips files
    that disappear mid-scan — they'll be accounted for in the next call."""
    base = root or pipeline.OUTPUT_ROOT
    if not base.exists():
        return 0
    total = 0
    for p in base.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def list_candidates(
    older_than_days: float,
    *,
    include_active: bool = False,
) -> list[dict]:
    """Return runs whose state.json hasn't been modified in `older_than_days`.

    `include_active` defaults to False: a run with status "storyboard_generating",
    "keyframes_generating", "shots_generating", "stitching", etc. is kept even
    if old, because the user probably forgot to kill it but hasn't abandoned it.
    """
    cutoff = time.time() - (older_than_days * 86400)
    base = pipeline.OUTPUT_ROOT
    if not base.exists():
        return []
    candidates: list[dict] = []
    for d in base.iterdir():
        if not d.is_dir():
            continue
        state_p = d / "state.json"
        if not state_p.exists():
            continue
        try:
            mtime = state_p.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        # Read status; skip active/in-flight work unless explicitly asked.
        if not include_active:
            try:
                state = pipeline.get_state(d.name)
                status = state.get("status", "")
                if status and status.endswith(("_generating", "stitching", "ripping", "translating", "synthesizing")):
                    continue
            except Exception:
                # Unreadable state — probably corrupt. Safe to delete.
                pass
        size = 0
        try:
            for p in d.rglob("*"):
                if p.is_file():
                    size += p.stat().st_size
        except Exception:
            pass
        candidates.append({
            "run_id": d.name,
            "last_modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
            "age_days": round((time.time() - mtime) / 86400, 1),
            "size_bytes": size,
        })
    candidates.sort(key=lambda c: c["last_modified"])
    return candidates


def delete_run(run_id: str) -> int:
    """Remove a run directory. Returns freed bytes (best-effort estimate)."""
    pipeline.validate_run_id(run_id)
    d = pipeline.OUTPUT_ROOT / run_id
    if not d.exists():
        raise FileNotFoundError(f"run not found: {run_id}")
    freed = 0
    try:
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    freed += p.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    shutil.rmtree(d)
    # Invalidate the cached list_runs result so the UI sees the deletion.
    pipeline._list_runs_cache["data"] = None
    return freed


def cleanup(older_than_days: float, *, dry_run: bool = True) -> dict:
    """Delete all runs older than `older_than_days`. Returns a summary with
    `deleted`, `skipped`, `freed_bytes`. When `dry_run` (the default), returns
    the list of candidates without removing them."""
    candidates = list_candidates(older_than_days)
    if dry_run:
        return {
            "dry_run": True,
            "would_delete": candidates,
            "total_bytes": sum(c["size_bytes"] for c in candidates),
        }
    deleted: list[str] = []
    skipped: list[dict] = []
    freed = 0
    for c in candidates:
        try:
            freed += delete_run(c["run_id"])
            deleted.append(c["run_id"])
        except Exception as e:
            skipped.append({"run_id": c["run_id"], "reason": str(e)})
            print(f"[retention] could not delete {c['run_id']}: {e}", file=sys.stderr)
    return {"dry_run": False, "deleted": deleted, "skipped": skipped, "freed_bytes": freed}
