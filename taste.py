"""Taste learning — watch the user's choices, feed the pattern back into generation.

Design:
  - Signals are captured as an append-only `taste.jsonl` log at project root.
    Every meaningful choice (picked storyboard option, promoted variant, edited a
    keyframe, applied a look, chose a voice, regenerated a shot) appends one line.
  - `summary()` computes aggregates from the log — counts, frequencies, most recent.
  - `refresh_claude_context()` asks Claude to turn the aggregates into a short
    natural-language "taste profile" that gets prepended to every downstream
    system prompt (storyboard writer, ideator, director). Cached to
    `taste_profile.json` so we don't re-summarize on every call.
  - `get_context_for_claude()` returns the current cached context string. Every
    Claude caller in the project reads this and prepends it to its system prompt.

The effect: after a few runs, Claude starts writing to YOUR taste — dune-style
grading if you keep picking that, preferred camera moves, typical pacing, common
edit patterns you reach for, voices you like.

Global per-install (not per-user auth) — this is a single-user tool.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import textutils
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

ROOT = Path(__file__).parent
SIGNAL_LOG = ROOT / "taste.jsonl"
PROFILE_CACHE = ROOT / "taste_profile.json"

from constants import ANTHROPIC_MODEL

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

_lock = threading.Lock()


# Canonical in textutils.
_now = textutils.now_iso


# ─── Signal recording ────────────────────────────────────────────────────

SIGNAL_TYPES = {
    "storyboard_pick",     # user chose option N from 3
    "variant_pick",        # user promoted a take to primary
    "keyframe_edit",       # user applied a Nano Banana edit
    "keyframe_regen",      # user regen'd a keyframe (different from edit)
    "shot_regen",          # user re-rendered a shot (signals dissatisfaction)
    "look_pick",           # user applied a color grade
    "voice_pick",          # user chose a VO voice
    "rule_toggle",         # user enabled/disabled a prompt rule
    "library_save",        # user saved something to library
    "music_snap",          # user ran music beat-snap
    "sweep_pick",          # user picked a winner from a prompt sweep
}


def record(signal_type: str, **fields) -> None:
    """Append one signal to the log. Silent-fails on IO errors — taste is
    nice-to-have, must never break a render."""
    if signal_type not in SIGNAL_TYPES:
        # Still log, but flag for attention later
        fields["_unknown_type"] = True
    entry = {"ts": _now(), "type": signal_type, **fields}
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock:
            with SIGNAL_LOG.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        print(f"[taste] signal recording failed: {e}", file=sys.stderr)


def _load_signals(limit: int = 2000) -> list[dict]:
    """Read the signal log. Cap at `limit` most-recent entries for summary."""
    if not SIGNAL_LOG.exists():
        return []
    try:
        with _lock:
            with SIGNAL_LOG.open("r", encoding="utf-8") as f:
                raw = deque(f, maxlen=limit)
    except Exception as e:
        print(f"[taste] signal log read failed: {e}", file=sys.stderr)
        return []
    out = []
    for line in raw:
        line = line.strip()
        if not line: continue
        try:
            out.append(json.loads(line))
        except Exception: continue
    return out


# ─── Aggregates ──────────────────────────────────────────────────────────

def summary() -> dict:
    """Compute a structured view of current taste signals."""
    sigs = _load_signals()
    by_type = defaultdict(list)
    for s in sigs:
        by_type[s.get("type")].append(s)

    storyboard_picks = Counter(s.get("option_idx") for s in by_type["storyboard_pick"] if s.get("option_idx") is not None)
    variant_picks = Counter(s.get("variant_idx") for s in by_type["variant_pick"] if s.get("variant_idx") is not None)
    look_picks = Counter(s.get("look") for s in by_type["look_pick"] if s.get("look") and s.get("look") != "none")
    voice_picks = Counter(s.get("voice_id") for s in by_type["voice_pick"] if s.get("voice_id"))
    edit_phrases = [s.get("edit_prompt", "")[:120] for s in by_type["keyframe_edit"] if s.get("edit_prompt")]
    regen_count_by_run = Counter(s.get("run_id") for s in by_type["shot_regen"] if s.get("run_id"))
    sweep_winners = [s.get("chosen_prompt", "")[:200] for s in by_type["sweep_pick"] if s.get("chosen_prompt")]

    return {
        "total_signals": len(sigs),
        "storyboard_picks": dict(storyboard_picks),
        "variant_picks": dict(variant_picks),
        "look_picks": dict(look_picks.most_common(5)),
        "voice_picks": dict(voice_picks.most_common(5)),
        "common_edit_phrases": edit_phrases[-15:],
        "avg_regens_per_run": round(sum(regen_count_by_run.values()) / max(1, len(regen_count_by_run)), 2),
        "recent_sweep_winners": sweep_winners[-10:],
        "last_signal_at": sigs[-1]["ts"] if sigs else None,
    }


# ─── Claude context (the actual payload other modules read) ──────────────

_TASTE_SYSTEM = """You distill a user's creative preferences from their editing-session signals into a compact natural-language "taste profile" that gets prepended to system prompts for future generations.

