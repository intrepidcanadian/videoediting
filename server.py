"""FastAPI server for the trailer review UI.

Design notes
────────────
Every phase is an explicit endpoint. Long-running generations (keyframes / shots /
stitch) are fire-and-forget background tasks — the UI polls GET /api/runs/{id}
every 2s and re-reads the per-item status to draw spinners / errors / artefacts.

There is NO implicit progression. The UI must explicitly call each phase. That is
the whole point of "review-as-you-go": no automatic march from storyboard → shots.

Run artefacts live at outputs/<run_id>/... and are served back via /assets/<run_id>/...
for inline <img>/<video> playback.
"""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import audio as audio_mod
import costs
import director as director_mod
import errors as trailer_errors
import ideate
import library as library_mod
import logger
import pipeline
import prompt_rules

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
OUTPUT_ROOT = pipeline.OUTPUT_ROOT

from constants import (
    MAX_PROMPT_LEN, MAX_REF_IMAGE_BYTES, MAX_REF_IMAGES_TOTAL_BYTES,
    MAX_VIDEO_UPLOAD_BYTES, MAX_AUDIO_UPLOAD_BYTES,
)


# ─── Pydantic request models ─────────────────────────────────────────────

class FaceLockPayload(BaseModel):
    reference_idx: int = Field(default=0, ge=0)

class SweepPayload(BaseModel):
    n: int = Field(default=3, ge=2, le=5)

class ExportPayload(BaseModel):
    presets: list[Literal["9x16", "1x1", "4x5", "16x9"]] = Field(min_length=1)
    burn_subtitles: bool = False

class SubtitlePayload(BaseModel):
    format: Literal["srt", "vtt"] = "srt"

class SetLookPayload(BaseModel):
    look: str = "none"

class DirectorMessagePayload(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_PROMPT_LEN)

class VoScriptPayload(BaseModel):
    vibe: Optional[str] = None

class UpdateVoScriptPayload(BaseModel):
    lines: list = Field(default_factory=list)
    voice_id: Optional[str] = None

class ComposeMusicPayload(BaseModel):
    vibe: str = ""


