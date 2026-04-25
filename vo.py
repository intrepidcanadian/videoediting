"""Voice-over phase: Claude writes a script, ElevenLabs TTS synthesizes each line.

Extracted from pipeline.py to keep the orchestrator leaner. Imports the state
primitives (`_save_state`, `_get_lock`, `get_state`, `_run_dir`, `_now`, `_record_taste`)
from pipeline itself — there's one source of truth for run state.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

import audio as audio_mod
import costs
import logger
import pipeline
import textutils


def _vo_total_trailer_duration(state: dict) -> float:
    """Estimate the final trailer duration for VO script generation.

    Prefers the approved cut-plan timeline total; falls back to storyboard
    shot_duration sum."""
    cp = state.get("cut_plan") or {}
    tl = cp.get("timeline") or {}
    if tl.get("total_duration"):
        return float(tl["total_duration"])
    shots = (state.get("story") or {}).get("shots") or []
    return float(sum(s.get("duration_s", 5) for s in shots))


async def generate_vo_script(run_id: str, *, vibe: Optional[str] = None) -> dict:
    """Claude writes a trailer VO script from the storyboard."""
    state = pipeline.get_state(run_id)
    story = state.get("story")
    if not story:
        raise RuntimeError("storyboard must exist before generating VO script")

    total = _vo_total_trailer_duration(state)
    logger.info(run_id, "vo", f"asking Claude for VO script ({total:.1f}s trailer)…")
    try:
        script = await asyncio.to_thread(
            audio_mod.generate_vo_script,
            story=story, total_duration_s=total, vibe=vibe, run_id=run_id,
        )
    except Exception as e:
        logger.error(run_id, "vo", f"✗ VO script failed: {e}")
        raise

    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        existing_audio = state.get("audio") or {}
        existing_vo = existing_audio.get("vo") or {}
        vo = {
            "script": script,
            "voice_id": existing_vo.get("voice_id") or audio_mod.DEFAULT_VOICE_ID,
            "lines_audio": [None] * len(script.get("lines", [])),
            "status": "script_ready",
            "generated_at": textutils.now_iso(),
        }
        state["audio"] = {**existing_audio, "vo": vo}
        pipeline._save_state(run_id, state)
    logger.success(run_id, "vo", f"✓ {len(script['lines'])} lines — {script.get('total_chars', '?')} chars")
    return vo


async def update_vo_script(run_id: str, *, lines: list[dict], voice_id: Optional[str] = None) -> dict:
    """User-edited VO script or voice choice. Invalidates any already-synthesized audio."""
    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        audio = state.get("audio") or {}
        vo = audio.get("vo") or {}
        script = vo.get("script") or {"lines": []}

        old_lines = script.get("lines") or []
        new_lines = []
        for i, l in enumerate(lines):
            base = old_lines[i] if i < len(old_lines) else {}
            merged = {**base, **l}
            new_lines.append(merged)
        script["lines"] = new_lines
        script["total_chars"] = sum(len(l.get("text", "")) for l in new_lines)

        vo["script"] = script
        if voice_id:
            vo["voice_id"] = voice_id
        vo["lines_audio"] = [None] * len(new_lines)
        vo["status"] = "script_ready"
        audio["vo"] = vo
        state["audio"] = audio
        pipeline._save_state(run_id, state)
    return vo


async def synthesize_vo(run_id: str) -> dict:
    """TTS every line in the current VO script via ElevenLabs."""
    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        audio = state.get("audio") or {}
        vo = audio.get("vo") or {}
        script = vo.get("script") or {}
        lines = script.get("lines") or []
        voice_id = vo.get("voice_id") or audio_mod.DEFAULT_VOICE_ID
        if not lines:
            raise RuntimeError("no VO script — generate one first")

        run_dir = pipeline._run_dir(run_id)
        audio_dir = run_dir / "audio" / "vo"
        audio_dir.mkdir(parents=True, exist_ok=True)

        vo["status"] = "synthesizing"
        audio["vo"] = vo
        state["audio"] = audio
        pipeline._save_state(run_id, state)

    # Long-running TTS loop runs OUTSIDE the lock so readers can poll status.
    # We serialize via the "synthesizing" status flag above; a second synthesize_vo
    # caller will see it and decide how to proceed (currently: runs again; fine since
    # each call writes to a deterministic path and last-write-wins on results merge).
    voice_name = next((v["name"] for v in audio_mod.PRESET_VOICES if v["id"] == voice_id), voice_id)
    logger.info(run_id, "vo", f"ElevenLabs synthesizing {len(lines)} line(s) with {voice_name}…")
    lines_audio = []
    try:
        for i, line in enumerate(lines):
            out = audio_dir / f"line_{i+1:02d}.mp3"
            await audio_mod.synthesize_line(line["text"], voice_id=voice_id, output_path=out)
            rel = f"audio/vo/{out.name}"
            lines_audio.append(rel)
            logger.info(run_id, "vo", f"  line {i+1}/{len(lines)} → {out.stat().st_size // 1024} KB")
    except Exception as e:
        async with pipeline._get_lock(run_id):
            state = pipeline.get_state(run_id)
            audio = state.get("audio") or {}
            vo = audio.get("vo") or {}
            vo["status"] = "failed"
            vo["error"] = str(e)[:400]
            audio["vo"] = vo
            state["audio"] = audio
            pipeline._save_state(run_id, state)
        logger.error(run_id, "vo", f"✗ synthesis failed: {e}")
        raise

    try:
        total_chars = sum(len(l.get("text", "")) for l in lines)
        from constants import ELEVENLABS_TTS_PRICE_PER_1K_CHARS
        cost = (total_chars / 1000) * ELEVENLABS_TTS_PRICE_PER_1K_CHARS
        costs._write(run_id, {
            "ts": textutils.now_iso(), "provider": "elevenlabs", "model": audio_mod.ELEVENLABS_VOICE_MODEL,
            "phase": "vo", "chars": total_chars, "cost_usd": round(cost, 6),
        })
    except Exception as e: print(f"[costs] vo cost log failed: {e}", file=sys.stderr)

    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        audio = state.get("audio") or {}
        vo = audio.get("vo") or {}
        vo["lines_audio"] = lines_audio
        vo["status"] = "ready"
        vo.pop("error", None)
        audio["vo"] = vo
        state["audio"] = audio
        pipeline._save_state(run_id, state)
    logger.success(run_id, "vo", f"✓ {len(lines_audio)} line(s) synthesized")
    pipeline._record_taste("voice_pick", run_id=run_id, voice_id=voice_id, line_count=len(lines_audio))
    return vo


async def remove_vo(run_id: str) -> None:
    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        audio = state.get("audio") or {}
        vo = audio.get("vo") or {}
        run_dir = pipeline._run_dir(run_id)
        for rel in vo.get("lines_audio") or []:
            if not rel: continue
            try: (run_dir / rel).unlink(missing_ok=True)
            except Exception as e: print(f"[cleanup] vo audio unlink failed: {e}", file=sys.stderr)
        audio["vo"] = None
        state["audio"] = audio
        pipeline._save_state(run_id, state)