Your job: read the aggregates + examples, write 2-3 short paragraphs + 3-6 DO/DON'T bullets that capture the user's TASTE (not their project — their style). Examples:
  - "Prefers slower pacing with longer holds on character beats; avoids fast whip pans."
  - "DO: dramatic side-lighting, muted teal grades, handheld energy"
  - "DON'T: generic cinematic tokens, hyperrealistic, overuse of lens flares"

If there's not enough signal yet to infer anything specific, say so plainly — an accurate "no strong signal yet, default to neutral" is better than fabricated preferences. Never invent specifics not supported by the data.

Output format (plain text, no JSON, no markdown fences):

USER TASTE PROFILE:
<1-2 sentences on overall creative lean>

PATTERNS OBSERVED:
- <bullet>
- <bullet>

DO:
- <actionable DO>
- <actionable DO>

DON'T:
- <actionable DON'T>
- <actionable DON'T>
"""


def refresh_claude_context() -> str:
    """Ask Claude to turn current aggregates into a taste profile string.
    Cached to taste_profile.json. Cheap — ~$0.01 per refresh."""
    if not ANTHROPIC_API_KEY:
        return ""
    sigs = summary()
    if sigs["total_signals"] < 5:
        # Not enough data — skip the Claude call, leave profile empty
        context = ""
        _save_profile({"context": context, "updated_at": _now(), "based_on_signals": sigs["total_signals"]})
        return context

    from anthropic import Anthropic
    import httpx as _httpx

    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=_httpx.Timeout(60.0, connect=30.0))
    try:
        user_msg = f"""# SIGNAL AGGREGATES
{json.dumps(sigs, indent=2)}

# TASK
Distill this into a taste profile as described in the system prompt. Keep it tight — this gets prepended to every system prompt we call Claude with, so brevity is critical."""

        try:
            resp = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=700,
                system=[{"type": "text", "text": _TASTE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_msg}],
            )
            context = textutils.resp_text(resp.content).strip()
        except Exception as e:
            print(f"[taste] Claude taste refresh failed: {e}", file=sys.stderr)
            context = ""

        _save_profile({"context": context, "updated_at": _now(), "based_on_signals": sigs["total_signals"]})

        # Log cost
        try:
            import costs
            if context:
                u = resp.usage
                costs.log_text("_taste", model=ANTHROPIC_MODEL, phase="taste",
                    input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                    cached_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
        except Exception as e: print(f"[costs] taste cost log failed: {e}", file=sys.stderr)

        return context
    finally:
        try: client.close()
        except Exception: pass


def _save_profile(data: dict) -> None:
    try:
        with _lock:
            PROFILE_CACHE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[taste] profile save failed: {e}", file=sys.stderr)


def _load_profile() -> dict:
    if not PROFILE_CACHE.exists():
        return {"context": "", "updated_at": None, "based_on_signals": 0}
    try:
        with _lock:
            return json.loads(PROFILE_CACHE.read_text())
    except Exception:
        return {"context": "", "updated_at": None, "based_on_signals": 0}


def get_context_for_claude() -> str:
    """Return the cached taste profile string. Callers prepend this to their
    system prompts. Empty string means 'no strong signal yet'."""
    profile = _load_profile()
    ctx = profile.get("context", "") or ""
    return ctx


def get_profile() -> dict:
    return {**_load_profile(), "current_summary": summary()}


def reset() -> None:
    with _lock:
        SIGNAL_LOG.unlink(missing_ok=True)
        PROFILE_CACHE.unlink(missing_ok=True)


# ─── System-prompt helper ────────────────────────────────────────────────

def wrap_system_prompt(base: str) -> list[dict]:
    """Return a [cache_control-tagged system prompt list] that prepends the taste
    profile (if any) to `base`. Use this in every Claude caller:

        system=taste.wrap_system_prompt(MY_SYSTEM_PROMPT)

    The taste context goes in its own cache-control block so changes to it don't
    invalidate the base prompt's cache.
    """
    blocks = []
    ctx = get_context_for_claude()
    if ctx:
        blocks.append({
            "type": "text",
            "text": f"# USER TASTE CONTEXT (applies to all generations for this user)\n{ctx}\n\n---\n",
            "cache_control": {"type": "ephemeral"},
        })
    blocks.append({"type": "text", "text": base, "cache_control": {"type": "ephemeral"}})
    return blocks