_bg_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_env()
    yield
    # Cancel any still-running background tasks so the server can shut down cleanly
    pending = [t for t in list(_bg_tasks) if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            print(f"[server] shutdown: {len(pending)} background tasks did not finish within 10s", file=sys.stderr)


app = FastAPI(title="Trailer Maker", lifespan=lifespan)

# Sub-routers live in routers/. See routers/__init__.py for the extraction rationale.
from routers import library_router, misc_router, playground_router
app.include_router(library_router)
app.include_router(misc_router)
app.include_router(playground_router)


@app.exception_handler(trailer_errors.TrailerError)
async def _trailer_error_handler(request, exc):
    """Map domain-specific errors to HTTP status codes. Loose RuntimeError usage
    in existing code still maps via each endpoint's own try/except; this handler
    only fires for the structured TrailerError hierarchy."""
    if isinstance(exc, trailer_errors.TrailerNotFound):
        status = 404
    elif isinstance(exc, trailer_errors.TrailerUserError):
        status = 400
    elif isinstance(exc, trailer_errors.TrailerNotReady):
        status = 409
    elif isinstance(exc, trailer_errors.ExternalServiceError):
        status = 502
    else:
        status = 500
    return JSONResponse(status_code=status, content={"detail": str(exc)[:500]})


_RUN_ID_RE = __import__("re").compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
_ASSET_ID_RE = __import__("re").compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _check_run_id(run_id: str) -> str:
    """Boundary check — reject anything that could escape OUTPUT_ROOT before it
    reaches pipeline. Complements pipeline.validate_run_id (defense in depth)."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise HTTPException(400, f"invalid run_id")
    return run_id


@app.middleware("http")
async def _run_id_boundary(request, call_next):
    # Match any path segment after /api/runs/ or /assets/ that should be a run_id.
    parts = request.url.path.split("/")
    rid_idx = None
    if len(parts) >= 4 and parts[1] == "api" and parts[2] == "runs":
        rid_idx = 3
    elif len(parts) >= 3 and parts[1] == "assets":
        rid_idx = 2
    if rid_idx is not None and parts[rid_idx]:
        if not _RUN_ID_RE.match(parts[rid_idx]):
            return JSONResponse(status_code=400, content={"detail": "invalid run_id"})
    return await call_next(request)


def _safe_int(val, default: int) -> int:
    try:
        return int(val or default)
    except (ValueError, TypeError):
        return default


# ─── File-type sniffing ──────────────────────────────────────────────────
# Magic byte signatures so we don't trust the client-declared MIME type.
# Keys are the category we care about; values are (offset, byte-sequence) pairs
# that must match for the file to be recognized.

_IMAGE_SIGS = (
    (0, b"\x89PNG\r\n\x1a\n"),            # PNG
    (0, b"\xff\xd8\xff"),                  # JPEG (any variant)
    (0, b"GIF87a"), (0, b"GIF89a"),        # GIF
    (0, b"RIFF"),                          # WEBP (needs also "WEBP" at offset 8)
    (0, b"BM"),                            # BMP (used occasionally)
)

_VIDEO_SIGS = (
    (4, b"ftyp"),                          # MP4 / MOV family (ISO base-media)
    (0, b"\x1a\x45\xdf\xa3"),              # Matroska / WEBM
    (0, b"RIFF"),                          # AVI (RIFF...AVI )
)

_AUDIO_SIGS = (
    (0, b"ID3"),                           # MP3 with ID3
    (0, b"\xff\xfb"), (0, b"\xff\xf3"), (0, b"\xff\xf2"),  # MP3 frame header
    (0, b"RIFF"),                          # WAV (RIFF...WAVE)
    (0, b"fLaC"),                          # FLAC
    (0, b"OggS"),                          # Ogg Vorbis / Opus
    (4, b"ftyp"),                          # M4A (same ISO base-media container as MP4)
)


def _matches_any(head: bytes, sigs: tuple) -> bool:
    for offset, sig in sigs:
        if len(head) >= offset + len(sig) and head[offset:offset + len(sig)] == sig:
            return True
    return False


from imgutils import HEIC_BRANDS as _HEIC_BRANDS

_M4A_BRANDS = {b"M4A ", b"M4B "}


def _sniff_kind(data: bytes) -> str:
    """Return 'image' / 'video' / 'audio' / 'unknown' based on magic bytes.
    Only inspects the first 32 bytes so it's cheap and safe to call on large uploads."""
    head = data[:32]
    if not head:
        return "unknown"
    # Disambiguate RIFF: image/webp vs video/avi vs audio/wav by the 4-byte form type at offset 8.
    if head[:4] == b"RIFF" and len(head) >= 12:
        form = head[8:12]
        if form == b"WEBP":
            return "image"
        if form == b"AVI ":
            return "video"
        if form == b"WAVE":
            return "audio"
        return "unknown"
    # Disambiguate ISO base-media (ftyp): HEIC/AVIF image vs M4A audio vs MP4/MOV video.
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIC_BRANDS:
            return "image"
        if brand in _M4A_BRANDS:
            return "audio"
        return "video"
    if _matches_any(head, _IMAGE_SIGS):
        return "image"
    if _matches_any(head, _VIDEO_SIGS):
        return "video"
    if _matches_any(head, _AUDIO_SIGS):
        return "audio"
    return "unknown"


def _require_kind(data: bytes, expected: str, field: str = "file") -> None:
    """Raise HTTPException(400) if the upload's magic bytes don't match `expected`.
    `expected` is 'image' / 'video' / 'audio'."""
    kind = _sniff_kind(data)
    if kind != expected:
        raise HTTPException(
            400,
            f"{field} content does not match declared type (expected {expected}, got {kind})",
        )


def _is_heic(data: bytes) -> bool:
    import imgutils
    return imgutils.is_heic(data)


def _convert_heic_to_jpeg(data: bytes) -> tuple[bytes, str]:
    """Convert HEIC/AVIF bytes to JPEG. Returns (jpeg_bytes, new_filename_ext)."""
    import imgutils
    return imgutils.convert_heic_to_jpeg(data)


def _validate_prompt(text: Optional[str], field_name: str = "prompt") -> Optional[str]:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_PROMPT_LEN:
        raise HTTPException(400, f"{field_name} too long ({len(text)} chars, max {MAX_PROMPT_LEN})")
    return text


# ─── Continuity QC ───────────────────────────────────────────────────────

@app.post("/api/runs/{run_id}/continuity")
async def api_continuity_check(run_id: str):
    """Run Claude-vision pair-by-pair continuity check over adjacent shots."""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    async def _do():
        try:
            await pipeline.run_continuity_check(run_id)
        except Exception as e:
            _log_bg_error(run_id, "continuity", f"background continuity failed: {e}")

    _bg(_do(), run_id=run_id, op="continuity")
    return {"ok": True, "run_id": run_id}


# ─── Face lock (post-gen identity enforcement) ─────────────────────────

@app.post("/api/runs/{run_id}/keyframes/{idx}/face-lock")
async def api_face_lock_one(run_id: str, idx: int, payload: FaceLockPayload = FaceLockPayload()):
    """Run face lock on one keyframe. Body: {"reference_idx": int} (optional)."""
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    kfs = state.get("keyframes") or []
    if idx < 0 or idx >= len(kfs):
        raise HTTPException(400, f"keyframe idx {idx} out of range (have {len(kfs)})")
    ref_idx = payload.reference_idx
    if ref_idx >= len(kfs):
        raise HTTPException(400, f"reference_idx {ref_idx} out of range (have {len(kfs)} keyframes)")

    async def _do():
        try:
            await pipeline.lock_face_on_keyframe(run_id, idx, reference_idx=ref_idx)
        except Exception as e:
            _log_bg_error(run_id, "keyframes", f"background face-lock failed for kf {idx}: {e}")

    _bg(_do(), run_id=run_id, op="keyframes")
    return {"ok": True, "idx": idx, "reference_idx": ref_idx}


@app.post("/api/runs/{run_id}/keyframes/face-lock-all")
async def api_face_lock_all(run_id: str, payload: FaceLockPayload = FaceLockPayload()):
    """Batch face-lock every ready keyframe."""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    ref_idx = payload.reference_idx

    async def _do():
        try:
            await pipeline.lock_face_on_all_keyframes(run_id, reference_idx=ref_idx)
        except Exception as e:
            _log_bg_error(run_id, "keyframes", f"background batch face-lock failed: {e}")

    _bg(_do(), run_id=run_id, op="keyframes")
    return {"ok": True, "reference_idx": ref_idx}


# ─── Animatic mode (pre-render preview) ─────────────────────────────────

@app.post("/api/runs/{run_id}/animatic")
async def api_build_animatic(run_id: str):
    """Build a Ken-Burns animatic from keyframes + music + VO. No Seedance."""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    async def _do():
        try:
            await pipeline.build_animatic(run_id)
        except Exception as e:
            _log_bg_error(run_id, "animatic", f"{e}")

    _bg(_do(), run_id=run_id, op="animatic")
    return {"ok": True, "run_id": run_id}


# Taste endpoints moved to routers/misc.py.


# ─── Shot prompt sweep ──────────────────────────────────────────────────

@app.post("/api/runs/{run_id}/shots/{idx}/sweep")
async def api_sweep_shot(run_id: str, idx: int, payload: SweepPayload = SweepPayload()):
    """Generate N distinct motion prompts for one shot + render each as a variant."""
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    shots = state.get("shots") or []
    if idx < 0 or idx >= len(shots):
        raise HTTPException(400, f"shot idx {idx} out of range (have {len(shots)})")
    n = payload.n

    async def _do():
        try:
            await pipeline.sweep_shot_prompts(run_id, idx, n=n)
        except Exception as e:
            _log_bg_error(run_id, "sweep", f"background sweep failed for shot {idx}: {e}")

    _bg(_do(), run_id=run_id, op="sweep")
    return {"ok": True, "run_id": run_id, "idx": idx, "n": n}


# ─── Platform exports (9:16 / 1:1 / 4:5 from master) ────────────────────
# GET /api/platform-variants moved to routers/misc.py.

@app.post("/api/runs/{run_id}/export")
async def api_export_platform_variants(run_id: str, payload: ExportPayload):
    """Generate platform variants from the master trailer.
    Body: {"presets": ["9x16", "1x1", "4x5"], "burn_subtitles": bool}"""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    presets = payload.presets
    burn = payload.burn_subtitles

    async def _do():
        try:
            await pipeline.export_platform_variants(run_id, presets, burn_subtitles=burn)
        except Exception as e:
            _log_bg_error(run_id, "export", f"background export failed: {e}")

    _bg(_do(), run_id=run_id, op="export")
    return {"ok": True, "presets": presets, "burn_subtitles": burn}


@app.post("/api/runs/{run_id}/subtitles")
def api_build_subtitles(run_id: str, payload: SubtitlePayload = SubtitlePayload()):
    """Build an SRT (or VTT) from the run's VO script. Returns relative path."""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    fmt = payload.format
    try:
        rel = pipeline.build_subtitle_file(run_id, fmt=fmt)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"path": rel}


# ─── Library (cross-run assets) ─────────────────────────────────────────
# Library CRUD + /library-assets/* moved to routers/library.py.
# Per-run inject/promote stay here because they take {run_id}.

