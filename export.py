"""Trailer export phase: title card, subtitles, platform variants.

Extracted from pipeline.py. These functions run AFTER stitch — they're the
"package for delivery" layer, not the "render the trailer" layer.

All state mutation goes through `pipeline._get_lock` + `pipeline._save_state`
to preserve the single-source-of-truth invariant.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Optional

import costs
import logger
import nano_banana
import pipeline
import seedance
import subtitles as subs_mod
import textutils
import video


# ─── Title card (Nano Banana still, optional Seedance animation) ─────────

async def generate_title_card(
    run_id: str,
    *,
    title_text: Optional[str] = None,
    style_hint: str = "",
    hold_seconds: float = 2.5,
    animate: bool = False,
) -> Path:
    """Generate a title card still via Nano Banana, optionally animate with Seedance.
    Saves to outputs/<run>/title/title.png (still) and /title/title.mp4 (animated),
    whichever is selected. hold_seconds only matters for the still path."""
    state = pipeline.get_state(run_id)
    run_dir = pipeline._run_dir(run_id)
    title_dir = run_dir / "title"
    title_dir.mkdir(exist_ok=True)

    title = title_text or state.get("story", {}).get("title") or state.get("params", {}).get("title") or "UNTITLED"
    style = style_hint or state.get("params", {}).get("style_intent", "")
    ratio = state.get("params", {}).get("ratio", "16:9")

    prompt = (
        f"A cinematic title card for a film trailer. Aspect ratio {ratio}. "
        f"Minimal composition, deep black background with subtle film grain and cinematic color grade. "
        f"The title text '{title}' rendered in weathered gold serif typography, centered, with considered "
        f"kerning and confident tracking. No subtitle, no dates, no additional text. "
        f"Photoreal film still aesthetic. {f'Style reference: {style}.' if style else ''}"
    )

    logger.info(run_id, "title", f"Nano Banana rendering title card for '{title}'…")
    still_path = title_dir / "title.png"
    try:
        await nano_banana.generate_keyframe(
            prompt, None, output_path=still_path,
            rules_target="nano_banana_title", run_id=run_id,
        )
        try:
            costs.log_image(run_id, model=nano_banana.NANO_BANANA_MODEL, phase="title")
        except Exception as e:
            logger.warn(run_id, "costs", f"cost logging failed (title): {e}")
    except Exception as e:
        logger.error(run_id, "title", f"✗ Nano Banana failed: {e}")
        raise
    logger.success(run_id, "title", f"✓ title still rendered ({still_path.stat().st_size // 1024} KB)")

    meta = {
        "path": f"title/{still_path.name}",
        "text": title,
        "style_hint": style,
        "hold_seconds": hold_seconds,
        "animate": animate,
        "animated_path": None,
        "updated_at": textutils.now_iso(),
    }

    if animate:
        logger.info(run_id, "title", "Seedance animating title card (subtle push + particles)…")
        mp4_path = title_dir / "title.mp4"
        motion_prompt = (
            f"The title card '{title}' holds on screen. Subtle cinematic motion: dust particles drift "
            f"across the frame, the camera pushes in slightly (5-10% scale), film grain shifts naturally. "
            f"No text changes, no typography movement — the letters stay stable and legible."
        )
        quality = state.get("params", {}).get("quality") or "standard"
        try:
            await seedance.render_shot(
                prompt=motion_prompt,
                reference_images=[still_path],
                output_path=mp4_path,
                ratio=ratio,
                duration=max(3, int(math.ceil(hold_seconds))),
                generate_audio=False,
                run_id=run_id,
                quality=quality,
            )
            try:
                costs.log_video(run_id, model=seedance.resolve_model(quality), phase="title_animate")
            except Exception as e2:
                print(f"[costs] title_animate cost log failed: {e2}", file=sys.stderr)
            meta["animated_path"] = f"title/{mp4_path.name}"
            logger.success(run_id, "title", f"✓ title animated ({mp4_path.stat().st_size // 1024} KB)")
        except Exception as e:
            logger.warn(run_id, "title", f"animation failed (still will be used instead): {e}")

    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        state["title_card"] = meta
        pipeline._save_state(run_id, state)
    return still_path


async def remove_title_card(run_id: str) -> None:
    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        tc = state.get("title_card")
        if tc:
            for key in ("path", "animated_path"):
                p = tc.get(key)
                if p:
                    try: (pipeline._run_dir(run_id) / p).unlink(missing_ok=True)
                    except Exception as e: print(f"[cleanup] title card {key} unlink failed: {e}", file=sys.stderr)
        state["title_card"] = None
        pipeline._save_state(run_id, state)


# ─── Platform variants (9:16, 1:1, 4:5 reframes of the master) ──────────

async def export_platform_variants(
    run_id: str,
    presets: list[str],
    *,
    burn_subtitles: bool = False,
) -> dict:
    """Generate platform-specific exports (9:16, 1:1, 4:5) from the master trailer.
    If burn_subtitles and a VO exists, hardcodes captions from the VO script.
    Saves under outputs/<run>/exports/."""
    # Read snapshot under lock — no mutation until ffmpeg work finishes.
    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        if not state.get("final"):
            raise RuntimeError("trailer not stitched yet — run stitch first")
        run_dir = pipeline._run_dir(run_id)
        master = run_dir / state["final"]
        if not master.exists():
            raise RuntimeError(f"master trailer file missing: {master.name}")
        exports_dir = run_dir / "exports"
        exports_dir.mkdir(exist_ok=True)
        vo_meta = (state.get("audio") or {}).get("vo") or {}

    # Optionally build an SRT from the VO script (file IO, no state mutation).
    srt_path = None
    if burn_subtitles and vo_meta.get("status") == "ready":
        srt_content = subs_mod.build_srt_from_vo(vo_meta, run_dir)
        if srt_content:
            srt_path = exports_dir / "subtitles.srt"
            srt_path.write_text(srt_content, encoding="utf-8")
            logger.info(run_id, "export", f"built SRT from VO ({len(srt_content.splitlines())} lines)")
        else:
            logger.warn(run_id, "export", "no VO lines to caption; burn-in skipped")
            srt_path = None

    results: dict[str, str] = {}
    for preset in presets:
        if preset not in video._PLATFORM_VARIANTS:
            logger.warn(run_id, "export", f"unknown preset: {preset}")
            continue

        # First reframe to preset
        out = exports_dir / f"trailer_{preset}.mp4"
        logger.info(run_id, "export", f"reframing to {preset}{' + captions' if srt_path else ''}…")
        try:
            await video.reframe_to_platform(master, out, preset)
            # Then optionally burn subtitles
            if srt_path:
                burnt = exports_dir / f"trailer_{preset}_cc.mp4"
                await subs_mod.burn_in(out, srt_path, burnt, font_size=24 if preset != "9x16" else 28)
                # Replace plain export with burnt-in version
                burnt.replace(out)
            rel = f"exports/{out.name}"
            results[preset] = rel
            logger.success(run_id, "export", f"✓ {preset} ({out.stat().st_size // 1024} KB)")
        except Exception as e:
            logger.error(run_id, "export", f"✗ {preset} failed: {e}")

    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        state["exports"] = {**(state.get("exports") or {}), **results}
        if srt_path:
            state["exports"]["_srt"] = f"exports/{srt_path.name}"
        pipeline._save_state(run_id, state)
    return results


# ─── Subtitles (standalone SRT/VTT from VO script) ──────────────────────

def build_subtitle_file(run_id: str, fmt: str = "srt") -> str:
    """Generate (or regenerate) the subtitle file for a run based on its current VO.
    Returns the path relative to the run dir."""
    state = pipeline.get_state(run_id)
    vo_meta = (state.get("audio") or {}).get("vo") or {}
    if vo_meta.get("status") != "ready":
        raise RuntimeError("no ready VO to caption — generate + synthesize VO first")
    run_dir = pipeline._run_dir(run_id)
    exports_dir = run_dir / "exports"
    exports_dir.mkdir(exist_ok=True)

    if fmt == "vtt":
        content = subs_mod.build_webvtt_from_vo(vo_meta, run_dir)
        out = exports_dir / "subtitles.vtt"
    else:
        content = subs_mod.build_srt_from_vo(vo_meta, run_dir)
        out = exports_dir / "subtitles.srt"
    if not content:
        raise RuntimeError("VO script has no lines to caption")
    out.write_text(content, encoding="utf-8")
    return f"exports/{out.name}"
