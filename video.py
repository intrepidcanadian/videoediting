"""ffmpeg helpers — concatenate shots into a single trailer, extract last frame.

Concat strategy: stream-copy first (fast, lossless), fall back to re-encode if the
Seedance outputs have slightly mismatched timebases.

Crossfade strategy: always re-encodes (xfade filter requires it). Use sparingly —
hard cuts are the trailer grammar; crossfades are for specific moods.
"""

import asyncio
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def require_ffmpeg():
    if not FFMPEG:
        raise RuntimeError("ffmpeg not found. Install with: brew install ffmpeg")


def _stderr_tail(stderr_bytes: Optional[bytes], lines: int = 10) -> list[str]:
    """Decode ffmpeg stderr bytes with error="replace" (so bad bytes don't drop
    silently) and return the last N non-empty lines. Using 'replace' consistently
    gives better diagnostics than 'ignore' when filenames contain non-UTF-8 bytes."""
    if not stderr_bytes:
        return []
    text = stderr_bytes.decode("utf-8", errors="replace")
    return text.split("\n")[-lines:]


def extract_last_frame(video_path: Path, output_png: Path) -> Path:
    """Extract the last frame of a video as a PNG. Best-effort two-stage: seek to end
    first (fast), fall back to keyframe scan if that fails."""
    require_ffmpeg()
    output_png.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-sseof", "-0.05",
        "-i", str(video_path),
        "-frames:v", "1", "-q:v", "2",
        str(output_png),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    if result.returncode != 0 or not output_png.exists():
        cmd2 = [
            FFMPEG, "-y", "-loglevel", "error",
            "-i", str(video_path),
            "-update", "1", "-q:v", "2",
            str(output_png),
        ]
        result2 = subprocess.run(cmd2, capture_output=True, timeout=30)
        if not output_png.exists():
            stderr_msg = (result2.stderr or result.stderr or b"").decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"extract_last_frame failed: {stderr_msg}")
    return output_png


async def concat_videos(input_paths: list[Path], output_path: Path) -> Path:
    """Stream-copy concat. Falls back to re-encode if codecs don't line up."""
    require_ffmpeg()
    if not input_paths:
        raise ValueError("concat_videos: no inputs")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = output_path.parent / f".concat_{output_path.stem}.txt"
    # ffmpeg concat demuxer: single-quoted paths, escape single quotes as '\'',
    # replace newlines so they can't break the line-based format.
    def _esc(p: Path) -> str:
        return str(p.resolve()).replace("'", "'\\''").replace("\n", " ")
    list_path.write_text("\n".join(f"file '{_esc(p)}'" for p in input_paths))

    try:
        cmd = [
            FFMPEG, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            cmd2 = [
                FFMPEG, "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(output_path),
            ]
            proc2 = await asyncio.create_subprocess_exec(
                *cmd2,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr2 = await proc2.communicate()
            if proc2.returncode != 0:
                tail = _stderr_tail(stderr2)
                raise RuntimeError("ffmpeg concat (re-encode) failed:\n" + "\n".join(tail))
        return output_path
    finally:
        try:
            list_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"[concat] temp file cleanup failed: {e}", file=sys.stderr)


async def concat_with_crossfade(
    input_paths: list[Path], output_path: Path, fade_duration: float = 0.25
) -> Path:
    """Concatenate with xfade transitions between shots. Always re-encodes."""
    require_ffmpeg()
    if len(input_paths) < 2:
        return await concat_videos(input_paths, output_path)

    durations = [await _probe_duration(p) for p in input_paths]

    inputs: list[str] = []
    for p in input_paths:
        inputs.extend(["-i", str(p)])

    parts: list[str] = []
    cumulative = 0.0
    last = "0"
    for i in range(1, len(input_paths)):
        cumulative += durations[i - 1]
        offset = max(0.0, cumulative - fade_duration * i)
        nxt = f"v{i:02d}"
        parts.append(
            f"[{last}][{i}]xfade=transition=fade:duration={fade_duration}:offset={offset:.3f}[{nxt}]"
        )
        last = nxt
    filter_complex = ";".join(parts)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{last}]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = _stderr_tail(stderr)
        raise RuntimeError("ffmpeg crossfade failed:\n" + "\n".join(tail))
    return output_path