@app.post("/api/runs/{run_id}/library/inject")
async def api_library_inject(
    run_id: str,
    kind: str = Form(...),
    slug: str = Form(...),
    target: str = Form("references"),
):
    """Copy a library item's files into the run (as references / refs etc)."""
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    run_dir = pipeline._run_dir(run_id)
    try:
        copied = library_mod.inject_into_run(
            run_root=run_dir, kind=kind, slug=slug, target_dir=target,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    # If injecting into references, update state.references list
    if target == "references":
        async with pipeline._get_lock(run_id):
            state = pipeline.get_state(run_id)
            state["references"] = list(state.get("references") or []) + copied
            pipeline._save_state(run_id, state)
    return {"ok": True, "copied": copied}


@app.post("/api/runs/{run_id}/library/promote")
async def api_library_promote(
    run_id: str,
    kind: str = Form(...),
    name: str = Form(...),
    file_rel_paths: str = Form(...),  # comma-separated
    description: str = Form(""),
    tags: str = Form(""),
):
    """Save run-scope files (references, assets, music) to the shared library."""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    run_dir = pipeline._run_dir(run_id)
    paths = [p.strip() for p in file_rel_paths.split(",") if p.strip()]
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    try:
        meta = library_mod.promote_from_run(
            run_root=run_dir, kind=kind, name=name,
            file_rel_paths=paths, description=description, tags=tag_list,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return meta


# ─── Looks / color grades ───────────────────────────────────────────────

# GET /api/looks moved to routers/misc.py.

@app.put("/api/runs/{run_id}/look")
async def api_set_look(run_id: str, payload: SetLookPayload):
    """Set the look for a run. Applied at stitch time."""
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    look_id = payload.look
    import looks as looks_mod
    if look_id not in looks_mod.LOOKS:
        raise HTTPException(400, f"unknown look: {look_id}")
    async with pipeline._get_lock(run_id):
        state = pipeline.get_state(run_id)
        state["params"]["look"] = look_id
        pipeline._save_state(run_id, state)
    try:
        import taste as _taste
        _taste.record("look_pick", run_id=run_id, look=look_id)
    except Exception as e:
        logger.warn(run_id, "taste", f"failed to record look_pick: {e}")
    return {"ok": True, "look": look_id}


# ─── Director conversation ──────────────────────────────────────────────

@app.post("/api/runs/{run_id}/director")
async def api_director_message(run_id: str, payload: DirectorMessagePayload):
    """Send a message to the director agent. Returns reply + any tool actions taken."""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    message = payload.message.strip()
    try:
        result = await director_mod.handle_message(run_id, message)
    except Exception as e:
        logger.error(run_id, "director", f"director failed: {e}")
        import traceback; traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, "director failed — check server logs")
    return result


@app.get("/api/runs/{run_id}/director")
def api_director_history(run_id: str):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"messages": director_mod.get_conversation(run_id)}


@app.delete("/api/runs/{run_id}/director")
def api_director_reset(run_id: str):
    try:
        director_mod.reset_conversation(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"ok": True}


# ─── Audio / VO ──────────────────────────────────────────────────────────
# GET /api/audio/status moved to routers/misc.py.

@app.post("/api/runs/{run_id}/vo/script")
async def api_generate_vo_script(run_id: str, payload: VoScriptPayload = VoScriptPayload()):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    vibe = payload.vibe
    try:
        vo = await pipeline.generate_vo_script(run_id, vibe=vibe)
    except Exception as e:
        logger.error(run_id, "vo", f"VO script failed: {e}")
        import traceback; traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, "VO script generation failed — check server logs")
    return {"vo": vo}


@app.put("/api/runs/{run_id}/vo/script")
async def api_update_vo_script(run_id: str, payload: UpdateVoScriptPayload):
    try:
        vo = await pipeline.update_vo_script(
            run_id,
            lines=payload.lines,
            voice_id=payload.voice_id,
        )
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"vo": vo}


@app.post("/api/runs/{run_id}/vo/synthesize")
async def api_synthesize_vo(run_id: str):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    async def _do():
        try:
            await pipeline.synthesize_vo(run_id)
        except Exception as e:
            _log_bg_error(run_id, "vo", f"background VO synthesis failed: {e}")
    _bg(_do(), run_id=run_id, op="vo")
    return {"ok": True, "run_id": run_id}


@app.delete("/api/runs/{run_id}/vo")
async def api_remove_vo(run_id: str):
    try:
        await pipeline.remove_vo(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"ok": True}


# ─── Prompt rules ────────────────────────────────────────────────────────
# Rules endpoints moved to routers/misc.py.


# ─── Run management ──────────────────────────────────────────────────────

@app.get("/api/runs")
def api_list_runs():
    return {"runs": pipeline.list_runs()}


@app.post("/api/runs")
async def api_create_run(
    concept: str = Form(...),
    num_shots: int = Form(6),
    shot_duration: int = Form(5),
    ratio: str = Form("16:9"),
    style_intent: str = Form(""),
    title: str = Form(""),
    crossfade: bool = Form(False),
    quality: str = Form("standard"),
    genre: str = Form("neutral"),
    # Comma-separated library slugs that become the run's fixed cast. Claude
    # will use only these as named characters, and their library portraits
    # are auto-injected into references/.
    cast_slugs: str = Form(""),
    location_slugs: str = Form(""),
    prop_slugs: str = Form(""),
    reference_images: Optional[list[UploadFile]] = File(None),
):
    if num_shots < 3 or num_shots > 10:
        raise HTTPException(400, "num_shots must be 3–10")
    if shot_duration < 3 or shot_duration > 10:
        raise HTTPException(400, "shot_duration must be 3–10")
    if ratio not in ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"):
        raise HTTPException(400, f"unsupported ratio {ratio}")
    import seedance as _sd
    import genre_pacing as _gp
    if quality not in _sd.QUALITY_TIERS:
        raise HTTPException(400, f"quality must be one of {list(_sd.QUALITY_TIERS)}")
    valid_genre_ids = {g["id"] for g in _gp.list_genres()}
    if genre and genre not in valid_genre_ids:
        raise HTTPException(400, f"genre must be one of {sorted(valid_genre_ids)}")

    ref_files = []
    total_ref_bytes = 0
    for up in reference_images or []:
        if not up or not up.filename:
            continue
        data = await up.read()
        if not data:
            continue
        if len(data) > MAX_REF_IMAGE_BYTES:
            raise HTTPException(413, f"reference image '{up.filename}' exceeds {MAX_REF_IMAGE_BYTES // (1024*1024)} MB limit")
        total_ref_bytes += len(data)
        if total_ref_bytes > MAX_REF_IMAGES_TOTAL_BYTES:
            raise HTTPException(413, f"total reference images exceed {MAX_REF_IMAGES_TOTAL_BYTES // (1024*1024)} MB limit")
        _require_kind(data, "image", field=f"reference image '{up.filename}'")
        if _is_heic(data):
            data, _ = _convert_heic_to_jpeg(data)
            ref_files.append((Path(up.filename).stem + ".jpg", data))
        else:
            ref_files.append((up.filename, data))

    cast_slug_list = [s.strip() for s in (cast_slugs or "").split(",") if s.strip()]
    location_slug_list = [s.strip() for s in (location_slugs or "").split(",") if s.strip()]
    prop_slug_list = [s.strip() for s in (prop_slugs or "").split(",") if s.strip()]
    run_id = pipeline.create_run(
        concept=concept,
        num_shots=num_shots,
        shot_duration=shot_duration,
        ratio=ratio,
        style_intent=style_intent,
        title=title,
        crossfade=crossfade,
        reference_files=ref_files,
        cast_slugs=cast_slug_list or None,
        location_slugs=location_slug_list or None,
        prop_slugs=prop_slug_list or None,
    )
    # Set quality tier on the run
    async with pipeline._get_lock(run_id):
        _st = pipeline.get_state(run_id)
        _st["params"]["quality"] = quality
        _st["params"]["genre"] = genre if genre else "neutral"
        pipeline._save_state(run_id, _st)
    return {"run_id": run_id, "state": pipeline.get_state(run_id)}


