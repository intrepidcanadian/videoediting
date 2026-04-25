"""Trailer audio: VO script generation + TTS synthesis + AI music composition.

Two providers:
  - Claude writes the VO script from the storyboard
  - ElevenLabs synthesizes the VO and composes AI music

Why ElevenLabs: it's the de facto trailer-voice quality bar right now. Josh /
Adam / Arnold are THE cinematic narrators. The API is simple (POST text → get mp3
bytes), and provides both TTS and text-to-music on the same key.

If no ELEVENLABS_API_KEY is configured, VO is unavailable — we fail with a clear
message. Music + title card still work without it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

import retry as retry_mod
import textutils

from constants import (
    ANTHROPIC_MODEL, ELEVENLABS_BASE_URL as ELEVENLABS_BASE,
    ELEVENLABS_VOICE_MODEL, MAX_TOKENS_AUDIO,
)

load_dotenv(Path(__file__).parent / ".env", override=True)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ─── Curated trailer-voice presets (ElevenLabs default voice library) ────
# These are the well-known trailer-narrator voices available on every account.

PRESET_VOICES = [
    {"id": "TxGEqnHWrfWFTfGW9XjX", "name": "Josh",    "style": "Deep cinematic narrator — classic trailer voice"},
    {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam",    "style": "Authoritative, warm — 'In a world where…'"},
    {"id": "VR6AewLTigWG4xSOukaG", "name": "Arnold",  "style": "Crisp, measured — modern thriller trailer"},
    {"id": "ErXwobaYiN019PkySvjV", "name": "Antoni",  "style": "Younger, conversational — indie film"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella",   "style": "Warm female, literary — character piece"},
    {"id": "AZnzlk1XvdvUeBnXmlld", "name": "Domi",    "style": "Strong female, urgent — action trailer"},
    {"id": "MF3mGyEYCl7XYWbV9V6O", "name": "Elli",    "style": "Gentle female — drama"},
    {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel",  "style": "Calm female — documentary / prestige"},
]

DEFAULT_VOICE_ID = PRESET_VOICES[0]["id"]  # Josh


def status() -> dict:
    return {
        "elevenlabs_configured": bool(ELEVENLABS_API_KEY),
        "voices": PRESET_VOICES,
        "default_voice_id": DEFAULT_VOICE_ID,
        "model": ELEVENLABS_VOICE_MODEL,
    }


# ─── VO script generation (Claude) ───────────────────────────────────────

_VO_SYSTEM_PROMPT = """You are a trailer voice-over writer. Given a storyboard + a trailer's final duration, you write 1–4 lines of narrator copy.

Hard rules:
  - BE TERSE. Trailer VO is compression. "In a world where the rain never stops, one detective remembers the sun." 12 words, a whole premise.
  - Match the tone of the storyboard (thriller → clipped and dread-heavy; drama → reflective and slow; action → staccato and urgent).
  - Each line should ANCHOR to a specific beat — hook, turn, reveal, tag. Never VO every shot; leave silence for the visuals.
  - At the trailer's title card, a closing tag line is optional ("This summer." / "Coming soon." / a logline echo).
  - No clichés that bore ("this summer, prepare for the ride of your life"). Specificity always.

Output ONLY JSON with:
  - lines: [{text, suggested_start_s, emphasis (low|medium|high), beat_role (hook|turn|reveal|tag)}]
  - voice_character: one-sentence description of the ideal voice (age, timbre, energy) — informs voice picking.
  - total_chars: sum of character counts (so we can sanity-check cost).

