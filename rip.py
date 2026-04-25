"""Rip-o-matic phase: translate a source trailer into a new one.

Flow:
  1. Upload source video (done at API layer before calling create_rip_run).
  2. Scene-detect + segment into shot-sized clips.
  3. Claude translates each segment (+ first-frame image) into a shot of the
     user's concept, preserving the source's camera grammar.
  4. Wire segment video as per-shot video_ref so Seedance matches pacing.

Extracted from pipeline.py. Imports pipeline for shared state primitives.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Optional

import logger
import pipeline
import storyboard
import textutils
import video


async def preview_rip_segments(
    run_id: str,
    *,
    scene_threshold: float = 0.30,
    min_shots: int = 4,
    max_shots: int = 10,
    min_segment_s: float = 2.5,
    max_segment_s: float = 12.0,
) -> dict:
    """Re-segment the source with new thresholds. Overwrites previous segments.
    Used BEFORE Claude translates — lets the user dial in the segmentation to match
    the source's actual cut density."""
    state = pipeline.get_state(run_id)
    source = state.get("source_video") or {}
    source_path_rel = source.get("path")
    if not source_path_rel:
        raise RuntimeError("no source video uploaded")
    run_dir = pipeline._run_dir(run_id)
    source_path = run_dir / source_path_rel

    logger.info(run_id, "rip", f"re-segmenting with threshold={scene_threshold}, bounds=[{min_segment_s}s, {max_segment_s}s], count=[{min_shots}, {max_shots}]")

    full_cuts = await asyncio.to_thread(video.ffmpeg_scene_detect, source_path, threshold=scene_threshold)
    segments_dir = run_dir / "source" / "segments"
    if segments_dir.exists():
        shutil.rmtree(segments_dir)
    segs = await video.scene_detect_and_segment(
        source_path, segments_dir,
        min_shots=min_shots, max_shots=max_shots,
        min_segment_s=min_segment_s, max_segment_s=max_segment_s,
        scene_threshold=scene_threshold,
    )
    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        state["source_video"]["cut_timeline"] = full_cuts
        state["source_video"]["segments"] = [
            {
                "idx": s["idx"], "start": s["start"], "end": s["end"],
                "duration": s["duration"],
                "path": str(s["path"].relative_to(run_dir)),
                "first_frame_path": str(s["first_frame_path"].relative_to(run_dir)) if s.get("first_frame_path") else None,
            }
            for s in segs
        ]
        state["segmentation_params"] = {
            "scene_threshold": scene_threshold,
            "min_shots": min_shots, "max_shots": max_shots,
            "min_segment_s": min_segment_s, "max_segment_s": max_segment_s,
        }
        pipeline._save_state(run_id, state)
    logger.success(run_id, "rip", f"✓ re-segmented → {len(segs)} segments, {len(full_cuts)} cuts detected in source")
    return {"segments": state["source_video"]["segments"], "cut_count": len(full_cuts)}