# GET /api/genres moved to routers/misc.py.

@app.post("/api/runs/{run_id}/rip/preview-segments")
async def api_preview_segments(
    run_id: str,
    scene_threshold: float = Form(0.30),
    min_shots: int = Form(4),
    max_shots: int = Form(10),
    min_segment_s: float = Form(2.5),
    max_segment_s: float = Form(12.0),
):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    try:
        result = await pipeline.preview_rip_segments(
            run_id,
            scene_threshold=scene_threshold,
            min_shots=min_shots, max_shots=max_shots,
            min_segment_s=min_segment_s, max_segment_s=max_segment_s,
        )
    except Exception as e:
        logger.error(run_id, "rip", f"segmentation failed: {e}")
        import traceback; traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, "segmentation failed — check server logs")
    return result


@app.post("/api/rip/upload")
async def api_rip_upload(
    source_video: UploadFile = File(...),
    concept: str = Form(...),
    title: str = Form(""),
    style_intent: str = Form(""),
    ratio: str = Form("16:9"),
    variants_per_scene: int = Form(1),
    cast_slugs: str = Form(""),
    location_slugs: str = Form(""),
    prop_slugs: str = Form(""),
    reference_images: Optional[list[UploadFile]] = File(None),
):
    """Create a rip-o-matic run. Long-running — scene detects, extracts segments,
    asks Claude to translate each. Returns run_id so the UI can navigate and poll."""
    if not source_video or not source_video.filename:
        raise HTTPException(400, "source_video is required")
    if ratio not in ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"):
        raise HTTPException(400, f"unsupported ratio {ratio}")
    if variants_per_scene < 1 or variants_per_scene > 3:
        raise HTTPException(400, "variants_per_scene must be 1–3")

    source_data = await source_video.read()
    if not source_data:
        raise HTTPException(400, "source video is empty")
    if len(source_data) > MAX_VIDEO_UPLOAD_BYTES:
        raise HTTPException(413, f"source video exceeds {MAX_VIDEO_UPLOAD_BYTES // (1024*1024)} MB limit")
    _require_kind(source_data, "video", field="source_video")

    ref_files = []
    for up in reference_images or []:
        if not up or not up.filename:
            continue
        data = await up.read()
        if data:
            _require_kind(data, "image", field=f"reference image '{up.filename}'")
            if _is_heic(data):
                data, _ = _convert_heic_to_jpeg(data)
                ref_files.append((Path(up.filename).stem + ".jpg", data))
            else:
                ref_files.append((up.filename, data))

    cast_slug_list = [s.strip() for s in (cast_slugs or "").split(",") if s.strip()]
    location_slug_list = [s.strip() for s in (location_slugs or "").split(",") if s.strip()]
    prop_slug_list = [s.strip() for s in (prop_slugs or "").split(",") if s.strip()]
    try:
        run_id = await _bg_rip_upload(
            source_filename=source_video.filename,
            source_data=source_data,
            concept=concept,
            title=title,
            style_intent=style_intent,
            ratio=ratio,
            reference_files=ref_files,
            variants_per_scene=max(1, min(3, _safe_int(variants_per_scene, 1))),
            cast_slugs=cast_slug_list or None,
            location_slugs=location_slug_list or None,
            prop_slugs=prop_slug_list or None,
        )
    except Exception as e:
        logger.error("_", "rip", f"rip-o-matic failed to start: {e}")
        import traceback; traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, "rip-o-matic failed to start — check server logs")
    return {"run_id": run_id}


async def _bg_rip_upload(**kwargs) -> str:
    """Create a stub run synchronously (fast), then schedule the scene-detect + Claude
    translation as a background task. Returns run_id immediately so the UI can poll."""
    from datetime import datetime as _dt
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    title_slug = pipeline._slug(kwargs.get("title", ""))
    run_id = f"{ts}_rip_{title_slug}" if title_slug else f"{ts}_rip"
    d = pipeline.OUTPUT_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)

    # Inject library items into the stub's state right away so the
    # panels are visible in the Run view while the rip is still detecting
    # scenes. create_rip_run also sees them via state and threads them into
    # Claude's translation call.
    all_ref_paths: list[str] = []

    def _inject_kind(kind, slug_list):
        entries = []
        for slug in (slug_list or []):
            slug = (slug or "").strip()
            if not slug:
                continue
            try:
                item = library_mod.get_item(kind, slug)
                copied = library_mod.inject_into_run(
                    run_root=d, kind=kind, slug=slug, target_dir="references",
                )
                all_ref_paths.extend(copied)
                entries.append({
                    "slug": slug,
                    "name": item.get("name") or slug,
                    "description": item.get("description") or "",
                    "ref_paths": copied,
                })
            except Exception as e:
                print(f"[rip] could not inject {kind} {slug!r}: {e}", file=sys.stderr)
        return entries

    cast_entries = _inject_kind("characters", kwargs.get("cast_slugs"))
    location_entries = _inject_kind("locations", kwargs.get("location_slugs"))
    prop_entries = _inject_kind("props", kwargs.get("prop_slugs"))

    initial_state = {
        "run_id": run_id,
        "created_at": pipeline._now(),
        "concept": kwargs["concept"],
        "params": {
            "num_shots": 0,
            "shot_duration": 5,
            "ratio": kwargs["ratio"],
            "style_intent": kwargs.get("style_intent", ""),
            "title": kwargs.get("title", ""),
            "crossfade": False,
            "variants_per_scene": kwargs.get("variants_per_scene", 1),
        },
        "status": "ripping",
        "story": None,
        "cast": cast_entries,
        "locations": location_entries,
        "props": prop_entries,
        "references": all_ref_paths,
        "keyframes": [],
        "shots": [],
        "cut_plan": None,
        "final": None,
        "rip_mode": True,
        "source_video": {"filename": kwargs.get("source_filename", ""), "uploading": True},
    }
    pipeline._save_state(run_id, initial_state)

    async def _do():
        try:
            await pipeline.create_rip_run(
                source_filename=kwargs["source_filename"],
                source_data=kwargs["source_data"],
                concept=kwargs["concept"],
                ratio=kwargs["ratio"],
                style_intent=kwargs.get("style_intent", ""),
                title=kwargs.get("title", ""),
                reference_files=kwargs.get("reference_files"),
                variants_per_scene=kwargs.get("variants_per_scene", 1),
                run_id=run_id,
                cast_slugs=kwargs.get("cast_slugs"),
            )
        except Exception as e:
            _log_bg_error(run_id, "rip", f"background rip failed: {e}")
            try:
                async with pipeline._get_lock(run_id):
                    st = pipeline.get_state(run_id)
                    st["status"] = "failed"
                    st["error"] = str(e)[:500]
                    pipeline._save_state(run_id, st)
            except Exception as e2:
                _log_bg_error(run_id, "rip", f"failed to persist error state: {e2}")

    _bg(_do(), run_id=run_id, op="rip")
    return run_id


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str):
    try:
        return pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")