No markdown fences."""


def _vo_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "The narrator line, no stage directions"},
                        "suggested_start_s": {"type": "number", "description": "When this line enters the trailer (seconds from start)"},
                        "emphasis": {"type": "string", "enum": ["low", "medium", "high"]},
                        "beat_role": {"type": "string", "enum": ["hook", "turn", "reveal", "tag"]},
                    },
                    "required": ["text", "suggested_start_s", "emphasis", "beat_role"],
                },
            },
            "voice_character": {"type": "string"},
            "total_chars": {"type": "integer"},
        },
        "required": ["lines", "voice_character"],
    }


def generate_vo_script(
    *,
    story: dict,
    total_duration_s: float,
    vibe: Optional[str] = None,
    run_id: str = "_unknown",
) -> dict:
    """Claude writes the trailer narrator lines with suggested timing."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        schema = _vo_schema()

        shots_block = "\n".join(
            f"  Shot {i+1} ({s.get('beat', '?')}) {s.get('duration_s', 5)}s: {s.get('keyframe_prompt', '')[:120]}"
            for i, s in enumerate(story.get("shots", []))
        )

        user_msg = f"""# STORYBOARD
Title: {story.get('title', '')}
Logline: {story.get('logline', '')}
Characters: {story.get('character_sheet', '')}
World: {story.get('world_sheet', '')}

Shots:
{shots_block}

# TRAILER SPECS
- Total duration: {total_duration_s:.1f} seconds
- Tone vibe (optional): {vibe or '(derive from storyboard)'}

# TASK
Write 1–4 lines of narrator VO. Place them at beats where visuals breathe — not on top of busy shots. Each line must land a beat: hook / turn / reveal / tag.

Return JSON matching this schema:
{json.dumps(schema, indent=2)}

Output ONLY the JSON."""

        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS_AUDIO,
            system=[{"type": "text", "text": _VO_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        try:
            import costs
            usage = resp.usage
            costs.log_text(
                run_id, model=ANTHROPIC_MODEL, phase="vo_script",
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
        except Exception as e: print(f"[costs] vo_script cost log failed: {e}", file=sys.stderr)

        raw = textutils.strip_json_fences(textutils.resp_text(resp.content))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"VO script returned non-JSON: {e}\n---\n{textutils.sanitize_for_log(raw)}")
        data["total_chars"] = sum(len(l["text"]) for l in data.get("lines", []))
        return data
    finally:
        try: client.close()
        except Exception: pass


# ─── TTS synthesis (ElevenLabs) ──────────────────────────────────────────

@retry_mod.retry_on_transient(tries=3, base=2.0, max_wait=30.0)
async def synthesize_line(
    text: str,
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    output_path: Path,
    model_id: Optional[str] = None,
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    style: float = 0.0,
    speaker_boost: bool = True,
) -> Path:
    """POST text to ElevenLabs, write mp3 to output_path. Raises on failure.

    stability: 0 = very expressive / variable, 1 = very consistent / monotone.
               Trailer VO sits around 0.4-0.5 — enough consistency, some life.
    style:     0 = neutral, 1 = exaggerated. Keep low for narrator voice.
    """
    if not ELEVENLABS_API_KEY:
        raise RuntimeError(
            "ELEVENLABS_API_KEY not set. Add it to .env. "
            "Get a key at https://elevenlabs.io/app/settings/api-keys."
        )
    if not text or not text.strip():
        raise ValueError("text is empty")

    url = f"{ELEVENLABS_BASE}/text-to-speech/{voice_id}"
    payload = {
        "text": text,
        "model_id": model_id or ELEVENLABS_VOICE_MODEL,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": speaker_boost,
        },
    }
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise RuntimeError(f"ElevenLabs TTS request failed: {e}") from e

    if resp.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs TTS failed ({resp.status_code}): {textutils.safe_err_body(resp.text)}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(resp.content)
    return output_path


# ─── AI music (ElevenLabs Music beta) ────────────────────────────────────

@retry_mod.retry_on_transient(tries=2, base=2.0, max_wait=30.0)
async def compose_music(
    brief: str,
    output_path: Path,
    *,
    duration_seconds: float = 45.0,
    timeout_s: float = 180.0,
) -> Path:
    """Compose bespoke music matching a text brief via ElevenLabs Music API.
    `brief` is a natural-language description: tempo, mood, instrumentation, structure.

    ~$0.03 per 10 seconds of music (ElevenLabs beta pricing). Cheap enough that
    trying 2-3 variations per run is easy.
    """
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY not set — required for AI music.")
    if not brief or not brief.strip():
        raise ValueError("brief is required")

    # ElevenLabs Music endpoint (beta). Returns mp3 bytes directly.
    url = f"{ELEVENLABS_BASE}/music/compose"
    payload = {
        "prompt": brief.strip(),
        "music_length_ms": int(max(10, min(300, duration_seconds)) * 1000),
    }
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    timeout = httpx.Timeout(connect=30.0, read=timeout_s, write=30.0, pool=30.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise RuntimeError(f"ElevenLabs Music request failed: {e}") from e

    if resp.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs Music failed ({resp.status_code}): {textutils.safe_err_body(resp.text)}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(resp.content)
    return output_path


# ─── Music brief (Claude writes it from the storyboard) ─────────────────

_MUSIC_BRIEF_SYSTEM = """You write a concise brief for an AI music composer (ElevenLabs Music).
Your output goes directly to the music model as its prompt. Keep it tight.

Each brief should cover:
  - Tempo (BPM range)
  - Key / mode feel (major / minor / modal)
  - Instrumentation (strings, percussion, brass, synths, hybrid orchestral, etc)
  - Mood / tone (tense / triumphant / melancholic / propulsive)
  - Structure (e.g. "slow start with building strings, brass stab at midpoint, sustained tail")
  - A named reference if it helps ("in the style of Max Richter" or "Trent Reznor Social Network")

Output ONLY the brief — no JSON, no preamble, no bullet points. 2-4 sentences. This is what gets sent to the composer verbatim."""


def generate_music_brief(
    *,
    story: dict,
    duration_s: float,
    vibe: str = "",
    genre: str = "",
    run_id: str = "_unknown",
) -> str:
    """Claude reads the storyboard + writes a bespoke music brief."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic
    import httpx as _httpx

    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=_httpx.Timeout(60.0, connect=30.0))
    try:
        shots_block = "\n".join(
            f"  • Shot {i+1} ({s.get('beat', '?')}, {s.get('duration_s', 5)}s): {s.get('keyframe_prompt', '')[:80]}"
            for i, s in enumerate(story.get("shots", []))
        )

        user_msg = f"""# TRAILER
Title: {story.get('title', '')}
Logline: {story.get('logline', '')}
World: {story.get('world_sheet', '')}
Duration: {duration_s:.0f}s
Genre: {genre or '(derive from concept)'}
Extra vibe: {vibe or '(none)'}

# SHOTS
{shots_block}

# TASK
Write a music composition brief per the system prompt. Target duration {duration_s:.0f}s. Match the trailer's energy arc — quiet → build → peak → tag."""

        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=500,
            system=[{"type": "text", "text": _MUSIC_BRIEF_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        try:
            import costs
            u = resp.usage
            costs.log_text(run_id, model=ANTHROPIC_MODEL, phase="music_brief",
                input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                cached_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
        except Exception as e: print(f"[costs] music_brief cost log failed: {e}", file=sys.stderr)

        return textutils.resp_text(resp.content).strip()
    finally:
        try: client.close()
        except Exception: pass


