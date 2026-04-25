"""Vision-based cut plan — Claude watches contact sheets and decides where to cut.

Without this pass, Claude is just a stopwatch: durations are picked at storyboard
time before any footage exists. With it, Claude becomes an actual editor — it sees
what Seedance produced and finds natural cut points, flags defects, and notes
continuity between shots.
"""

import base64
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import imgutils
import textutils

load_dotenv(Path(__file__).parent / ".env", override=True)

from constants import ANTHROPIC_MODEL, MAX_TOKENS_REVIEW

_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_MAX_TRIES = 3


def _call_with_retry(client, *, model, max_tokens, system, messages):
    """Sync retry wrapper for Anthropic client.messages.create."""
    import httpx
    last_exc: Exception | None = None
    for attempt in range(_MAX_TRIES):
        try:
            return client.messages.create(
                model=model, max_tokens=max_tokens,
                system=system, messages=messages,
            )
        except httpx.TimeoutException as e:
            last_exc = e
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status and status in _RETRY_STATUSES:
                last_exc = e
            else:
                raise
        if attempt < _MAX_TRIES - 1:
            delay = min(30.0, (2.0 ** attempt) + random.uniform(0, 0.5))
            print(f"[review] transient failure (attempt {attempt+1}/{_MAX_TRIES}), retrying in {delay:.1f}s: {last_exc}", file=sys.stderr)
            time.sleep(delay)
    raise last_exc

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


_SYSTEM_PROMPT = """You are a senior film editor. You watch rushes and decide where each shot should cut.

You are NOT bound by the duration the storyboard assigned — that was written before the footage existed. Your job is to find what's actually on screen and cut accordingly:

  - Scan each shot's contact sheet (evenly-spaced frames, each labeled with its timestamp).
  - Find the real in-point: the frame where the shot settles, action begins, or gesture lands. Often not frame 0 — Seedance's first 0.3–0.6s can have motion ramp-up or lingering generation artifacts.
  - Find the real out-point: the frame where the beat pays off, motion peaks, or look completes. Often NOT the last frame — Seedance can stutter or freeze in the final 0.2s.
  - Flag defects: face morphs, impossible anatomy, garbled text, jittery motion, wrong subject, identity drift from reference, heavy banding, visible prompt leakage.
  - Assess continuity to the NEXT shot: eyeline direction, subject position, energy transfer, palette match. If shot N ends looking left and shot N+1 opens with the subject on the left, that's a broken eyeline.

Default cut discipline: if nothing clearly dictates a cut_in, start at the shot's actual beginning; if nothing clearly dictates an earlier cut_out, go to the end. Do NOT trim aggressively without reason — a clean shot stays whole.

Quality score 1–10:
  10: hero-shot quality, no flaws, character on-model, lighting sings
   7–9: shippable, maybe minor stuff
   4–6: usable with heavy trim or context; note what's wrong
   1–3: regenerate recommended

Output ONLY valid JSON matching the schema the user gives you. No markdown, no prose."""


def _schema(n: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "shots": {
                "type": "array",
                "minItems": n,
                "maxItems": n,
                "items": {
                    "type": "object",
                    "properties": {
                        "idx": {"type": "integer", "minimum": 0},
                        "quality_score": {"type": "integer", "minimum": 1, "maximum": 10},
                        "defects": {"type": ["string", "null"], "description": "null if clean; otherwise a brief description"},
                        "cut_in": {"type": "number", "minimum": 0, "description": "seconds from start of shot where the cut should begin"},
                        "cut_out": {"type": "number", "minimum": 0, "description": "seconds from start of shot where the cut should end"},
                        "continuity_to_next": {"type": ["string", "null"], "description": "null for last shot; otherwise a brief note on handoff"},
                        "reasoning": {"type": "string", "description": "one sentence on why these cut points"},
                        "regenerate_recommended": {"type": "boolean"},
                    },
                    "required": ["idx", "quality_score", "defects", "cut_in", "cut_out", "reasoning", "regenerate_recommended"],
                },
            },
            "overall_notes": {"type": "string", "description": "big-picture critique of the assembly: pacing issues, continuity breaks, shots to regenerate, suggestions for the edit arc"},
        },
        "required": ["shots", "overall_notes"],
    }