@app.get("/api/runs/{run_id}/costs")
def api_get_costs(run_id: str):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"summary": costs.summary(run_id), "entries": costs.tail(run_id)}


@app.get("/api/runs/{run_id}/log")
def api_get_run_log(run_id: str, since: Optional[str] = None, limit: int = 500):
    """Tail the per-run structured log. `since` is an ISO timestamp — server returns
    entries strictly after it. Client polls while anything is running."""
    limit = max(1, min(limit, 2000))
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"entries": logger.tail(run_id, since=since, limit=limit)}


@app.post("/api/runs/{run_id}/clone")
async def api_clone_run(run_id: str, new_title: Optional[str] = Form(None)):
    try:
        new_id = await asyncio.to_thread(pipeline.clone_run, run_id, new_title)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    except Exception as e:
        logger.error(run_id, "clone", f"clone failed: {e}")
        import traceback; traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, "clone failed — check server logs")
    return {"run_id": new_id, "state": pipeline.get_state(new_id)}


@app.get("/api/runs/{run_id}/archive")
async def api_archive_run(run_id: str):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    path = await asyncio.to_thread(pipeline.archive_run, run_id)
    return FileResponse(path, filename=f"{run_id}.zip", media_type="application/zip")


@app.delete("/api/runs/{run_id}")
async def api_delete_run(run_id: str):
    import shutil as _sh
    async with pipeline._get_lock(run_id):
        d = (OUTPUT_ROOT / run_id).resolve()
        if not d.is_relative_to(OUTPUT_ROOT.resolve()):
            raise HTTPException(400, "invalid run_id")
        if not d.exists() or not (d / "state.json").exists():
            raise HTTPException(404, f"run not found: {run_id}")
        await asyncio.to_thread(_sh.rmtree, d)
    await asyncio.to_thread(pipeline.list_runs, bust_cache=True)
    return {"deleted": run_id}


# ─── Phase 1: storyboard ─────────────────────────────────────────────────

@app.post("/api/runs/{run_id}/storyboard")
async def api_run_storyboard(run_id: str, n_options: int = Form(1)):
    """Kick off the storyboard. One Claude call (~5-10s). n_options=1 for single
    storyboard; n_options=3 for three distinct options the user picks from."""
    try:
        story = await pipeline.run_storyboard(run_id, n_options=max(1, min(3, n_options)))
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    except Exception as e:
        raise HTTPException(500, f"storyboard failed: {e}")
    return {"story": story, "state": pipeline.get_state(run_id)}


@app.post("/api/runs/{run_id}/storyboard/pick/{option_idx}")
async def api_pick_storyboard_option(run_id: str, option_idx: int):
    try:
        story = await pipeline.pick_storyboard_option(run_id, option_idx)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    except IndexError as e:
        raise HTTPException(400, str(e))
    return {"story": story, "state": pipeline.get_state(run_id)}


@app.put("/api/runs/{run_id}/storyboard")
async def api_update_storyboard(run_id: str, payload: dict):
    try:
        story = await pipeline.update_storyboard(run_id, payload)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"story": story, "state": pipeline.get_state(run_id)}


# ─── Phase 1.5: asset discovery ─────────────────────────────────────────

@app.post("/api/runs/{run_id}/assets/discover")
async def api_discover_assets(run_id: str, force: bool = False):
    """Kick off Claude asset discovery. Fire-and-forget; client polls
    state.asset_discovery_status. Returns 409 if assets are currently
    generating/uploading unless force=true."""
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    if not state.get("story"):
        raise HTTPException(400, "storyboard must exist first")

    if not force:
        in_flight = [a for a in (state.get("assets") or [])
                     if a.get("status") in ("generating", "uploaded", "generated")]
        if in_flight:
            names = ", ".join(a.get("name", a.get("id", "?")) for a in in_flight[:5])
            raise HTTPException(
                409,
                f"assets already in progress or resolved ({len(in_flight)}): {names}. "
                f"Pass force=true to discard and re-scan.",
            )

    async def _do():
        try:
            await pipeline.run_asset_discovery(run_id)
        except Exception as e:
            _log_bg_error(run_id, "assets", f"asset discovery failed: {e}")

    _bg(_do(), run_id=run_id, op="assets")
    return {"ok": True, "run_id": run_id}


_ALLOWED_ASSET_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/heic", "image/heif", "image/avif"}
from constants import MAX_ASSET_BYTES as _MAX_ASSET_SIZE  # noqa: E402


@app.post("/api/runs/{run_id}/assets/{asset_id}/upload")
async def api_upload_asset(run_id: str, asset_id: str, file: UploadFile = File(...)):
    if not _ASSET_ID_RE.match(asset_id):
        raise HTTPException(400, "invalid asset_id")
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    if not file or not file.filename:
        raise HTTPException(400, "file required")
    if file.content_type and file.content_type not in _ALLOWED_ASSET_MIMES:
        raise HTTPException(400, f"unsupported file type: {file.content_type} (expected image)")
    data = await file.read()
    if not data:
        raise HTTPException(400, "file is empty")
    if len(data) > _MAX_ASSET_SIZE:
        raise HTTPException(413, f"file too large: {len(data) // 1024}KB (max {_MAX_ASSET_SIZE // 1024 // 1024}MB)")
    _require_kind(data, "image", field="asset")
    filename = file.filename
    if _is_heic(data):
        data, _ = _convert_heic_to_jpeg(data)
        filename = Path(filename).stem + ".jpg"
    try:
        slot = await pipeline.upload_asset(run_id, asset_id, filename=filename, data=data)
    except KeyError:
        raise HTTPException(404, f"asset not found: {asset_id}")
    return {"ok": True, "asset": slot}


@app.post("/api/runs/{run_id}/assets/{asset_id}/generate")
async def api_generate_asset(
    run_id: str,
    asset_id: str,
    prompt_override: Optional[str] = Form(None),
):
    if not _ASSET_ID_RE.match(asset_id):
        raise HTTPException(400, "invalid asset_id")
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    prompt_override = _validate_prompt(prompt_override, "prompt_override")

    async def _do():
        try:
            await pipeline.generate_asset(run_id, asset_id, prompt_override=prompt_override)
        except Exception as e:
            _log_bg_error(run_id, "assets", f"asset generation failed for {asset_id}: {e}")

    _bg(_do(), run_id=run_id, op="assets")
    return {"ok": True, "run_id": run_id, "asset_id": asset_id}


