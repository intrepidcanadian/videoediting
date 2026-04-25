"""Per-run cost tracking.

Each API call appends a JSON line to outputs/<run>/costs.jsonl:
  {"ts": "...", "provider": "anthropic", "model": "claude-sonnet-4-6",
   "phase": "storyboard", "input_tokens": 1200, "output_tokens": 300,
   "cached_tokens": 0, "cost_usd": 0.0048}

UI fetches /api/runs/<id>/costs to show a running total.

Pricing (April 2026, per 1M tokens unless noted):
  claude-sonnet-4-6:  $3 in / $15 out / $0.30 cached
  claude-opus-4-7:    $15 in / $75 out / $1.50 cached
  gemini-2.5-flash-image:       $0.039 per image
  gemini-3.1-flash-image-preview: $0.04 per image
  seedance 2.0 / dreamina:      ~$0.40 per shot (1:1, 5s, 720p)
These are estimates and may drift; update MODEL_PRICES as needed.
"""

import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

_warned_models: set[str] = set()

OUTPUT_ROOT = Path(__file__).parent / "outputs"
_lock = threading.Lock()

# Per 1M tokens, USD. Cached input ~90% discount.
TEXT_PRICES = {
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "cached_in": 0.30},
    "claude-opus-4-7": {"in": 15.0, "out": 75.0, "cached_in": 1.50},
    "claude-haiku-4-5-20251001": {"in": 0.80, "out": 4.0, "cached_in": 0.08},
}

# Per-image flat rate (no token pricing for image outputs)
IMAGE_PRICES = {
    "gemini-2.5-flash-image": 0.039,
    "gemini-3.1-flash-image-preview": 0.04,
    "gemini-3-pro-image-preview": 0.12,
}

# Per-shot flat rate (1:1 5s 720p baseline)
VIDEO_PRICES = {
    "dreamina-seedance-2-0-260128": 0.40,
}


def _log_path(run_id: str) -> Path:
    return OUTPUT_ROOT / run_id / "costs.jsonl"


def _write(run_id: str, entry: dict) -> None:
    p = _log_path(run_id)
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def log_text(
    run_id: str,
    *,
    model: str,
    phase: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> dict:
    """Log a text-LLM call (Claude / similar). Returns entry."""
    prices = TEXT_PRICES.get(model)
    if not prices:
        prices = {"in": 3.0, "out": 15.0, "cached_in": 0.30}
        if model not in _warned_models:
            _warned_models.add(model)
            print(f"[costs] unknown text model '{model}', using default pricing", file=sys.stderr)
    uncached_in = max(0, input_tokens - cached_tokens)
    cost = (
        uncached_in * prices["in"]
        + cached_tokens * prices["cached_in"]
        + output_tokens * prices["out"]
    ) / 1_000_000
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "provider": "anthropic" if "claude" in model.lower() else "text",
        "model": model,
        "phase": phase,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cached_tokens": int(cached_tokens),
        "cost_usd": round(cost, 6),
    }
    _write(run_id, entry)
    return entry


def log_image(run_id: str, *, model: str, phase: str, count: int = 1) -> dict:
    unit = IMAGE_PRICES.get(model)
    if unit is None:
        unit = 0.04
        if model not in _warned_models:
            _warned_models.add(model)
            print(f"[costs] unknown image model '{model}', using default pricing", file=sys.stderr)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "provider": "gemini",
        "model": model,
        "phase": phase,
        "count": int(count),
        "cost_usd": round(unit * count, 6),
    }
    _write(run_id, entry)
    return entry


def log_video(run_id: str, *, model: str, phase: str, count: int = 1) -> dict:
    unit = VIDEO_PRICES.get(model)
    if unit is None:
        unit = 0.40
        if model not in _warned_models:
            _warned_models.add(model)
            print(f"[costs] unknown video model '{model}', using default pricing", file=sys.stderr)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "provider": "byteplus-ark",
        "model": model,
        "phase": phase,
        "count": int(count),
        "cost_usd": round(unit * count, 6),
    }
    _write(run_id, entry)
    return entry


def tail(run_id: str) -> list[dict]:
    p = _log_path(run_id)
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception as e:
                print(f"[costs] skipping malformed line: {e}", file=sys.stderr)
                continue
    return out


def summary(run_id: str) -> dict:
    entries = tail(run_id)
    total = sum(e.get("cost_usd", 0) for e in entries)
    by_phase: dict[str, float] = {}
    by_provider: dict[str, float] = {}
    for e in entries:
        by_phase[e.get("phase", "?")] = round(
            by_phase.get(e.get("phase", "?"), 0) + e.get("cost_usd", 0), 6
        )
        by_provider[e.get("provider", "?")] = round(
            by_provider.get(e.get("provider", "?"), 0) + e.get("cost_usd", 0), 6
        )
    return {
        "total_usd": round(total, 4),
        "call_count": len(entries),
        "by_phase": by_phase,
        "by_provider": by_provider,
    }