def analyze_shots(
    contact_sheets: list[dict],
    storyboard: dict,
    *,
    shot_durations: Optional[list[float]] = None,
    run_id: str = "_unknown",
) -> dict:
    """Send contact sheets + storyboard context to Claude, return structured cut plan.

    `contact_sheets` is a list aligned with shots:
        [{"idx": 0, "frames": [{"path": Path, "t": 0.25}, ...]}, ...]
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic

    import httpx
    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=httpx.Timeout(120.0, connect=30.0))
    shots = storyboard.get("shots", [])
    n = len(shots)
    schema = _schema(n)

    content: list[dict] = [
        {
            "type": "text",
            "text": f"""# STORYBOARD CONTEXT
Title: {storyboard.get('title', '')}
Logline: {storyboard.get('logline', '')}
Characters: {storyboard.get('character_sheet', '')}
World: {storyboard.get('world_sheet', '')}

# TASK
Below are {n} rendered shots, in order. For each shot I'm giving you a contact sheet — evenly-spaced frames, each labeled with its timestamp inside the shot.

Decide, for each shot:
  - Quality score 1-10, defects (if any), regenerate_recommended
  - cut_in and cut_out in seconds (within the shot's own duration — NOT the trailer's global timeline)
  - Continuity to the NEXT shot (brief — how they hand off)
  - One-line reasoning

Then give overall_notes on the whole assembly.

Return JSON matching this schema:
{json.dumps(schema, indent=2)}

Return ONLY the JSON object.""",
        }
    ]

    for i, sheet in enumerate(contact_sheets):
        shot = shots[i] if i < len(shots) else {}
        duration = shot_durations[i] if shot_durations and i < len(shot_durations) else shot.get("duration_s", 5)
        content.append(
            {
                "type": "text",
                "text": f"\n## SHOT {i+1}  —  beat: {shot.get('beat', '')}  —  rendered duration: {duration}s\nMotion prompt: {shot.get('motion_prompt', '')[:200]}",
            }
        )
        for f in sheet.get("frames", []):
            try:
                resized, mime = imgutils.resize_path(f["path"], max_side=1024)
            except Exception as e:
                print(f"[review] frame resize failed for {f.get('path', '?')}: {e}", file=sys.stderr)
                continue
            content.append({"type": "text", "text": f"frame at t={f['t']:.2f}s:"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.b64encode(resized).decode("ascii"),
                    },
                }
            )

    if len(contact_sheets) != n:
        print(f"[review] warning: {len(contact_sheets)} contact sheets for {n} shots — schema expects {n}", file=sys.stderr)

    resp = _call_with_retry(
        client,
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS_REVIEW,
        system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
    )
    try:
        import costs
        usage = resp.usage
        costs.log_text(
            run_id,
            model=ANTHROPIC_MODEL, phase="review",
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
    except Exception as e: print(f"[costs] review cost log failed: {e}", file=sys.stderr)

    raw = textutils.strip_json_fences(textutils.resp_text(resp.content))

    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Cut plan returned non-JSON: {e}\n---\n{textutils.sanitize_for_log(raw)}")

    # Clamp cut points to actual shot durations (Claude can miscalculate)
    for i, s in enumerate(plan.get("shots", [])):
        if not isinstance(s, dict):
            continue
        max_dur = shot_durations[i] if shot_durations and i < len(shot_durations) else 10.0
        min_span = min(0.3, max_dur)
        orig_in = float(s.get("cut_in", 0.0))
        orig_out = float(s.get("cut_out", max_dur))
        s["cut_in"] = max(0.0, min(orig_in, max_dur - min_span))
        s["cut_out"] = min(max(s["cut_in"] + min_span, min(orig_out, max_dur)), max_dur)
        if abs(s["cut_in"] - orig_in) > 0.01 or abs(s["cut_out"] - orig_out) > 0.01:
            print(f"[review] shot {i+1}: clamped cuts {orig_in:.2f}–{orig_out:.2f} → {s['cut_in']:.2f}–{s['cut_out']:.2f} (max_dur={max_dur:.2f})", file=sys.stderr)

    try: client.close()
    except Exception: pass
    return plan


# ─── Vision-refined timeline ──────────────────────────────────────────────

_REFINE_SYSTEM = """You are a senior film editor polishing a rough cut. You are given:

  1. A timeline of slices — each slice = (shot_idx, variant_idx, slice_in, slice_out).
  2. Contact sheets for every READY variant of every shot referenced.