@app.post("/api/runs/{run_id}/assets/generate-all")
async def api_generate_all_assets(run_id: str):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    async def _do():
        try:
            await pipeline.generate_all_assets(run_id)
        except Exception as e:
            _log_bg_error(run_id, "assets", f"batch asset generation failed: {e}")

    _bg(_do(), run_id=run_id, op="assets")
    return {"ok": True, "run_id": run_id}


@app.post("/api/runs/{run_id}/assets/{asset_id}/skip")
async def api_skip_asset(run_id: str, asset_id: str):
    if not _ASSET_ID_RE.match(asset_id):
        raise HTTPException(400, "invalid asset_id")
    try:
        slot = await pipeline.skip_asset(run_id, asset_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    except KeyError:
        raise HTTPException(404, f"asset not found: {asset_id}")
    return {"ok": True, "asset": slot}


@app.post("/api/runs/{run_id}/assets/{asset_id}/promote")
async def api_promote_asset(
    run_id: str,
    asset_id: str,
    name: str = Form(None),
    description: str = Form(""),
    tags: str = Form(""),
):
    """Promote a discovered asset to the shared library. Uses the asset's
    type to determine the library kind (character→characters, location→locations,
    prop/product/logo→props). Only works for uploaded/generated assets."""
    if not _ASSET_ID_RE.match(asset_id):
        raise HTTPException(400, "invalid asset_id")
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    slot = None
    for a in state.get("assets") or []:
        if a.get("id") == asset_id:
            slot = a
            break
    if not slot:
        raise HTTPException(404, f"asset not found: {asset_id}")
    if slot.get("status") not in ("uploaded", "generated"):
        raise HTTPException(400, f"asset must be uploaded or generated to promote (status: {slot.get('status')})")
    if not slot.get("path"):
        raise HTTPException(400, "asset has no file to promote")

    asset_type = slot.get("type", "prop")
    kind_map = {"character": "characters", "location": "locations"}
    kind = kind_map.get(asset_type, "props")

    run_dir = pipeline._run_dir(run_id)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    try:
        meta = library_mod.promote_from_run(
            run_root=run_dir, kind=kind,
            name=name or slot.get("name", asset_id),
            file_rel_paths=[slot["path"]],
            description=description or slot.get("description", ""),
            tags=tag_list,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "kind": kind, "meta": meta}



@app.post("/api/runs/{run_id}/assets/promote-all")
async def api_promote_all_assets(run_id: str):
    """Promote all uploaded/generated assets to the shared library in one click."""
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    run_dir = pipeline._run_dir(run_id)
    promoted = []
    errors = []
    kind_map = {"character": "characters", "location": "locations"}
    for slot in state.get("assets") or []:
        if slot.get("status") not in ("uploaded", "generated"):
            continue
        if not slot.get("path"):
            continue
        kind = kind_map.get(slot.get("type", "prop"), "props")
        try:
            meta = library_mod.promote_from_run(
                run_root=run_dir, kind=kind,
                name=slot.get("name", slot.get("id", "unknown")),
                file_rel_paths=[slot["path"]],
                description=slot.get("description", ""),
                tags=[],
            )
            promoted.append({"asset_id": slot["id"], "kind": kind, "slug": meta.get("slug")})
        except (ValueError, Exception) as e:
            errors.append({"asset_id": slot.get("id"), "error": str(e)})
    return {"ok": True, "promoted": promoted, "errors": errors}


# ─── Phase 2: keyframes ──────────────────────────────────────────────────

def _bg(coro, *, run_id: Optional[str] = None, op: str = "task"):
    """Schedule a coroutine on the running event loop as a fire-and-forget task.

    FastAPI's BackgroundTasks run AFTER the response is sent, which is fine, but
    we also want per-item concurrency, so we use create_task directly. Tracked in
    _bg_tasks so the lifespan shutdown hook can cancel anything still running.

    `run_id` + `op` are encoded into the task name so that if the coroutine
    raises an unhandled exception (most call sites catch inside `_do`, but the
    handler is still a safety net), `_log_task_exception` can write a row to
    that run's log.jsonl instead of silently printing to stderr.
    """
    task = asyncio.create_task(coro, name=f"bg:{run_id or '_'}:{op}")
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    task.add_done_callback(_log_task_exception)
    return task


def _log_bg_error(run_id: str, phase: str, msg: str) -> None:
    """Log a caught background-task exception with the current traceback.

    `_do()` wrappers normally swallow exceptions with a one-line summary, which
    drops the traceback. Call this from inside an `except` block to keep the
    one-line summary in log.jsonl *and* mirror the traceback to stderr where an
    operator debugging a stuck run can find it.
    """
    import traceback
    logger.error(run_id, phase, msg)
    traceback.print_exc(file=sys.stderr)


def _log_task_exception(task: asyncio.Task):
    """Safety-net for unhandled exceptions in `_bg`-scheduled coroutines.

    Most `_do()` wrappers catch inside and call `logger.error(run_id, ...)`
    themselves — this handler only fires when that catch was missed (or when
    logger.error itself raised). When the task was named with a run_id, we
    attempt to write to its per-run log; otherwise we fall through to stderr.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if not exc:
        return

    import traceback
    import sys

    # Parse "bg:<run_id>:<op>" task name set by _bg().
    name = task.get_name() or ""
    run_id: Optional[str] = None
    op = "task"
    if name.startswith("bg:"):
        parts = name.split(":", 2)
        if len(parts) == 3:
            rid, op = parts[1], parts[2]
            if rid and rid != "_":
                run_id = rid

    summary = f"background task failed: {type(exc).__name__}: {exc}"
    if run_id:
        try:
            logger.error(run_id, op, summary)
        except Exception as inner:
            print(f"[bg-task] (run {run_id}) per-run log write failed: {inner}", file=sys.stderr)
    # Always mirror to stderr — the jsonl write could itself fail, and stderr
    # is the backstop a human will scan when the UI shows nothing.
    print(f"[bg-task] {run_id or '?'} {op}: {summary}", file=sys.stderr)
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


@app.post("/api/runs/{run_id}/keyframes/{idx}")
async def api_run_keyframe(
    run_id: str,
    idx: int,
    prompt_override: Optional[str] = Form(None),
):
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    if not state.get("story"):
        raise HTTPException(400, "storyboard not generated yet")
    if idx < 0 or idx >= len(state["keyframes"]):
        raise HTTPException(400, "idx out of range")
    prompt_override = _validate_prompt(prompt_override, "prompt_override")

    claimed = await pipeline.claim_keyframe_slot(run_id, idx, prompt_override=prompt_override)
    if not claimed:
        return {"ok": True, "run_id": run_id, "idx": idx, "already_generating": True}

    async def _do():
        try:
            await pipeline.run_keyframe(run_id, idx, prompt_override=prompt_override)
        except Exception as e:
            _log_bg_error(run_id, "keyframes", f"keyframe {idx+1} failed: {e}")

    _bg(_do(), run_id=run_id, op="keyframes")
    return {"ok": True, "run_id": run_id, "idx": idx}


@app.post("/api/runs/{run_id}/keyframes/{idx}/edit")
async def api_edit_keyframe(
    run_id: str,
    idx: int,
    edit_prompt: str = Form(...),
):
    """Nano Banana edit mode — surgical change to an existing keyframe."""
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    if not state.get("story"):
        raise HTTPException(400, "storyboard missing")
    if idx < 0 or idx >= len(state["keyframes"]):
        raise HTTPException(400, "idx out of range")
    if state["keyframes"][idx]["status"] != "ready":
        raise HTTPException(400, f"keyframe {idx+1} not ready — generate it first")
    edit_prompt = _validate_prompt(edit_prompt, "edit_prompt")
    if not edit_prompt:
        raise HTTPException(400, "edit_prompt is required")

    async def _do():
        try:
            await pipeline.edit_keyframe(run_id, idx, edit_prompt=edit_prompt)
        except Exception as e:
            _log_bg_error(run_id, "keyframes", f"keyframe {idx+1} edit failed: {e}")

    _bg(_do(), run_id=run_id, op="keyframes")
    return {"ok": True, "run_id": run_id, "idx": idx}


@app.post("/api/runs/{run_id}/keyframes")
async def api_run_all_keyframes(run_id: str):
    """Generate all pending keyframes sequentially (identity chain)."""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    async def _do():
        try:
            await pipeline.run_all_keyframes(run_id)
        except Exception as e:
            _log_bg_error(run_id, "keyframes", f"run-all-keyframes failed: {e}")

    _bg(_do(), run_id=run_id, op="keyframes")
    return {"ok": True, "run_id": run_id}


# ─── Phase 3: shots ──────────────────────────────────────────────────────

@app.post("/api/runs/{run_id}/shots/{idx}/variants/{variant_idx}/primary")
async def api_set_primary_variant(run_id: str, idx: int, variant_idx: int):
    try:
        shot = await pipeline.set_primary_variant(run_id, idx, variant_idx)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    except IndexError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "shot": shot}


@app.post("/api/runs/{run_id}/shots/{idx}/variants/{variant_idx}/regenerate")
async def api_regen_variant(run_id: str, idx: int, variant_idx: int):
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    shots = state.get("shots") or []
    if idx < 0 or idx >= len(shots):
        raise HTTPException(400, f"shot idx {idx} out of range (have {len(shots)})")
    variants = shots[idx].get("variants") or []
    if variant_idx < 0 or variant_idx >= len(variants):
        raise HTTPException(400, f"variant_idx {variant_idx} out of range (have {len(variants)})")

    async def _do():
        try:
            await pipeline.run_shot(run_id, idx, variant_idx=variant_idx)
        except Exception as e:
            _log_bg_error(run_id, "shots", f"variant regen shot {idx+1}/{variant_idx+1} failed: {e}")

    _bg(_do(), run_id=run_id, op="shots")
    return {"ok": True, "run_id": run_id, "idx": idx, "variant_idx": variant_idx}


@app.post("/api/runs/{run_id}/shots/{idx}")
async def api_run_shot(
    run_id: str,
    idx: int,
    prompt_override: Optional[str] = Form(None),
):
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    if idx < 0 or idx >= len(state["shots"]):
        raise HTTPException(400, "idx out of range")
    kfs = state.get("keyframes") or []
    if idx >= len(kfs) or kfs[idx].get("status") != "ready":
        raise HTTPException(400, f"keyframe {idx+1} not ready")
    prompt_override = _validate_prompt(prompt_override, "prompt_override")

    try:
        claimed = await pipeline.claim_shot_slot(run_id, idx, variant_idx=0)
    except IndexError as e:
        raise HTTPException(400, str(e))
    if not claimed:
        return {"ok": True, "run_id": run_id, "idx": idx, "already_generating": True}

    async def _do():
        try:
            await pipeline.run_shot(run_id, idx, prompt_override=prompt_override)
        except Exception as e:
            _log_bg_error(run_id, "shots", f"shot {idx+1} failed: {e}")

    _bg(_do(), run_id=run_id, op="shots")
    return {"ok": True, "run_id": run_id, "idx": idx}


@app.post("/api/runs/{run_id}/shots/{idx}/video-ref")
async def api_attach_video_ref(
    run_id: str,
    idx: int,
    video: UploadFile = File(...),
    slot_idx: Optional[int] = Form(None),
):
    """Attach a video reference to a shot. Normalized to ≤15s, 720p, baseline H.264.
    Pass slot_idx (0-2) to target a specific slot; omit to append. Seedance supports
    up to 3 video refs per shot."""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    if not video or not video.filename:
        raise HTTPException(400, "video file is required")
    data = await video.read()
    if not data:
        raise HTTPException(400, "video file is empty")
    if len(data) > MAX_VIDEO_UPLOAD_BYTES:
        raise HTTPException(413, f"video exceeds {MAX_VIDEO_UPLOAD_BYTES // (1024*1024)} MB limit")
    _require_kind(data, "video", field="video")

    try:
        meta = await pipeline.attach_video_ref(
            run_id, idx, filename=video.filename, data=data, slot_idx=slot_idx,
        )
    except IndexError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(run_id, "video_ref", f"attach failed: {e}")
        import traceback; traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, "video ref attach failed — check server logs")
    return {"ok": True, "video_ref": meta}


@app.delete("/api/runs/{run_id}/shots/{idx}/video-ref")
async def api_detach_video_ref(run_id: str, idx: int):
    """Detach ALL video refs for a shot."""
    try:
        await pipeline.detach_video_ref(run_id, idx)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    except IndexError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/runs/{run_id}/shots/{idx}/video-ref/{slot_idx}")
async def api_detach_video_ref_slot(run_id: str, idx: int, slot_idx: int):
    """Detach one specific video-ref slot (0-2)."""
    try:
        await pipeline.detach_video_ref_slot(run_id, idx, slot_idx)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    except IndexError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/runs/{run_id}/shots")
async def api_run_all_shots(run_id: str):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    async def _do():
        try:
            await pipeline.run_all_shots(run_id)
        except Exception as e:
            _log_bg_error(run_id, "shots", f"run-all-shots failed: {e}")

    _bg(_do(), run_id=run_id, op="shots")
    return {"ok": True, "run_id": run_id}


# ─── Phase 3.5: cut plan (vision) ────────────────────────────────────────

@app.post("/api/runs/{run_id}/cut-plan")
async def api_run_cut_plan(run_id: str):
    """Kick off contact-sheet extraction + Claude vision pass. Fire-and-forget —
    client polls state.cut_plan_status for 'generating' | 'ready' | 'failed'."""
    try:
        state = pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    if not all(s["status"] == "ready" for s in state.get("shots") or []):
        raise HTTPException(400, "all shots must be ready")

    async def _do():
        try:
            await pipeline.run_cut_plan(run_id)
        except Exception as e:
            _log_bg_error(run_id, "review", f"cut plan failed: {e}")

    _bg(_do(), run_id=run_id, op="review")
    return {"ok": True, "run_id": run_id}


@app.put("/api/runs/{run_id}/cut-plan")
async def api_update_cut_plan(run_id: str, payload: dict):
    try:
        plan = await pipeline.update_cut_plan(run_id, payload)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"cut_plan": plan}


@app.post("/api/runs/{run_id}/title-card")
async def api_generate_title(
    run_id: str,
    title_text: Optional[str] = Form(None),
    style_hint: str = Form(""),
    hold_seconds: float = Form(2.5),
    animate: bool = Form(False),
):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    async def _do():
        try:
            await pipeline.generate_title_card(
                run_id, title_text=title_text, style_hint=style_hint,
                hold_seconds=hold_seconds, animate=animate,
            )
        except Exception as e:
            _log_bg_error(run_id, "title", f"title card generation failed: {e}")

    _bg(_do(), run_id=run_id, op="title")
    return {"ok": True, "run_id": run_id}


@app.delete("/api/runs/{run_id}/title-card")
async def api_remove_title(run_id: str):
    try:
        await pipeline.remove_title_card(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"ok": True}


@app.post("/api/runs/{run_id}/music/compose")
async def api_compose_music(run_id: str, payload: ComposeMusicPayload = ComposeMusicPayload()):
    """Claude writes a music brief from storyboard → ElevenLabs composes a bespoke
    track. Saved to state.music like an upload, so downstream is unchanged."""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    vibe = payload.vibe

    async def _do():
        try:
            await pipeline.compose_music(run_id, vibe=vibe)
        except Exception as e:
            _log_bg_error(run_id, "music", f"background compose failed: {e}")

    _bg(_do(), run_id=run_id, op="music")
    return {"ok": True, "run_id": run_id, "vibe": vibe}


@app.post("/api/runs/{run_id}/music")
async def api_attach_music(run_id: str, audio: UploadFile = File(...)):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    if not audio or not audio.filename:
        raise HTTPException(400, "audio file required")
    data = await audio.read()
    if not data:
        raise HTTPException(400, "audio file is empty")
    if len(data) > MAX_AUDIO_UPLOAD_BYTES:
        raise HTTPException(413, f"audio exceeds {MAX_AUDIO_UPLOAD_BYTES // (1024*1024)} MB limit")
    _require_kind(data, "audio", field="audio")
    try:
        meta = await pipeline.attach_music(run_id, filename=audio.filename, data=data)
    except Exception as e:
        logger.error(run_id, "music", f"music analysis failed: {e}")
        import traceback; traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, "music analysis failed — check server logs")
    return {"ok": True, "music": meta}


@app.delete("/api/runs/{run_id}/music")
async def api_detach_music(run_id: str):
    try:
        await pipeline.detach_music(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"ok": True}


@app.post("/api/runs/{run_id}/cut-plan/refine-timeline")
async def api_refine_timeline(run_id: str):
    """Claude vision pass that re-picks the best variant per slice."""
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    async def _do():
        try:
            await pipeline.refine_timeline_with_vision(run_id)
        except Exception as e:
            _log_bg_error(run_id, "review", f"timeline refinement failed: {e}")

    _bg(_do(), run_id=run_id, op="review")
    return {"ok": True, "run_id": run_id}


@app.post("/api/runs/{run_id}/music/snap")
async def api_snap_music(run_id: str):
    try:
        report = await pipeline.snap_timeline_to_music(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "report": report}


@app.get("/api/runs/{run_id}/music/score")
def api_music_score(run_id: str):
    try:
        score = pipeline.score_timeline_vs_music(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"score": score}


@app.post("/api/runs/{run_id}/cut-plan/auto-regen")
async def api_auto_regen_flagged(run_id: str):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    async def _do():
        try:
            await pipeline.auto_regen_flagged_shots(run_id)
        except Exception as e:
            _log_bg_error(run_id, "review", f"auto-regen failed: {e}")

    _bg(_do(), run_id=run_id, op="review")
    return {"ok": True, "run_id": run_id}


@app.delete("/api/runs/{run_id}/cut-plan")
async def api_delete_cut_plan(run_id: str):
    try:
        await pipeline.delete_cut_plan(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"ok": True}


# ─── Phase 4: stitch ─────────────────────────────────────────────────────

@app.post("/api/runs/{run_id}/stitch")
async def api_run_stitch(
    run_id: str,
    crossfade: bool = Form(False),
    use_cut_plan: bool = Form(True),
):
    try:
        pipeline.get_state(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run not found: {run_id}")

    async def _do():
        try:
            await pipeline.run_stitch(run_id, crossfade=crossfade, use_cut_plan=use_cut_plan)
        except Exception as e:
            _log_bg_error(run_id, "stitch", f"stitch failed: {e}")

    _bg(_do(), run_id=run_id, op="stitch")
    return {"ok": True, "run_id": run_id}


# Ideation endpoints moved to routers/misc.py.


# ─── Asset serving ───────────────────────────────────────────────────────

@app.get("/assets/{run_id}/{path:path}")
def api_asset(run_id: str, path: str):
    """Serve images/videos/etc from outputs/<run_id>/. Path traversal guarded."""
    base = (OUTPUT_ROOT / run_id).resolve()
    if not base.is_relative_to(OUTPUT_ROOT.resolve()):
        raise HTTPException(400, "invalid run_id")
    target = (base / path).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(400, "invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "asset not found")
    return FileResponse(target)


# ─── Static frontend ─────────────────────────────────────────────────────

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return JSONResponse(
            {"error": "static/index.html missing"}, status_code=500
        )
    return FileResponse(index)


def _validate_env():
    import os, sys, shutil
    missing = []
    if not os.getenv("GEMINI_API_KEY"):
        missing.append("GEMINI_API_KEY")
    if not os.getenv("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if not os.getenv("ARK_API_KEY") and not os.getenv("ARK_API_KEYS"):
        missing.append("ARK_API_KEY or ARK_API_KEYS")
    if missing:
        print(f"WARNING: missing env vars: {', '.join(missing)} — some features will fail", file=sys.stderr)
    if not shutil.which("ffmpeg"):
        print("WARNING: ffmpeg not found in PATH — video stitching, trimming, and frame extraction will fail. Install with: brew install ffmpeg", file=sys.stderr)
    if not shutil.which("ffprobe"):
        print("WARNING: ffprobe not found in PATH — video duration probing will fail", file=sys.stderr)

    # Disk hygiene — warn the user once per server start if outputs/ is getting
    # big. No hard fail: we'd rather nag than surprise.
    try:
        import retention
        bytes_used = retention.disk_usage()
        warn_gb = float(os.getenv("DISK_WARN_GB", "20"))
        if bytes_used > warn_gb * 1024 * 1024 * 1024:
            gb = bytes_used / (1024**3)
            print(
                f"WARNING: outputs/ is using {gb:.1f} GB (threshold: {warn_gb:.0f} GB). "
                f"Run: curl -X POST 'http://127.0.0.1:8787/api/retention/cleanup?older_than_days=30'",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[startup] disk-usage check skipped: {e}", file=sys.stderr)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8787, reload=False)
