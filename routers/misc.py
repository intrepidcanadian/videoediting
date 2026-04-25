"""Configuration + knowledge endpoints: rules, taste learning, ideation,
genres, looks, platform-variant list, audio-status.

All routes here are stateless or operate on global config — none take a
`{run_id}`. The /api/runs/* cluster stays in server.py since those endpoints
share the `_bg` background-task tracker.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import audio as audio_mod
import ideate
import logger
import prompt_rules

router = APIRouter(tags=["config"])


# ─── Taste learning ──────────────────────────────────────────────────────

@router.get("/api/taste")
def api_taste_get():
    import taste as taste_mod
    return taste_mod.get_profile()


@router.post("/api/taste/refresh")
def api_taste_refresh():
    """Regenerate the natural-language taste profile from current signals."""
    import taste as taste_mod
    ctx = taste_mod.refresh_claude_context()
    return {"context": ctx, "profile": taste_mod.get_profile()}


@router.delete("/api/taste")
def api_taste_reset():
    import taste as taste_mod
    taste_mod.reset()
    return {"ok": True}


# ─── Platform export list ────────────────────────────────────────────────

@router.get("/api/platform-variants")
def api_list_platform_variants():
    import video as v
    return {"variants": v.platform_variants_available()}


# ─── Looks (named color grades) ──────────────────────────────────────────

@router.get("/api/looks")
def api_list_looks():
    """Return all available named color grades."""
    import looks as looks_mod
    return {"looks": looks_mod.list_looks()}


# ─── Audio / VO status ───────────────────────────────────────────────────

@router.get("/api/audio/status")
def api_audio_status():
    """Available VO voices + whether ElevenLabs is configured."""
    return audio_mod.status()


# ─── Prompt rules ────────────────────────────────────────────────────────

@router.get("/api/rules")
def api_get_rules():
    return prompt_rules.load_rules()


@router.put("/api/rules")
async def api_save_rules(payload: dict):
    try:
        saved = prompt_rules.save_rules(payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return saved


@router.post("/api/rules/reset")
def api_reset_rules():
    return prompt_rules.reset_to_defaults()


@router.post("/api/rules/test")
async def api_test_rules(payload: dict):
    """Apply the current ruleset to a test prompt. Returns before/after + applied list."""
    prompt = (payload or {}).get("prompt")
    target = (payload or {}).get("target", "nano_banana_keyframe")
    if not prompt:
        raise HTTPException(400, "'prompt' is required")
    return prompt_rules.transform(prompt, target)


# ─── Genres (pacing templates) ───────────────────────────────────────────

@router.get("/api/genres")
def api_list_genres():
    """Return all available genre-pacing templates."""
    import genre_pacing as _gp
    return {"genres": _gp.list_genres()}


# ─── Disk retention / cleanup ───────────────────────────────────────────

@router.get("/api/retention/status")
def api_retention_status():
    """Show total disk usage and what's eligible for pruning."""
    import retention
    bytes_used = retention.disk_usage()
    # Default cutoff: 30 days. Client can re-query with a custom ?older_than_days=N.
    eligible = retention.list_candidates(older_than_days=30)
    return {
        "bytes_used": bytes_used,
        "human_size": f"{bytes_used / (1024**3):.2f} GB" if bytes_used >= 1024**3 else f"{bytes_used / (1024**2):.1f} MB",
        "eligible_for_prune": eligible,
        "eligible_bytes": sum(c["size_bytes"] for c in eligible),
    }


@router.post("/api/retention/cleanup")
def api_retention_cleanup(older_than_days: float = 30, dry_run: bool = True):
    """Delete runs older than `older_than_days`. Defaults to dry-run — call with
    `dry_run=false` to actually remove files. Active runs (stitching / rendering)
    are always preserved regardless of age."""
    import retention
    if older_than_days < 0:
        raise HTTPException(400, "older_than_days must be >= 0")
    # Guardrail: disallow 0-day cleanup without explicit confirmation — easy to
    # blow away every run with a stray click.
    if older_than_days == 0 and not dry_run:
        raise HTTPException(400, "older_than_days=0 would delete ALL runs; pass dry_run=true to preview or use a non-zero value")
    return retention.cleanup(older_than_days=older_than_days, dry_run=dry_run)


# ─── Ideation (Claude vision + text rewriter) ───────────────────────────

# Magic-byte check is done by importing the helper from server (single source of
# truth). We accept the circular risk because server imports *this* router; the
# import happens at app-wire time, not at decorator time.
def _require_image(data: bytes, field: str) -> None:
    from server import _require_kind  # late import — avoids circular at module load
    _require_kind(data, "image", field=field)


@router.post("/api/ideate/concepts")
async def api_ideate_concepts(
    theme: str = Form(""),
    existing_concept: str = Form(""),
    n: int = Form(3),
    images: Optional[list[UploadFile]] = File(None),
):
    if n < 1 or n > 5:
        raise HTTPException(400, "n must be 1–5")

    image_data = []
    for up in images or []:
        if not up or not up.filename:
            continue
        data = await up.read()
        if data:
            _require_image(data, field=f"image '{up.filename}'")
            image_data.append((up.filename, data))

    try:
        concepts = await asyncio.to_thread(
            ideate.brainstorm_concepts,
            image_data=image_data,
            theme=theme,
            existing_concept=existing_concept,
            n=n,
        )
    except Exception as e:
        logger.error("_", "ideate", f"ideation failed: {e}")
        raise HTTPException(500, "ideation failed — check server logs")
    return {"concepts": concepts}


@router.post("/api/ideate/enhance")
async def api_ideate_enhance(payload: dict):
    kind = payload.get("kind")
    text = payload.get("text")
    context = payload.get("context") or {}
    if kind not in ("concept", "keyframe_prompt", "motion_prompt"):
        raise HTTPException(400, "kind must be concept | keyframe_prompt | motion_prompt")
    if not text or not str(text).strip():
        raise HTTPException(400, "text is required")
    try:
        rewritten = await asyncio.to_thread(ideate.enhance_text, kind, text, context)
    except Exception as e:
        logger.error("_", "ideate", f"enhance failed: {e}")
        raise HTTPException(500, "text enhancement failed — check server logs")
    return {"text": rewritten}