Your job: for each slice, decide if a DIFFERENT variant of the same shot would be a stronger pick for that cut position. A variant is "stronger" if its composition at the proportional moment is clearer, more impactful, or has better subject positioning than the current pick.

Do NOT change slice_in / slice_out or shot_idx — only variant_idx. If every slice is already well-picked, return the timeline unchanged.

Return JSON: {"timeline": [{"idx": <slice_idx>, "variant_idx": <picked>, "reasoning": "<one line>"}]}"""


# ─── Continuity QC (adjacent-pair vision pass) ───────────────────────────

_CONTINUITY_SYSTEM = """You are a script supervisor + continuity editor. You watch PAIRS of adjacent shots from a trailer and call out continuity breaks that would make a real editor wince.

What you're looking for:
  - EYELINE breaks (180° rule violations)
  - PROP / wardrobe mismatches
  - LIGHTING / time-of-day jumps with no narrative cue
  - COLOR TEMPERATURE discontinuity in a scene
  - MOTION stutter WITHIN a shot (frame hitches, morphs)
  - ENERGY mismatch between cuts
  - IDENTITY drift (same character looks different)

For each adjacent pair output ONE finding with severity: ok / minor / major / unsure.
Be specific — "eyeline break: shot 3 looks right, shot 4 subject is on the right" beats generic wording.

