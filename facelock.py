"""Face lock — enforce character identity across keyframes.

The #1 complaint about AI video: "the character looks different in every shot."
Even with labeled references + taste learning, Nano Banana drifts because each
keyframe is generated independently.

This module runs a POST-generation edit pass: take a reference portrait +
an already-rendered keyframe, send both to Nano Banana with a prompt that says
"preserve everything from image 1 except the face — replace with the face from
image 2." Nano Banana's multi-ref edit mode handles this well without adding
InsightFace / face-swap dependencies.

When to use:
  - Any run where character drift is visible across keyframes
  - After asset discovery generated a character anchor portrait
  - After you edit a keyframe to change something and want to lock identity back

Implementation uses the SAME nano_banana.generate_keyframe() path we already
have — we just construct a specific prompt + pass both images as refs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


# The prompt that tells Nano Banana what to do. Labeled refs clarify roles.
FACE_LOCK_PROMPT = (
    "PRESERVE image 1 EXACTLY — the composition, pose, camera angle, lighting, "
    "color grading, wardrobe, environment, and every non-face element must remain "
    "pixel-identical to image 1.\n\n"
    "ONLY REPLACE the face of the main character with the face from image 2. "
    "Match the face from image 2 precisely: skin tone, facial structure, bone "
    "geometry, hair, age, distinguishing features. Keep the expression, eye "
    "direction, and head angle from image 1 so the shot still works narratively.\n\n"
    "Do not redraw the scene. Do not reinterpret the composition. Do not change "
    "lighting. This is a surgical face swap, not a regeneration. Output a single "
    "photoreal film still."
)

FACE_LOCK_LABELS = [
    "SOURCE SCENE — preserve everything except the face",
    "REFERENCE FACE — copy this face exactly, including skin tone, features, and hair",
]


async def lock_identity(
    source_image: Path,
    face_reference: Path,
    output_path: Path,
    *,
    run_id: Optional[str] = None,
) -> Path:
    """Swap the face from `face_reference` into `source_image`, preserving
    composition + pose + lighting. Writes to `output_path`.

    Uses Nano Banana's multi-reference edit mode. ~$0.04 per lock, ~5-10s wall
    clock. No local GPU, no model download.
    """
    import nano_banana

    if not source_image.exists():
        raise FileNotFoundError(f"source image not found: {source_image}")
    if not face_reference.exists():
        raise FileNotFoundError(f"face reference not found: {face_reference}")

    await nano_banana.generate_keyframe(
        FACE_LOCK_PROMPT,
        reference_paths=[source_image, face_reference],
        output_path=output_path,
        reference_labels=FACE_LOCK_LABELS,
        rules_target="nano_banana_edit",  # edit-mode rules (preserve-everything-else)
        run_id=run_id,
    )
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"face lock produced no output: {output_path}")
    head = output_path.read_bytes()[:16]
    if not (head[:8] == b"\x89PNG\r\n\x1a\n" or head[:3] == b"\xff\xd8\xff"
            or head[:4] == b"RIFF" or head[:3] == b"GIF"):
        raise RuntimeError(f"face lock output is not a valid image: {output_path}")
    return output_path
