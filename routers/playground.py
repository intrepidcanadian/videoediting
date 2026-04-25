"""Seedance playground endpoints.

Separate from the runs cluster because clips have no storyboard / phase state
and don't belong in the Runs list. One flat kind: generate → poll → consume.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import playground as playground_mod

router = APIRouter(tags=["playground"])

# Clip IDs follow pipeline's run_id shape. Reject anything else at the URL layer.
_CLIP_ID_RE = re.compile(r"^clip_[0-9]{8}_[0-9]{6}_[a-zA-Z0-9_-]{1,40}$")


def _check_clip_id(clip_id: str) -> str:
    if not _CLIP_ID_RE.match(clip_id):
        raise HTTPException(400, "invalid clip_id")
    return clip_id


# Max bytes per reference image — matches the server-wide cap.
_MAX_REF_BYTES = 10 * 1024 * 1024
# Max bytes per reference video. Higher than image since we normalize to <15s
# + 720p on disk anyway, but we still want a ceiling so a 2 GB upload doesn't
# exhaust memory before the server gets to say no.
_MAX_VREF_BYTES = 300 * 1024 * 1024


@router.get("/api/playground/clips")
def api_list_clips():
    """Return every playground clip, newest first."""
    return {"clips": playground_mod.list_clips()}


@router.get("/api/playground/clips/{clip_id}")
def api_get_clip(clip_id: str):
    _check_clip_id(clip_id)
    try:
        return playground_mod.get_meta(clip_id)
    except FileNotFoundError:
        raise HTTPException(404, f"clip not found: {clip_id}")


@router.delete("/api/playground/clips/{clip_id}")
def api_delete_clip(clip_id: str):
    _check_clip_id(clip_id)
    try:
        playground_mod.delete_clip(clip_id)
    except FileNotFoundError:
        raise HTTPException(404, f"clip not found: {clip_id}")
    return {"ok": True}


@router.post("/api/playground/generate")
async def api_generate_clip(
    prompt: str = Form(...),
    ratio: str = Form("16:9"),
    duration: int = Form(5),
    quality: str = Form("standard"),
    generate_audio: bool = Form(False),
    reference_images: Optional[list[UploadFile]] = File(None),
    reference_videos: Optional[list[UploadFile]] = File(None),
):
    """Kick off a Seedance render. Returns a clip_id immediately; the UI polls
    /api/playground/clips/{clip_id} to watch the status transition from
    `queued` → `generating` → `ready` | `failed`.

    Reference videos are stored as uploaded bytes here and normalized (trimmed
    to ≤15s + downscaled to 720p + re-encoded H.264) by the background task
    before the Seedance call — ffmpeg work never happens on the request path."""
    # Magic-byte check — reuse the sniffer from the main server module so we
    # have one consistent upload-validation policy across the app.
    from server import _require_kind

    refs: list[tuple[str, bytes]] = []
    total_img = 0
    for up in reference_images or []:
        if not up or not up.filename:
            continue
        data = await up.read()
        if not data:
            continue
        if len(data) > _MAX_REF_BYTES:
            raise HTTPException(
                413, f"reference image '{up.filename}' exceeds {_MAX_REF_BYTES // (1024*1024)} MB limit",
            )
        total_img += len(data)
        if total_img > _MAX_REF_BYTES * 3:
            raise HTTPException(413, "total reference image size too large")
        _require_kind(data, "image", field=f"reference image '{up.filename}'")
        refs.append((up.filename, data))

    vrefs: list[tuple[str, bytes]] = []
    total_vid = 0
    for up in reference_videos or []:
        if not up or not up.filename:
            continue
        data = await up.read()
        if not data:
            continue
        if len(data) > _MAX_VREF_BYTES:
            raise HTTPException(
                413, f"reference video '{up.filename}' exceeds {_MAX_VREF_BYTES // (1024*1024)} MB limit",
            )
        total_vid += len(data)
        if total_vid > _MAX_VREF_BYTES * 2:
            raise HTTPException(413, "total reference video size too large")
        _require_kind(data, "video", field=f"reference video '{up.filename}'")
        if len(vrefs) >= 3:
            # Seedance API hard-caps at 3 video refs per task; silently drop extras.
            break
        vrefs.append((up.filename, data))

    try:
        clip_id = await playground_mod.generate_clip(
            prompt=prompt,
            reference_images=refs or None,
            reference_videos=vrefs or None,
            ratio=ratio,
            duration=int(duration),
            quality=quality,
            generate_audio=bool(generate_audio),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"clip_id": clip_id}


@router.post("/api/playground/generate-image")
async def api_generate_image(
    prompt: str = Form(...),
    reference_images: Optional[list[UploadFile]] = File(None),
):
    """Kick off a Nano Banana (Gemini 2.5 Flash Image) render. Returns a
    clip_id immediately; the UI polls /api/playground/clips/{clip_id} — same
    polling path as video, different shape (image_path instead of video_path).

    Image renders are an order of magnitude faster than video (5-15s vs 60-180s),
    so the UX feels closer to an image editor than a trailer pipeline."""
    from server import _require_kind

    refs: list[tuple[str, bytes]] = []
    total = 0
    for up in reference_images or []:
        if not up or not up.filename:
            continue
        data = await up.read()
        if not data:
            continue
        if len(data) > _MAX_REF_BYTES:
            raise HTTPException(
                413, f"reference image '{up.filename}' exceeds {_MAX_REF_BYTES // (1024*1024)} MB limit",
            )
        total += len(data)
        if total > _MAX_REF_BYTES * 4:
            raise HTTPException(413, "total reference image size too large")
        _require_kind(data, "image", field=f"reference image '{up.filename}'")
        refs.append((up.filename, data))

    try:
        clip_id = await playground_mod.generate_image(
            prompt=prompt,
            reference_images=refs or None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"clip_id": clip_id}


@router.post("/api/playground/clips/{clip_id}/promote")
def api_promote_clip(
    clip_id: str,
    kind: str = Form("characters"),
    name: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
):
    """Copy a ready playground image into the cross-run library.

    Skips the download-and-reupload roundtrip the user would otherwise do
    by hand: the PNG lives on disk already, we just stream its bytes into
    `library.save_item` which handles slug generation + dedupe + meta.

    Image-kind clips only. Video clips don't map to any library kind cleanly
    (library.characters/locations/music/looks) so we reject those up front."""
    _check_clip_id(clip_id)

    try:
        meta = playground_mod.get_meta(clip_id)
    except FileNotFoundError:
        raise HTTPException(404, f"clip not found: {clip_id}")

    if meta.get("kind") != "image":
        raise HTTPException(400, "Only image-kind clips can be promoted to the library")
    if meta.get("status") != "ready":
        raise HTTPException(409, f"clip not ready (status={meta.get('status')})")

    image_rel = meta.get("image_path")
    if not image_rel:
        raise HTTPException(500, "clip meta missing image_path")

    clip_dir = playground_mod.PLAYGROUND_ROOT / clip_id
    image_path = (clip_dir / image_rel).resolve()
    if not image_path.is_relative_to(clip_dir.resolve()) or not image_path.exists():
        raise HTTPException(404, "clip image missing on disk")

    import library as library_mod
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    try:
        result = library_mod.save_item(
            kind,
            name=name,
            description=description or meta.get("prompt", "")[:500],
            tags=tag_list,
            files=[(image_path.name, image_path.read_bytes())],
            # Stash provenance so the library knows this came from the playground.
            extra={"source": {"type": "playground", "clip_id": clip_id, "prompt": meta.get("prompt")}},
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "item": result}


@router.get("/playground-assets/{clip_id}/{path:path}")
def api_playground_asset(clip_id: str, path: str):
    """Serve a playground clip's video or reference image. Path-traversal
    guarded identically to /assets/ for the main runs."""
    _check_clip_id(clip_id)
    base = playground_mod.PLAYGROUND_ROOT.resolve()
    target = (base / clip_id / path).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(400, "invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "asset not found")
    return FileResponse(target)
