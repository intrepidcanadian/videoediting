"""Multi-angle asset generator (characters, locations, and props).

Characters get a 5-angle consistent character sheet; locations get a 5-angle
environment sheet; props get a 5-angle product sheet. All generated via Nano
Banana, saved as a single library item. Each subsequent angle uses the
first-generated image as a reference so visual identity stays locked.

Character angles (in render order):
  1. Front   — seed render, no references
  2. 3/4 L   — body turned 45° to the left, head toward camera
  3. Profile — head turned 90° left
  4. 3/4 R   — body turned 45° to the right, head toward camera
  5. Looking off-camera, half-smile (expression variant)

Location angles (in render order):
  1. Wide establishing — full environment, cinematic wide angle
  2. Medium — closer vantage, architectural detail visible
  3. Close detail — texture/material detail, environmental storytelling
  4. Golden hour — same establishing angle, warm sunset lighting
  5. Night/blue hour — same establishing angle, cool artificial/moonlight

Prop angles (in render order):
  1. Hero — clean product shot, front-facing on neutral background
  2. 3/4 left — rotated 45° showing depth and side detail
  3. Top-down — flat lay / bird's eye showing outline and proportions
  4. Close detail — macro detail of distinctive texture or marking
  5. In-context — prop held or placed in a natural scene environment

Background-execution model: caller gets the slug back immediately. The library
item is created with `generation_status=generating` and `generated_count=0`;
each angle that finishes increments the counter. The UI polls the library
list endpoint to show progress — no new state storage needed.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

import library as library_mod
import logger
import nano_banana
import textutils


# Shared style block prepended to every angle prompt so lighting/lens/look
# stay consistent across the 5 images. Keeping it short since Nano Banana's
# quality drops past ~1500 chars per keyframe prompt.
_STYLE_BLOCK = (
    "Photoreal portrait, studio softbox lighting, neutral grey backdrop, "
    "85mm lens at f/2 shallow depth of field, sharp focus on face, "
    "consistent identity across all angles, cinematic color grade."
)

# Angle definitions. Each entry: (slug, one-line camera/pose description).
# When the prompt is composed we wrap the description with the user's base
# character description so the model knows WHO it's drawing.
_CHARACTER_ANGLES: list[tuple[str, str]] = [
    ("front",
     "Head-and-shoulders portrait, front-facing. Direct gaze to camera, neutral expression."),
    ("three_quarter_left",
     "Three-quarter view: body turned 45° to the left, head turned toward camera. Same wardrobe, same hair, same lighting."),
    ("profile_left",
     "Left profile: head turned 90° to the left, looking off-camera to the left. Same wardrobe, same hair, same lighting."),
    ("three_quarter_right",
     "Three-quarter view: body turned 45° to the right, head turned toward camera. Same wardrobe, same hair, same lighting."),
    ("expression",
     "Same subject, same framing as the front portrait. Gaze down and slightly off-camera, subtle half-smile, softer eyes."),
]

_LOCATION_STYLE_BLOCK = (
    "Photoreal establishing shot, cinematic wide-angle lens, "
    "high dynamic range, sharp focus throughout, "
    "consistent architecture and environment across all angles, cinematic color grade."
)

_LOCATION_ANGLES: list[tuple[str, str]] = [
    ("wide_establishing",
     "Wide establishing shot showing the full environment. Cinematic wide angle, eye-level, natural daylight, clear sky."),
    ("medium",
     "Medium shot from a closer vantage point. Architectural and structural details visible. Same location, same time of day, same weather."),
    ("close_detail",
     "Close-up detail shot of a distinctive texture, material, or environmental storytelling element within this location. Same lighting conditions."),
    ("golden_hour",
     "Same wide establishing angle as the first image. Golden hour / warm sunset lighting casting long shadows. Same architecture, same environment."),
    ("night",
     "Same wide establishing angle as the first image. Night / blue hour with cool artificial lighting or moonlight. Same architecture, same environment."),
]

_PROP_STYLE_BLOCK = (
    "Photoreal product photography, studio softbox lighting, neutral white backdrop, "
    "50mm macro lens at f/4, sharp focus throughout, "
    "consistent object identity across all angles, cinematic color grade."
)

_PROP_ANGLES: list[tuple[str, str]] = [
    ("hero",
     "Clean product shot, front-facing centered on neutral white background. Studio lighting, no shadows, sharp detail."),
    ("three_quarter_left",
     "Same object rotated 45° to the left, showing depth and side detail. Same lighting, same background."),
    ("top_down",
     "Top-down / bird's eye view showing the object's outline and proportions. Same lighting, same background."),
    ("close_detail",
     "Macro close-up of the most distinctive texture, marking, or detail on the object. Same lighting conditions."),
    ("in_context",
     "Same object placed or held naturally in a realistic scene environment. Natural lighting, shallow depth of field, object clearly identifiable."),
]


def _build_prompt(base_description: str, angle_description: str, is_seed: bool,
                  kind: str = "characters") -> str:
    """Compose a Nano Banana prompt for one angle."""
    if kind == "locations":
        style = _LOCATION_STYLE_BLOCK
        ref_anchor = "Same location as reference image 1."
        framing_label = "Framing"
    elif kind == "props":
        style = _PROP_STYLE_BLOCK
        ref_anchor = "Same object as reference image 1."
        framing_label = "Angle"
    else:
        style = _STYLE_BLOCK
        ref_anchor = "Same character as reference image 1."
        framing_label = "Pose / framing"
    if is_seed:
        return (
            f"{base_description}\n\n"
            f"{framing_label}: {angle_description}\n\n"
            f"{style}"
        )
    return (
        f"{ref_anchor} {angle_description}\n\n"
        f"{style}"
    )


async def generate_turnaround(
    *,
    name: str,
    description: str,
    tags: Optional[list[str]] = None,
    kind: str = "characters",
) -> str:
    """Create a library item and kick off turnaround generation in the
    background. Returns the slug immediately.

    The library item exists with status=generating the moment this returns,
    so the UI can render the partially-complete character without waiting
    ~50s for every angle to finish."""
    if not name or not name.strip():
        raise ValueError("name is required")
    if not description or not description.strip():
        raise ValueError("description is required")
    if kind not in ("characters", "locations", "props"):
        raise ValueError("turnaround is supported for kind='characters', kind='locations', or kind='props'")

    angles = _PROP_ANGLES if kind == "props" else (_LOCATION_ANGLES if kind == "locations" else _CHARACTER_ANGLES)
    meta = library_mod.save_item(
        kind,
        name=name.strip(),
        description=description.strip(),
        tags=list(tags or []),
        extra={
            "turnaround": {
                "status": "generating",
                "generated_count": 0,
                "planned_count": len(angles),
                "started_at": textutils.now_iso(),
                "angles": [a[0] for a in angles],
                "failed_angles": [],
            },
        },
    )
    slug = meta["slug"]
    asyncio.create_task(_render_all(slug, name.strip(), description.strip(), kind=kind),
                        name=f"turnaround:{slug}")
    return slug


async def _render_all(slug: str, name: str, description: str, *, kind: str = "characters") -> None:
    """Background worker: render each angle sequentially, updating meta as
    each completes. A single failure doesn't abort the rest — we log it and
    move on, since 4 of 5 angles is still useful."""
    angles = _PROP_ANGLES if kind == "props" else (_LOCATION_ANGLES if kind == "locations" else _CHARACTER_ANGLES)
    item_dir = library_mod._item_dir(kind, slug)
    seed_path: Optional[Path] = None
    generated = 0
    failed: list[str] = []
    t0 = time.time()
    if kind == "locations":
        ref_label = "location identity — preserve architecture, materials, and environment"
    elif kind == "props":
        ref_label = "prop identity — preserve exact shape, color, markings, and material"
    else:
        ref_label = "primary character identity — preserve face, hair, wardrobe"

    for idx, (angle_slug, angle_desc) in enumerate(angles):
        is_seed = idx == 0
        angle_prompt = _build_prompt(description, angle_desc, is_seed=is_seed, kind=kind)
        angle_path = item_dir / f"{angle_slug}.png"
        try:
            logger.info(slug, "turnaround", f"angle {idx+1}/{len(angles)}: {angle_slug}")
            refs = [seed_path] if (seed_path and not is_seed) else []
            ref_labels = [ref_label] if refs else []
            await asyncio.wait_for(
                nano_banana.generate_keyframe(
                    prompt=angle_prompt,
                    reference_paths=refs,
                    output_path=angle_path,
                    reference_labels=ref_labels or None,
                    rules_target="nano_banana_keyframe",
                    run_id=slug,
                ),
                timeout=120,
            )
            if is_seed:
                seed_path = angle_path
            generated += 1
            _bump_turnaround_meta(slug, name, description,
                                  generated_count=generated, failed_angles=failed,
                                  status="generating", kind=kind)
        except Exception as e:
            logger.error(slug, "turnaround", f"✗ angle {angle_slug} failed: {e}")
            failed.append(angle_slug)
            print(f"[turnaround] {slug} {angle_slug} failed: {e}", file=sys.stderr)
            # If the SEED angle fails we can't proceed — no identity anchor
            # exists to reference the remaining angles against.
            if is_seed:
                _bump_turnaround_meta(slug, name, description,
                                      generated_count=0, failed_angles=failed,
                                      status="failed", kind=kind,
                                      error=f"seed angle failed: {str(e)[:200]}")
                logger.error(slug, "turnaround", "aborting — seed angle required for subsequent angles")
                return

    elapsed = time.time() - t0
    final_status = "ready" if generated > 0 else "failed"
    _bump_turnaround_meta(slug, name, description,
                          generated_count=generated, failed_angles=failed,
                          status=final_status, kind=kind, elapsed_s=round(elapsed, 1))
    logger.success(slug, "turnaround",
                   f"✓ {generated}/{len(angles)} angles in {elapsed:.0f}s"
                   + (f" (failed: {', '.join(failed)})" if failed else ""))


def _bump_turnaround_meta(slug: str, name: str, description: str,
                          *, generated_count: int, failed_angles: list[str],
                          status: str, kind: str = "characters",
                          elapsed_s: Optional[float] = None,
                          error: Optional[str] = None) -> None:
    """Update only the turnaround sub-object on the library item's meta,
    preserving whatever the user (or library.save_item) already stored."""
    angles = _PROP_ANGLES if kind == "props" else (_LOCATION_ANGLES if kind == "locations" else _CHARACTER_ANGLES)
    extra: dict = {
        "turnaround": {
            "status": status,
            "generated_count": generated_count,
            "planned_count": len(angles),
            "angles": [a[0] for a in angles],
            "failed_angles": failed_angles,
        }
    }
    if elapsed_s is not None:
        extra["turnaround"]["elapsed_s"] = elapsed_s
    if error:
        extra["turnaround"]["error"] = error
    library_mod.save_item(
        kind,
        name=name, description=description,
        slug=slug, extra=extra,
    )