async def still_to_clip(
    still_path: Path,
    output_path: Path,
    *,
    duration: float = 2.5,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> Path:
    """Turn a still image into a held video clip. Used for title cards when not
    animating with Seedance."""
    require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scale = ""
    if width and height:
        scale = f",scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-loop", "1", "-i", str(still_path),
        "-t", f"{duration:.3f}",
        "-vf", f"format=yuv420p{scale}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-r", "24",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = _stderr_tail(stderr)
        raise RuntimeError("still_to_clip failed:\n" + "\n".join(tail))
    return output_path


_PLATFORM_VARIANTS = {
    "9x16":  {"w": 1080, "h": 1920, "label": "9:16 (TikTok / Reels / Shorts)"},
    "1x1":   {"w": 1080, "h": 1080, "label": "1:1 (Instagram feed)"},
    "4x5":   {"w": 1080, "h": 1350, "label": "4:5 (IG/FB portrait)"},
    "16x9":  {"w": 1920, "h": 1080, "label": "16:9 (YouTube / desktop)"},
}


def platform_variants_available() -> list[dict]:
    return [{"preset": k, **v} for k, v in _PLATFORM_VARIANTS.items()]


async def reframe_to_platform(input_path: Path, output_path: Path, preset: str) -> Path:
    """Center-crop + scale a master trailer into a platform variant (9:16, 1:1, etc).
    Preserves audio. Re-encodes video to h264 + AAC for broadest compatibility."""
    require_ffmpeg()
    if preset not in _PLATFORM_VARIANTS:
        raise ValueError(f"unknown platform preset: {preset}. Valid: {list(_PLATFORM_VARIANTS)}")
    spec = _PLATFORM_VARIANTS[preset]
    w, h = spec["w"], spec["h"]
    # Crop to target aspect centered on frame, then scale.
    vf = (
        f"crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})',"
        f"scale={w}:{h}:flags=lanczos,setsar=1"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(input_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = _stderr_tail(stderr)
        raise RuntimeError("reframe_to_platform failed:\n" + "\n".join(tail))
    return output_path


async def _still_to_kenburns_clip(
    still_path: Path,
    output_path: Path,
    *,
    duration: float,
    ratio: str = "16:9",
    direction: str = "in",   # in | out | left | right
    fps: int = 24,
) -> Path:
    """Turn one still into a motion clip using ffmpeg's zoompan (Ken Burns effect).

    Directions give variety across shots so an animatic doesn't all zoom the same way.
    Canvas size is derived from the ratio; output is always widescreen-safe.
    """
    require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Output canvas per ratio
    sizes = {
        "16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080),
        "4:3": (1440, 1080), "3:4": (1080, 1440), "21:9": (2560, 1080),
    }
    w, h = sizes.get(ratio, (1920, 1080))
    total_frames = max(1, int(duration * fps))

    # zoompan expressions — keep zoom in [1.0, 1.12] for subtlety
    # z = current zoom, x/y = pan positions (0.5 = center)
    if direction == "in":
        z_expr = f"'min(zoom+0.0012,1.12)'"
        x_expr = "'iw/2-(iw/zoom/2)'"; y_expr = "'ih/2-(ih/zoom/2)'"
    elif direction == "out":
        z_expr = f"'if(eq(on,0),1.12,max(zoom-0.0012,1.0))'"
        x_expr = "'iw/2-(iw/zoom/2)'"; y_expr = "'ih/2-(ih/zoom/2)'"
    elif direction == "left":
        z_expr = "'1.08'"
        x_expr = f"'iw*0.04+iw*0.06*(on/{total_frames})'"
        y_expr = "'ih/2-(ih/zoom/2)'"
    elif direction == "right":
        z_expr = "'1.08'"
        x_expr = f"'iw*0.9-iw*0.06-iw*0.06*(on/{total_frames})'"
        y_expr = "'ih/2-(ih/zoom/2)'"
    else:
        z_expr = "'min(zoom+0.0008,1.06)'"
        x_expr = "'iw/2-(iw/zoom/2)'"; y_expr = "'ih/2-(ih/zoom/2)'"

    # Scale input up first so we have headroom for the crop/pan; then zoompan; then fit to canvas
    vf = (
        f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase,"
        f"crop={w*2}:{h*2},"
        f"zoompan=z={z_expr}:x={x_expr}:y={y_expr}:d={total_frames}:s={w}x{h}:fps={fps},"
        f"setsar=1"
    )

    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-loop", "1", "-i", str(still_path),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = _stderr_tail(stderr)
        raise RuntimeError("ken-burns clip failed:\n" + "\n".join(tail))
    return output_path


async def build_animatic(
    keyframes: list[Path],
    durations: list[float],
    output_path: Path,
    *,
    ratio: str = "16:9",
    music_path: Optional[Path] = None,
    vo_lines: Optional[list[dict]] = None,
) -> Path:
    """Fast preview video built from keyframes with subtle motion, stitched
    with optional music + VO. No Seedance calls — this is for iteration speed
    before committing to full renders.

    Alternates Ken-Burns directions across shots for variety."""
    require_ffmpeg()
    if not keyframes:
        raise ValueError("no keyframes to build animatic from")
    if len(durations) < len(keyframes):
        durations = list(durations) + [5.0] * (len(keyframes) - len(durations))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = output_path.parent / ".animatic_tmp"
    tmp_dir.mkdir(exist_ok=True)

    # Render each keyframe as a clip with rotating motion direction
    directions = ["in", "left", "out", "right"]
    clips: list[Path] = []
    for i, (kf, dur) in enumerate(zip(keyframes, durations)):
        if not kf.exists():
            continue
        clip = tmp_dir / f"clip_{i:02d}.mp4"
        direction = directions[i % len(directions)]
        try:
            await _still_to_kenburns_clip(kf, clip, duration=float(dur), ratio=ratio, direction=direction)
            clips.append(clip)
        except Exception as e:
            print(f"[animatic] clip {i} render failed for {kf.name}: {e}", file=sys.stderr)
            continue
    if not clips:
        raise RuntimeError("no valid clips produced for animatic")

    # Concat them silently (all same codec/timebase since we just rendered)
    silent = tmp_dir / "silent.mp4"
    await concat_videos(clips, silent)

    # Mux music + VO just like the real stitch
    await mix_music_and_vo(silent, output_path, music_path=music_path, vo_lines=vo_lines)

    shutil.rmtree(tmp_dir, ignore_errors=True)

    return output_path


async def apply_look(input_path: Path, output_path: Path, filter_chain: str) -> Path:
    """Apply a color-grade filter chain (from looks.LOOKS) via ffmpeg.
    No-op if filter_chain is empty/None — just copies input."""
    require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not filter_chain:
        cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(input_path), "-c", "copy", str(output_path)]
    else:
        cmd = [
            FFMPEG, "-y", "-loglevel", "error",
            "-i", str(input_path),
            "-vf", filter_chain,
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "copy",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = _stderr_tail(stderr)
        raise RuntimeError("apply_look failed:\n" + "\n".join(tail))
    return output_path


def _db_to_amp(db: float) -> float:
    return round(10 ** (db / 20.0), 4)


async def mix_music_and_vo(
    video_path: Path,
    output_path: Path,
    *,
    music_path: Optional[Path] = None,
    vo_lines: Optional[list[dict]] = None,
    music_level_db: float = -3.0,
    vo_level_db: float = 0.0,
) -> Path:
    """Mix music + VO onto the final trailer. Music ducks automatically when any
    VO line is active (sidechain compressor).

    vo_lines: [{path: Path-like, start_s: float}, ...]

    Degrades gracefully:
      - no music + no VO → pass-through
      - only music → same as mux_audio (music over video)
      - only VO → VO over silent video
      - both → full mixed track with auto-ducking
    """
    require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    has_music = music_path is not None and Path(music_path).exists()
    clean_vo = []
    for vo in vo_lines or []:
        if not vo or not vo.get("path"): continue
        if Path(vo["path"]).exists():
            clean_vo.append({"path": Path(vo["path"]), "start_s": float(vo.get("start_s", 0.0))})
    has_vo = len(clean_vo) > 0

    if not has_music and not has_vo:
        cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(video_path), "-c", "copy", str(output_path)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            tail = (stderr or b"").decode(errors="replace")[-300:]
            raise RuntimeError(f"ffmpeg copy failed (rc={proc.returncode}): {tail}")
        return output_path

    if has_music and not has_vo:
        return await mux_audio(video_path, music_path, output_path)

    # Build filter graph
    inputs: list[str] = ["-i", str(video_path)]
    music_idx: Optional[int] = None
    if has_music:
        inputs.extend(["-i", str(music_path)])
        music_idx = len(inputs) // 2 - 1
    vo_start = len(inputs) // 2
    for vo in clean_vo:
        inputs.extend(["-i", str(vo["path"])])

    filter_parts: list[str] = []
    vo_labels: list[str] = []
    vo_vol = _db_to_amp(vo_level_db)
    for i, vo in enumerate(clean_vo):
        src = vo_start + i
        delay_ms = max(0, round(vo["start_s"] * 1000, 1))
        label = f"vo{i}"
        filter_parts.append(
            f"[{src}:a]aformat=sample_rates=48000:sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms},volume={vo_vol}[{label}]"
        )
        vo_labels.append(f"[{label}]")

    if len(vo_labels) > 1:
        filter_parts.append(
            f"{''.join(vo_labels)}amix=inputs={len(vo_labels)}:dropout_transition=0:normalize=0[voall]"
        )
    else:
        filter_parts.append(f"{vo_labels[0]}acopy[voall]")

    if has_music:
        music_vol = _db_to_amp(music_level_db)
        filter_parts.append(
            f"[{music_idx}:a]aformat=sample_rates=48000:sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={music_vol}[music]"
        )
        filter_parts.append("[voall]asplit=2[vosend][vomain]")
        filter_parts.append(
            f"[music][vosend]sidechaincompress=threshold=0.05:ratio=8:attack=5:release=400:"
            f"makeup=1:level_sc=1[ducked]"
        )
        filter_parts.append("[ducked][vomain]amix=inputs=2:dropout_transition=0:normalize=0[mix]")
        final_label = "[mix]"
    else:
        final_label = "[voall]"

    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", "0:v:0",
        "-map", final_label,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = _stderr_tail(stderr, 15)
        raise RuntimeError("mix_music_and_vo failed:\n" + "\n".join(tail))
    return output_path


async def mux_audio(video_path: Path, audio_path: Path, output_path: Path) -> Path:
    """Add an audio bed to a silent video. Trims/extends audio to video length."""
    require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-map", "0:v:0", "-map", "1:a:0",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = _stderr_tail(stderr)
        raise RuntimeError("ffmpeg mux_audio failed:\n" + "\n".join(tail))
    return output_path


def extract_frames(
    video_path: Path,
    output_dir: Path,
    *,
    n_frames: int = 5,
    prefix: str = "frame",
) -> list[dict]:
    """Extract N evenly-spaced frames. Returns [{'path': Path, 't': seconds}, ...].

    Skips the first and last 5% to avoid freeze-frames / tail stutter that Seedance
    sometimes renders. One subprocess call per frame — not the fastest, but reliable
    and easy to debug.
    """
    require_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = _probe_duration_sync(video_path)
    if duration <= 0:
        duration = 5.0  # reasonable default for Seedance shots

    head_margin = duration * 0.05
    tail_margin = duration * 0.05
    usable = max(0.1, duration - head_margin - tail_margin)
    timestamps = [head_margin + usable * i / max(1, n_frames - 1) for i in range(n_frames)]

    def _extract_one(args: tuple[int, float]) -> Optional[dict]:
        i, t = args
        fname = f"{prefix}_{i:02d}_{t:.2f}s.png"
        out_path = output_dir / fname
        cmd = [
            FFMPEG, "-y", "-loglevel", "error",
            "-ss", f"{t:.3f}",
            "-i", str(video_path),
            "-frames:v", "1", "-q:v", "2",
            str(out_path),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and out_path.exists():
            return {"path": out_path, "t": round(t, 3)}
        return None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(4, n_frames)) as pool:
        results = list(pool.map(_extract_one, enumerate(timestamps)))
    return [r for r in results if r is not None]


def _probe_duration_sync(video_path: Path) -> float:
    if not FFPROBE:
        return 0.0
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, timeout=10,
        )
        dur = float(r.stdout.decode("utf-8").strip())
        if dur <= 0 or dur > 86400:
            print(f"[video] probe returned out-of-range duration {dur} for {video_path}", file=sys.stderr)
            return 0.0
        return dur
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, ValueError) as e:
        print(f"[video] probe duration failed for {video_path}: {e}", file=sys.stderr)
        return 0.0
    except OSError as e:
        print(f"[video] probe duration OS error for {video_path}: {e}", file=sys.stderr)
        raise


def ffmpeg_scene_detect(video_path: Path, *, threshold: float = 0.30) -> list[float]:
    """Return a list of scene-change timestamps (seconds) via ffmpeg's scene filter.
    threshold: 0.3 is a reasonable default for theatrical trailers; lower = more cuts."""
    require_ffmpeg()
    cmd = [
        FFMPEG, "-i", str(video_path),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    timestamps: list[float] = []
    # showinfo lines look like: [Parsed_showinfo_1 @ ...] n: 0 pts: ... pts_time:4.208333 ...
    for line in r.stderr.splitlines():
        if "pts_time:" not in line:
            continue
        try:
            t = float(line.split("pts_time:", 1)[1].split()[0])
            timestamps.append(round(t, 3))
        except Exception:
            continue
    return sorted(set(timestamps))


def _clamp_segments(
    cuts: list[float],
    duration: float,
    *,
    min_s: float,
    max_s: float,
    min_count: int,
    max_count: int,
) -> list[tuple[float, float]]:
    """Given cut timestamps and total duration, build Seedance-compatible segments.

    Enforces min/max per-segment duration AND a shot-count window. Small segments get
    absorbed into neighbors; large ones get chopped into uniform sub-chunks."""
    # Build initial segments
    boundaries = [0.0] + [c for c in cuts if 0 < c < duration] + [duration]
    boundaries = sorted(set(boundaries))
    segs: list[tuple[float, float]] = [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]

    # Absorb too-short segments into the next (or previous if last)
    merged: list[tuple[float, float]] = []
    for s, e in segs:
        if e - s < min_s and merged:
            ps, _ = merged[-1]
            merged[-1] = (ps, e)
        else:
            merged.append((s, e))
    if merged and merged[0][1] - merged[0][0] < min_s and len(merged) > 1:
        s0, _ = merged[0]
        _, e1 = merged[1]
        merged[0] = (s0, e1)
        merged = [merged[0]] + merged[2:]

    # Split too-long segments into roughly uniform chunks ≤ max_s
    split: list[tuple[float, float]] = []
    for s, e in merged:
        dur = e - s
        if dur <= max_s:
            split.append((s, e))
            continue
        n_chunks = math.ceil(dur / max_s)
        chunk = dur / n_chunks
        for i in range(n_chunks):
            split.append((s + i * chunk, s + (i + 1) * chunk))

    # Cap count — if too many, drop shortest first; if too few, split longest
    while len(split) > max_count:
        shortest_idx = min(range(len(split)), key=lambda i: split[i][1] - split[i][0])
        split.pop(shortest_idx)
    while len(split) < min_count and split:
        longest_idx = max(range(len(split)), key=lambda i: split[i][1] - split[i][0])
        s, e = split[longest_idx]
        mid = (s + e) / 2
        split = split[:longest_idx] + [(s, mid), (mid, e)] + split[longest_idx + 1 :]

    return [(round(s, 3), round(e, 3)) for s, e in split]


async def scene_detect_and_segment(
    source_video: Path,
    output_dir: Path,
    *,
    min_shots: int = 4,
    max_shots: int = 10,
    min_segment_s: float = 2.5,
    max_segment_s: float = 12.0,
    scene_threshold: float = 0.30,
) -> list[dict]:
    """End-to-end: detect scenes in `source_video`, derive Seedance-compatible segments,
    and write each segment mp4 to `output_dir`. Returns list of segment metadata.

    Each returned item: {idx, start, end, duration, path (relative to output_dir.parent),
                         first_frame_path (relative)}
    """
    require_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = _probe_duration_sync(source_video) or 0.0
    if duration <= 0:
        raise RuntimeError("source video has zero duration")

    cuts = await asyncio.to_thread(ffmpeg_scene_detect, source_video, threshold=scene_threshold)
    segments = _clamp_segments(
        cuts, duration,
        min_s=min_segment_s, max_s=max_segment_s,
        min_count=min_shots, max_count=max_shots,
    )
    if not segments:
        raise RuntimeError("scene detection produced no usable segments")

    out: list[dict] = []
    for i, (s, e) in enumerate(segments):
        seg_path = output_dir / f"seg_{i+1:02d}.mp4"
        # Re-encode — cheap for a few seconds, and ensures keyframe at 0 so in-points land
        cmd = [
            FFMPEG, "-y", "-loglevel", "error",
            "-ss", f"{s:.3f}", "-i", str(source_video),
            "-t", f"{(e - s):.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
            str(seg_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not seg_path.exists():
            continue

        # First frame of segment — used as composition reference for Nano Banana
        first_frame = output_dir / f"seg_{i+1:02d}_first.png"
        f_cmd = [
            FFMPEG, "-y", "-loglevel", "error",
            "-ss", "0.1", "-i", str(seg_path),
            "-frames:v", "1", "-q:v", "2", str(first_frame),
        ]
        f_result = subprocess.run(f_cmd, capture_output=True, timeout=15)
        if f_result.returncode != 0:
            print(f"[scene_detect] first frame extraction failed for seg {i+1}: {f_result.stderr.decode(errors='replace')[:200]}", file=sys.stderr)

        out.append({
            "idx": i,
            "start": s,
            "end": e,
            "duration": round(e - s, 3),
            "path": seg_path,
            "first_frame_path": first_frame if first_frame.exists() else None,
        })
    return out


def probe_duration(video_path: Path) -> float:
    """Public wrapper for sync duration probe."""
    return _probe_duration_sync(video_path)


async def normalize_video_ref(
    input_path: Path,
    output_path: Path,
    *,
    max_duration: float = 15.0,
    max_long_side: int = 1280,
    target_bitrate: str = "1800k",
) -> dict:
    """Normalize an uploaded video to something Seedance happily accepts.

    - Trims to `max_duration` seconds max (hard limit at 15s for Seedance).
    - Downscales long side to `max_long_side` (720p-ish) to keep base64 payload sane.
    - Re-encodes H.264 baseline / yuv420p for broadest compatibility.

    Returns {'duration', 'trimmed_from', 'width', 'height', 'size'} for UI display.
    """
    require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    src_duration = _probe_duration_sync(input_path) or max_duration
    actual_duration = min(src_duration, max_duration)
    trimmed = src_duration > max_duration

    vf = (
        f"scale='if(gt(iw,ih),min({max_long_side},iw),-2)':"
        f"'if(gt(iw,ih),-2,min({max_long_side},ih))':flags=lanczos,setsar=1"
    )

    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(input_path),
        "-t", f"{actual_duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-profile:v", "baseline", "-level", "4.0",
        "-preset", "medium", "-b:v", target_bitrate,
        "-pix_fmt", "yuv420p",
        "-an",  # strip audio — Seedance doesn't need it from the ref
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = _stderr_tail(stderr)
        raise RuntimeError("normalize_video_ref failed:\n" + "\n".join(tail))

    info = {"duration": actual_duration, "trimmed_from": src_duration if trimmed else None}
    info["size"] = output_path.stat().st_size
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", str(output_path)],
            capture_output=True, timeout=10,
        )
        wh = r.stdout.decode().strip().split("x")
        if len(wh) == 2:
            info["width"], info["height"] = int(wh[0]), int(wh[1])
    except Exception as e:
        print(f"[video] probe dimensions failed for {output_path}: {e}", file=sys.stderr)
    return info


async def trim_video(input_path: Path, output_path: Path, *, start: float, end: float) -> Path:
    """Trim [start, end) seconds from input → output. Re-encodes for frame-accurate cuts."""
    require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.1, end - start)
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-i", str(input_path),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = _stderr_tail(stderr)
        raise RuntimeError("ffmpeg trim failed:\n" + "\n".join(tail))
    return output_path


async def _probe_duration(video_path: Path) -> float:
    if not FFPROBE:
        return 0.0
    proc = await asyncio.create_subprocess_exec(
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode("utf-8").strip())
    except (ValueError, AttributeError):
        return 0.0