Output ONLY valid JSON."""


def _continuity_schema(n_pairs: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "pairs": {
                "type": "array",
                "minItems": n_pairs, "maxItems": n_pairs,
                "items": {
                    "type": "object",
                    "properties": {
                        "pair_idx": {"type": "integer"},
                        "from_shot": {"type": "integer"},
                        "to_shot": {"type": "integer"},
                        "severity": {"type": "string", "enum": ["ok", "minor", "major", "unsure"]},
                        "issue": {"type": ["string", "null"]},
                        "suggested_fix": {"type": ["string", "null"]},
                    },
                    "required": ["pair_idx", "from_shot", "to_shot", "severity", "issue", "suggested_fix"],
                },
            },
            "overall_notes": {"type": "string"},
        },
        "required": ["pairs", "overall_notes"],
    }


def check_continuity(contact_sheets: list[dict], storyboard: dict, *, run_id: str = "_unknown") -> dict:
    """Pair-wise continuity review via Claude vision."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic
    import httpx
    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=httpx.Timeout(120.0, connect=30.0))
    shots = storyboard.get("shots", [])
    n = len(shots)
    if n < 2:
        return {"pairs": [], "overall_notes": "only one shot — nothing to pair"}
    n_pairs = n - 1
    schema = _continuity_schema(n_pairs)

    content: list[dict] = [{
        "type": "text",
        "text": f"""# STORYBOARD
Title: {storyboard.get('title', '')}
Characters: {storyboard.get('character_sheet', '')}
World: {storyboard.get('world_sheet', '')}

# TASK
Review {n_pairs} adjacent shot pairs. Each pair: the END frames of shot N vs the OPENING frames of shot N+1.
Return JSON:
{json.dumps(schema, indent=2)}

Return ONLY the JSON."""
    }]

    for pair_i in range(n_pairs):
        a = next((s for s in contact_sheets if s.get("idx") == pair_i), None)
        b = next((s for s in contact_sheets if s.get("idx") == pair_i + 1), None)
        if not a or not b: continue
        a_all = a.get("frames") or []
        b_all = b.get("frames") or []
        if len(a_all) < 2 or len(b_all) < 2:
            print(f"[review] warning: continuity pair {pair_i} has sparse frames (shot {pair_i+1}: {len(a_all)}, shot {pair_i+2}: {len(b_all)}), skipping", file=sys.stderr)
            continue
        a_frames = sorted(a_all, key=lambda f: f.get("t", 0))[-2:]
        b_frames = sorted(b_all, key=lambda f: f.get("t", 0))[:2]
        content.append({"type": "text", "text": f"\n## PAIR {pair_i}: shot {pair_i+1} → shot {pair_i+2}"})
        content.append({"type": "text", "text": f"Shot {pair_i+1} ending frames:"})
        for f in a_frames:
            try:
                resized, mime = imgutils.resize_path(Path(f["path"]), max_side=900)
                content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(resized).decode("ascii")}})
            except Exception as e:
                print(f"[review] continuity frame resize failed: {e}", file=sys.stderr)
                continue
        content.append({"type": "text", "text": f"Shot {pair_i+2} opening frames:"})
        for f in b_frames:
            try:
                resized, mime = imgutils.resize_path(Path(f["path"]), max_side=900)
                content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(resized).decode("ascii")}})
            except Exception as e:
                print(f"[review] continuity frame resize failed: {e}", file=sys.stderr)
                continue

    resp = _call_with_retry(
        client,
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS_REVIEW,
        system=[{"type": "text", "text": _CONTINUITY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
    )
    try:
        import costs
        u = resp.usage
        costs.log_text(run_id, model=ANTHROPIC_MODEL, phase="continuity",
            input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            cached_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
    except Exception as e: print(f"[costs] continuity cost log failed: {e}", file=sys.stderr)

    raw = textutils.strip_json_fences(textutils.resp_text(resp.content))
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"continuity check returned non-JSON: {textutils.sanitize_for_log(raw)}")
    try: client.close()
    except Exception: pass
    return out


def refine_timeline_with_vision(
    entries: list[dict],
    sheets_by_shot: dict,
    storyboard: dict,
    *,
    run_id: str = "_unknown",
) -> list[dict]:
    """Re-pick variant_idx for each timeline slice using Claude vision.
    sheets_by_shot[shot_idx][variant_idx] = list of frame dicts."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic

    import httpx
    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=httpx.Timeout(120.0, connect=30.0))
    shots = storyboard.get("shots", [])

    content: list[dict] = [{"type": "text", "text": f"""# STORYBOARD
Title: {storyboard.get('title', '')}
Characters: {storyboard.get('character_sheet', '')}

# TIMELINE ({len(entries)} slices)
{chr(10).join(f"  slice {i}: shot {e['shot_idx']+1} take {e['variant_idx']+1} @ {e['slice_in']:.2f}-{e['slice_out']:.2f}s ({e['duration']:.2f}s)" for i, e in enumerate(entries))}

# AVAILABLE VARIANTS
Below are contact sheets for every variant of every shot referenced. Study the visual content — which take has the strongest composition at the relevant moment in each slice?"""}]

    for shot_idx in sorted(sheets_by_shot.keys()):
        beat = shots[shot_idx].get("beat", "") if shot_idx < len(shots) else ""
        content.append({"type": "text", "text": f"\n## SHOT {shot_idx+1}  ({beat})"})
        for variant_idx, frames in sheets_by_shot[shot_idx].items():
            content.append({"type": "text", "text": f"### Take {variant_idx+1}"})
            for f in frames[:4]:  # Cap frames per variant for token budget
                try:
                    resized, mime = imgutils.resize_path(Path(f["path"]), max_side=768)
                    content.append({"type": "text", "text": f"t={f['t']:.2f}s:"})
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(resized).decode("ascii")},
                    })
                except Exception as e:
                    print(f"[review] timeline frame resize failed: {e}", file=sys.stderr)
                    continue

    content.append({"type": "text", "text": """
# TASK
For each slice, pick the strongest variant. Return JSON:
{"timeline": [{"idx": 0, "variant_idx": N, "reasoning": "brief"}, ...]}
Include ALL slices, even if unchanged. Return ONLY the JSON."""})

    resp = _call_with_retry(
        client,
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS_REVIEW,
        system=[{"type": "text", "text": _REFINE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
    )
    try:
        import costs
        usage = resp.usage
        costs.log_text(
            run_id,
            model=ANTHROPIC_MODEL, phase="review",
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
    except Exception as e: print(f"[costs] review cost log failed: {e}", file=sys.stderr)

    raw = textutils.strip_json_fences(textutils.resp_text(resp.content))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Timeline refinement returned non-JSON: {e}\n---\n{textutils.sanitize_for_log(raw)}")
    picks = {}
    for p in data.get("timeline", []):
        if isinstance(p, dict) and "idx" in p:
            picks[p["idx"]] = p

    refined = []
    for i, e in enumerate(entries):
        pick = picks.get(i)
        new_entry = {**e}
        if pick and pick.get("variant_idx") != e["variant_idx"]:
            # Validate: must be a ready variant of the same shot
            if e["shot_idx"] in sheets_by_shot and pick["variant_idx"] in sheets_by_shot[e["shot_idx"]]:
                new_entry["variant_idx"] = pick["variant_idx"]
                new_entry["reasoning"] = (pick.get("reasoning") or e.get("reasoning", "") + " · vision-refined").strip()
        refined.append(new_entry)
    try: client.close()
    except Exception: pass
    return refined
