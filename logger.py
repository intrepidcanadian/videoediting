"""Per-run structured activity log (JSONL).

Why its own module: pipeline / storyboard / review / assets all log to it and must
not import from each other. This module has no intra-project deps, so everyone can
import it without circulars.

Write format (one JSON object per line):
  {"ts": "2026-04-22T15:30:45.123", "level": "info", "phase": "keyframes",
   "msg": "generating keyframe 3/6", "shot_idx": 2}

Tail format: identical — returned as a list by tail().

Thread safety: writes are protected by a process-wide lock. Reads are not protected
— we accept the risk of reading mid-write (incomplete last line gets skipped). In
practice the log endpoint polls every ~1s and lines are small.
"""

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

OUTPUT_ROOT = Path(__file__).parent / "outputs"
_lock = threading.Lock()

# Rotate once a run's log.jsonl passes this threshold. The rotated file keeps
# the last N kilobytes of history in `log.jsonl.1`; anything older is dropped.
# Override via LOG_MAX_BYTES if a user wants more (or less) history.
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))   # 5 MB
LOG_ROTATE_KEEP_TAIL = int(os.getenv("LOG_ROTATE_KEEP_TAIL", str(512 * 1024)))  # 512 KB


def _log_path(run_id: str) -> Path:
    return OUTPUT_ROOT / run_id / "log.jsonl"


def _maybe_rotate(p: Path) -> None:
    """If `p` exceeds LOG_MAX_BYTES, move its last LOG_ROTATE_KEEP_TAIL bytes
    to `log.jsonl.1` and truncate `p`. Must be called with `_lock` already held
    so we don't race with another writer."""
    try:
        size = p.stat().st_size
    except OSError:
        return
    if size <= LOG_MAX_BYTES:
        return
    try:
        # Read the tail slice. Seek to (size - keep) and find the first newline
        # so we don't start mid-line.
        keep = min(LOG_ROTATE_KEEP_TAIL, size)
        with p.open("rb") as f:
            f.seek(max(0, size - keep))
            tail = f.read()
        # Drop leading partial line if present.
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1:]
        rotated = p.with_suffix(p.suffix + ".1")
        rotated.write_bytes(tail)
        # Truncate the live file. A new writer will append a fresh set of rows.
        p.write_bytes(b"")
    except Exception as e:
        print(f"[logger] rotation failed for {p}: {e}", file=sys.stderr)


def log(
    run_id: str,
    level: str,
    phase: str,
    msg: str,
    **extra,
) -> dict:
    """Append a log entry for a run. `level` ∈ {info, success, warn, error}."""
    entry = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "level": level,
        "phase": phase,
        "msg": msg,
    }
    if extra:
        entry.update(extra)
    p = _log_path(run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with _lock:
        _maybe_rotate(p)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    # Also mirror to stdout so /tmp/trailer_server.log stays useful for debugging
    print(f"[{run_id}] [{phase}] {msg}")
    return entry


def info(run_id: str, phase: str, msg: str, **extra): return log(run_id, "info", phase, msg, **extra)
def success(run_id: str, phase: str, msg: str, **extra): return log(run_id, "success", phase, msg, **extra)
def warn(run_id: str, phase: str, msg: str, **extra): return log(run_id, "warn", phase, msg, **extra)
def error(run_id: str, phase: str, msg: str, **extra): return log(run_id, "error", phase, msg, **extra)


def tail(run_id: str, *, since: Optional[str] = None, limit: int = 500) -> list[dict]:
    """Return entries strictly after `since` (ISO timestamp). Capped at `limit`
    (most recent entries kept)."""
    p = _log_path(run_id)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        with _lock:
            raw_lines = p.read_text(encoding="utf-8").splitlines()
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if since:
                ts = e.get("ts", "")
                if ts and ts <= since:
                    continue
            out.append(e)
    except Exception:
        return out
    if limit and len(out) > limit:
        out = out[-limit:]
    return out
