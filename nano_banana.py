"""Nano Banana (Gemini 2.5 Flash Image) — keyframe generator.

Each keyframe is a single cinematic film still. Character / world identity is held
across shots by:
  1. Always prepending the same character_sheet + world_sheet to the prompt.
  2. Passing user-provided reference images (portraits, location photos) with EVERY
     shot as inline ref images.
  3. For shot N > 0, passing shot N-1's rendered keyframe as an additional reference
     so color/lighting/props carry over visually.

Endpoint: {BASE}/models/{MODEL}:generateContent?key=API_KEY
"""

import base64
import os
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

import anchors as anchors_mod
import imgutils
import prompt_rules
import retry as retry_mod
import textutils

from constants import GEMINI_BASE_URL, NANO_BANANA_MODEL

load_dotenv(Path(__file__).parent / ".env", override=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


@retry_mod.retry_on_transient(tries=3, base=2.0, max_wait=30.0)
async def generate_keyframe(
    prompt: str,
    reference_paths: Optional[list[Path]] = None,
    output_path: Optional[Path] = None,
    timeout_s: float = 120.0,
    reference_labels: Optional[list[str]] = None,
    rules_target: str = "nano_banana_keyframe",
    run_id: Optional[str] = None,
) -> bytes:
    """Generate one keyframe. Writes PNG to output_path if provided. Returns bytes.

    If `reference_labels` is provided, we prepend a short text label to each image
    part — e.g. "Reference image 1 (character identity): preserve face, hair, build."
    Gemini weights labeled references much more decisively than unlabeled ones, which
    is the difference between "looks kinda similar" and "locked identity."

    Prompt is run through prompt_rules for `rules_target` (default: nano_banana_keyframe)
    before being sent, so model-specific token normalization happens automatically.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Get one at https://aistudio.google.com/app/apikey"
        )

    # Apply deterministic prompt rules
    tr = prompt_rules.transform(prompt, rules_target)
    if tr["applied"] and run_id:
        try:
            import logger
            applied_names = ", ".join(a["name"] for a in tr["applied"])
            logger.info(run_id, "keyframes", f"prompt rules applied ({rules_target}): {applied_names}")
        except Exception:
            pass
    final_prompt = tr["transformed"]

    # Anchor validation + auto-prepend default anchor when refs exist but prompt is bare
    ref_count = min(8, len(reference_paths or []))
    final_prompt = anchors_mod.auto_prepend_default(final_prompt, image_count=ref_count)
    warnings = anchors_mod.validate(final_prompt, image_count=ref_count, video_count=0)
    if warnings and run_id:
        try:
            import logger
            for w in warnings:
                logger.warn(run_id, "keyframes", f"anchor issue: {w}")
        except Exception:
            pass

    parts: list[dict] = [{"text": final_prompt}]
    # Nano Banana supports up to 14 refs per Google Cloud docs. 8 is a practical cap
    # that keeps payloads sane while giving us headroom for character + composition
    # + assets + prior keyframe.
    refs = (reference_paths or [])[:8]
    labels = list(reference_labels or [])
    for i, p in enumerate(refs):
        try:
            resized, mime = imgutils.resize_path(p)
        except Exception:
            continue
        label = labels[i] if i < len(labels) else None
        if label:
            parts.append({"text": f"Reference image {i+1} — {label}:"})
        parts.append(
            {
                "inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(resized).decode("ascii"),
                }
            }
        )

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }

    url = f"{GEMINI_BASE_URL}/models/{NANO_BANANA_MODEL}:generateContent"
    timeout = httpx.Timeout(connect=30.0, read=timeout_s, write=timeout_s, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, params={"key": GEMINI_API_KEY}, json=payload)

    if resp.status_code != 200:
        raise RuntimeError(
            f"Nano Banana failed ({resp.status_code}): {textutils.safe_err_body(resp.text)}"
        )

    try:
        body = resp.json()
    except ValueError as e:
        raise RuntimeError(f"Nano Banana returned invalid JSON: {e}") from e
    for cand in body.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inline_data") or part.get("inlineData")
            if inline and inline.get("data"):
                try:
                    img_bytes = base64.b64decode(inline["data"])
                except Exception:
                    raise RuntimeError("Nano Banana returned invalid base64 image data")
                if output_path:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(img_bytes)
                return img_bytes

    raise RuntimeError(f"No image in Nano Banana response: {str(body)[:500]}")


@retry_mod.retry_on_transient(tries=3, base=2.0, max_wait=30.0)
async def edit_image(
    source_path: Path,
    edit_prompt: str,
    output_path: Optional[Path] = None,
    timeout_s: float = 120.0,
    run_id: Optional[str] = None,
) -> bytes:
    """Edit mode: take ONE source image + an instruction, return the edited image.

    Gemini handles this via the same generateContent endpoint; the difference vs
    generate_keyframe is prompt framing. We wrap the user's instruction with
    'Edit the attached image. Keep everything else unchanged.' — without that hint
    the model tends to re-generate the whole scene as a loose interpretation
    instead of making a surgical change.

    Use this when you want to tweak an existing keyframe (hair length, wardrobe,
    time of day, remove an object, etc.) rather than regenerate from scratch.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set.")
    if not source_path.exists():
        raise RuntimeError(f"source image not found: {source_path}")
    if not edit_prompt or not edit_prompt.strip():
        raise ValueError("edit_prompt is required")

    try:
        resized, mime = imgutils.resize_path(source_path)
    except Exception as e:
        raise RuntimeError(f"could not read source image: {e}")

    # Apply edit-mode rules to the user's raw edit instruction
    tr = prompt_rules.transform(edit_prompt, "nano_banana_edit")
    if tr["applied"] and run_id:
        try:
            import logger
            logger.info(run_id, "keyframes", f"edit rules applied: {', '.join(a['name'] for a in tr['applied'])}")
        except Exception:
            pass
    edit_prompt_final = tr["transformed"]

    framed = (
        "Edit the attached image exactly as described below. "
        "Preserve everything else — composition, lighting, color palette, style, "
        "and any subjects not mentioned — precisely as-is. Return a single edited "
        "image matching the original's aspect ratio.\n\n"
        f"EDIT: {edit_prompt_final}"
    )

    parts = [
        {"text": framed},
        {
            "inline_data": {
                "mime_type": mime,
                "data": base64.b64encode(resized).decode("ascii"),
            }
        },
    ]

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }

    url = f"{GEMINI_BASE_URL}/models/{NANO_BANANA_MODEL}:generateContent"
    timeout = httpx.Timeout(connect=30.0, read=timeout_s, write=timeout_s, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, params={"key": GEMINI_API_KEY}, json=payload)

    if resp.status_code != 200:
        raise RuntimeError(
            f"Nano Banana edit failed ({resp.status_code}): {textutils.safe_err_body(resp.text)}"
        )

    try:
        body = resp.json()
    except ValueError as e:
        raise RuntimeError(f"Nano Banana edit returned invalid JSON: {e}") from e
    for cand in body.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inline_data") or part.get("inlineData")
            if inline and inline.get("data"):
                try:
                    img_bytes = base64.b64decode(inline["data"])
                except Exception:
                    raise RuntimeError("Nano Banana returned invalid base64 image data")
                if output_path:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(img_bytes)
                return img_bytes

    raise RuntimeError(f"Nano Banana edit returned no image: {str(body)[:500]}")
