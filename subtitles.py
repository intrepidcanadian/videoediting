"""Subtitles: build SRT/WebVTT from the VO script; burn into exports.

Two paths to a subtitle track:
  1. FROM VO SCRIPT (deterministic, cheap)
     We already have per-line text + start_s + per-line audio file duration.
     Build SRT directly — no transcription needed. This is the default.

  2. FROM AUDIO TRANSCRIPTION (future)
     If shots have dialogue or an external audio source, transcribe via Gemini
     audio (reusing our existing key) and merge. Stub lives in transcribe().

Burn-in: ffmpeg's `subtitles=` filter applies the SRT as hardcoded captions
during a re-encode. Optional — we also ship the raw .srt alongside so editors
can toggle captions in a player.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Optional

FFPROBE = shutil.which("ffprobe")


def _secs_to_srt_timestamp(seconds: float) -> str:
    """'01:02:03,456' — SRT's native format."""
    if seconds < 0: seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _audio_duration(audio_path: Path) -> float:
    if not FFPROBE or not audio_path.exists():
        return 0.0
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, timeout=10,
        )
        raw = r.stdout.decode("utf-8").strip()
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0
    except Exception:
        return 0.0


def build_srt_from_vo(vo_meta: dict, run_dir: Path) -> str:
    """Construct an SRT string from a VO meta dict.

    For each line, we look up its audio file on disk and read its actual duration,
    so captions stay in sync even if Claude's `suggested_start_s` was optimistic.
    Minimum caption display is 1.5s (so short lines don't flash and vanish).
    """
    script = (vo_meta or {}).get("script") or {}
    lines = script.get("lines") or []
    audio_paths = (vo_meta or {}).get("lines_audio") or []

    entries = []
    for i, line in enumerate(lines):
        text = (line.get("text") or "").strip()
        if not text: continue
        start = float(line.get("suggested_start_s") or 0.0)

        # Compute end from actual audio duration when available
        end = start + 2.0  # fallback
        if i < len(audio_paths) and audio_paths[i]:
            audio_path = run_dir / audio_paths[i]
            dur = _audio_duration(audio_path)
            if dur > 0:
                end = start + dur
        # Enforce minimum display time
        if end - start < 1.5:
            end = start + 1.5

        entries.append((i + 1, start, end, text))

    if not entries:
        return ""

    out_lines = []
    for idx, s, e, text in entries:
        out_lines.append(str(idx))
        out_lines.append(f"{_secs_to_srt_timestamp(s)} --> {_secs_to_srt_timestamp(e)}")
        out_lines.append(text)
        out_lines.append("")
    return "\n".join(out_lines)


def build_webvtt_from_vo(vo_meta: dict, run_dir: Path) -> str:
    """WebVTT version — usable directly by HTML5 <track>."""
    srt = build_srt_from_vo(vo_meta, run_dir)
    if not srt: return ""
    # SRT → WebVTT: replace ',' in timestamps with '.' and prepend header
    vtt = srt.replace(",", ".")
    return "WEBVTT\n\n" + vtt


async def burn_in(
    input_video: Path,
    srt_path: Path,
    output_path: Path,
    *,
    font_size: int = 24,
    outline: int = 2,
    margin_v: int = 40,
) -> Path:
    """Hardcode subtitles onto a video using ffmpeg's subtitles filter.

    Styling via force_style: centered, white w/ black outline, readable against
    most footage. Tuned for trailer aesthetics (larger than default, more margin).
    """
    from video import FFMPEG, require_ffmpeg
    require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Escape SRT path for ffmpeg filter graph (colons on macOS are fine but paths with special chars break)
    srt_str = str(srt_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace('"', '\\"').replace("[", "\\[").replace("]", "\\]").replace(";", "\\;")
    style = f"FontName=Arial,FontSize={font_size},PrimaryColour=&HFFFFFF,OutlineColour=&H000000,BorderStyle=1,Outline={outline},Shadow=0,MarginV={margin_v},Alignment=2"

    vf = f"subtitles='{srt_str}':force_style='{style}'"

    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(input_video),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = (stderr.decode("utf-8", errors="ignore") or "").split("\n")[-10:]
        raise RuntimeError("subtitles burn-in failed:\n" + "\n".join(tail))
    return output_path


# ─── Transcription stub (future, for dialogue from shot audio) ───────────

async def transcribe(audio_path: Path) -> list[dict]:
    """Transcribe audio → [{start, end, text}]. Stubbed for v1.

    Planned: upload to Gemini 2.x file API, request structured transcription with
    segment timestamps. Reuses existing GEMINI_API_KEY — no new provider needed.
    """
    raise NotImplementedError(
        "Audio transcription is not yet implemented. "
        "Use build_srt_from_vo() when captions come from the VO script — that's "
        "the default + cheapest path."
    )