async def create_rip_run(
    *,
    source_filename: str,
    source_data: bytes,
    concept: str,
    ratio: str = "16:9",
    style_intent: str = "",
    title: str = "",
    crossfade: bool = False,
    reference_files: Optional[list[tuple[str, bytes]]] = None,
    variants_per_scene: int = 1,
    run_id: Optional[str] = None,
    cast_slugs: Optional[list[str]] = None,
) -> str:
    """Upload a source trailer, scene-detect it, ask Claude to translate each segment
    into a shot of the user's concept, and wire up per-shot video_ref + composition_ref
    to preserve the source's camera grammar.

    `variants_per_scene` (1-3): how many alternate takes to render per scene. Higher
    gives the cut plan more material to intercut for matching rapid source rhythm."""
    variants_per_scene = max(1, min(3, int(variants_per_scene or 1)))
    # Step 1: create a run (or reuse an existing stub from _bg_rip_upload)
    if run_id:
        run_dir = pipeline._run_dir(run_id)
        _s = pipeline.get_state(run_id)
        _s["params"]["variants_per_scene"] = variants_per_scene
        # Store reference files into the existing stub's directory
        stored_refs = []
        if reference_files:
            refs_dir = run_dir / "references"
            refs_dir.mkdir(exist_ok=True)
            for i, (name, data) in enumerate(reference_files):
                ext = Path(name).suffix.lower() or ".png"
                if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                    ext = ".png"
                dst = refs_dir / f"user_ref_{i+1:02d}{ext}"
                dst.write_bytes(data)
                stored_refs.append(f"references/{dst.name}")
        _s["references"] = stored_refs
        pipeline._save_state(run_id, _s)
    else:
        run_id = pipeline.create_run(
            concept=concept,
            num_shots=3,  # placeholder — overwritten below
            shot_duration=5,
            ratio=ratio,
            style_intent=style_intent,
            title=title,
            crossfade=crossfade,
            reference_files=reference_files,
            cast_slugs=cast_slugs,
        )
        _s = pipeline.get_state(run_id)
        _s["params"]["variants_per_scene"] = variants_per_scene
        pipeline._save_state(run_id, _s)
        run_dir = pipeline._run_dir(run_id)

    # Step 2: save source video
    source_dir = run_dir / "source"
    source_dir.mkdir(exist_ok=True)
    src_suffix = Path(source_filename).suffix.lower() or ".mp4"
    source_path = source_dir / f"trailer{src_suffix}"
    source_path.write_bytes(source_data)

    source_duration = video.probe_duration(source_path)
    if source_duration < 3.0:
        state = pipeline.get_state(run_id)
        state["status"] = "failed"
        state["error"] = f"source video too short ({source_duration:.1f}s) — need at least 3s for scene detection"
        pipeline._save_state(run_id, state)
        raise RuntimeError(f"source video too short ({source_duration:.1f}s)")

    state = pipeline.get_state(run_id)
    state["status"] = "ripping"
    state["source_video"] = {
        "path": f"source/{source_path.name}",
        "filename": source_filename,
        "duration": source_duration,
    }
    pipeline._save_state(run_id, state)
    logger.info(run_id, "rip", f"source trailer saved ({source_duration:.1f}s). running ffmpeg scene detection…")

    # Step 3: scene detect + segment. Also capture the FULL cut timeline of the
    # source (every cut, not just the grouped ones) for timeline-based editing later.
    segments_dir = source_dir / "segments"
    try:
        full_cuts = await asyncio.to_thread(video.ffmpeg_scene_detect, source_path, threshold=0.28)
        state = pipeline.get_state(run_id)
        state["source_video"]["cut_timeline"] = full_cuts
        pipeline._save_state(run_id, state)
        logger.info(run_id, "rip", f"detected {len(full_cuts)} source cut points (avg {state['source_video']['duration']/max(1,len(full_cuts)):.1f}s between)")
        segs = await video.scene_detect_and_segment(source_path, segments_dir)
    except Exception as e:
        state = pipeline.get_state(run_id)
        state["status"] = "failed"
        state["error"] = f"scene detect failed: {e}"
        pipeline._save_state(run_id, state)
        logger.error(run_id, "rip", f"✗ scene detect failed: {e}")
        raise

    if not segs:
        state = pipeline.get_state(run_id)
        state["status"] = "failed"
        state["error"] = "scene detection produced no segments"
        pipeline._save_state(run_id, state)
        logger.error(run_id, "rip", "✗ no segments produced")
        raise RuntimeError("no segments")
    preview = ", ".join(f"{s['duration']:.1f}s" for s in segs[:5])
    suffix = "…" if len(segs) > 5 else ""
    logger.success(run_id, "rip", f"✓ {len(segs)} segments extracted — {preview}{suffix}")

    # Persist segments info with relative paths
    state = pipeline.get_state(run_id)
    state["source_video"]["segments"] = [
        {
            "idx": s["idx"],
            "start": s["start"],
            "end": s["end"],
            "duration": s["duration"],
            "path": str(s["path"].relative_to(run_dir)),
            "first_frame_path": str(s["first_frame_path"].relative_to(run_dir)) if s.get("first_frame_path") else None,
        }
        for s in segs
    ]
    state["params"]["num_shots"] = len(segs)
    state["status"] = "translating"
    pipeline._save_state(run_id, state)
    logger.info(run_id, "rip", "Claude is translating source segments into a new storyboard…")

    # Step 4: Claude translates segments → storyboard
    # Convert relative paths back to absolute for Claude vision
    claude_segments = []
    for s in state["source_video"]["segments"]:
        claude_segments.append({
            "idx": s["idx"],
            "duration": s["duration"],
            "first_frame_path": (run_dir / s["first_frame_path"]) if s.get("first_frame_path") else None,
        })

    try:
        story = await asyncio.to_thread(
            storyboard.generate_from_source,
            concept=concept,
            segments=claude_segments,
            ratio=ratio,
            style_intent=style_intent,
            title_hint=title,
            run_id=run_id,
            cast=state.get("cast") or None,
            locations=state.get("locations") or None,
            props=state.get("props") or None,
        )
    except Exception as e:
        state = pipeline.get_state(run_id)
        state["status"] = "failed"
        state["error"] = f"translation failed: {e}"
        pipeline._save_state(run_id, state)
        logger.error(run_id, "rip", f"✗ translation failed: {e}")
        raise

    # Step 5: wire up keyframe/shot slots with segment refs
    state = pipeline.get_state(run_id)
    n = len(state["source_video"]["segments"])
    state["story"] = story
    state["keyframes"] = [
        {
            "idx": i, "path": None, "status": "pending", "error": None,
            "prompt_override": None, "updated_at": None,
            "composition_ref": state["source_video"]["segments"][i]["first_frame_path"],
        }
        for i in range(n)
    ]
    shots: list[dict] = []
    for i in range(n):
        shots.append({
            "idx": i, "path": None, "status": "pending", "error": None,
            "prompt_override": None, "updated_at": None,
            # Auto-attach source segment as the shot's video_ref — reused by run_shot
            "video_ref": {
                "path": state["source_video"]["segments"][i]["path"],
                "filename": f"source_seg_{i+1:02d}.mp4",
                "duration": state["source_video"]["segments"][i]["duration"],
                "trimmed_from": None,
                "size": None,
                "width": None,
                "height": None,
                "attached_at": textutils.now_iso(),
                "auto_attached": True,
            },
            "variants": [
                {"idx": v, "path": None, "seed": None, "status": "pending", "error": None, "updated_at": None}
                for v in range(variants_per_scene)
            ],
            "primary_variant": 0,
        })
    state["shots"] = shots
    state["status"] = "storyboard_ready"
    state["rip_mode"] = True
    pipeline._save_state(run_id, state)
    (run_dir / "storyboard.json").write_text(json.dumps(story, indent=2))
    logger.success(run_id, "rip", f"✓ translated storyboard — {story.get('title', '(no title)')} — ready for review")

    return run_id
