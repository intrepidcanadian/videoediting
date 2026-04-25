"""Anchor syntax: @imageN / @videoN / @audioN references inside prompts.

Convention borrowed from the ComfyUI Seedance community (Anil-matcha/seedance2-comfyui):

    "The detective in @image1 pushes through @image2's rainy alley.
     Follow the camera motion from @video1."

Why it matters: models weight attached references much more confidently when the
prompt text DECLARES which one plays which role. Without anchors, Gemini and
Seedance have to infer — inference drifts, identity breaks.

This module only PARSES + VALIDATES anchors. Actual reference ordering and
labeling is done by the caller (nano_banana, seedance) which has the list of
available refs.
"""

from __future__ import annotations

import re
from typing import Optional

# @image1, @video2, @audio1, @imageN (case-insensitive, 1-indexed)
_ANCHOR_RE = re.compile(r"@(image|video|audio)(\d+)", re.IGNORECASE)


def parse(prompt: str) -> dict:
    """Scan `prompt` for anchor tokens. Return:
      {
        "image_indices": [1, 2, ...],   # 1-indexed, in order of first appearance, unique
        "video_indices": [1, ...],
        "audio_indices": [1, ...],
        "raw_matches": [(kind, idx, start, end), ...],
      }
    """
    seen_img: list[int] = []
    seen_vid: list[int] = []
    seen_aud: list[int] = []
    raw: list[tuple[str, int, int, int]] = []
    for m in _ANCHOR_RE.finditer(prompt or ""):
        kind = m.group(1).lower()
        idx = int(m.group(2))
        raw.append((kind, idx, m.start(), m.end()))
        bucket = seen_img if kind == "image" else seen_vid if kind == "video" else seen_aud
        if idx not in bucket:
            bucket.append(idx)
    return {
        "image_indices": seen_img,
        "video_indices": seen_vid,
        "audio_indices": seen_aud,
        "raw_matches": raw,
    }


def has_any(prompt: str) -> bool:
    return bool(_ANCHOR_RE.search(prompt or ""))


def validate(prompt: str, *, image_count: int, video_count: int, audio_count: int = 0) -> list[str]:
    """Return human-readable warnings for anchors that reference nonexistent refs.
    Empty list = all anchors resolve cleanly."""
    parsed = parse(prompt)
    warnings: list[str] = []
    for i in parsed["image_indices"]:
        if i < 1 or i > image_count:
            warnings.append(f"@image{i} but only {image_count} reference image(s) available")
    for i in parsed["video_indices"]:
        if i < 1 or i > video_count:
            warnings.append(f"@video{i} but only {video_count} reference video(s) available")
    for i in parsed["audio_indices"]:
        if i < 1 or i > audio_count:
            warnings.append(f"@audio{i} but only {audio_count} reference audio(s) available")
    return warnings


def annotate_with_labels(prompt: str, labels_by_kind: dict[str, dict[int, str]]) -> str:
    """Optional: inline-expand anchors with their semantic labels for models that
    don't pattern-match @N natively. Keeps anchor readable but adds clarity.

    Example:
        prompt = "The detective in @image1 turns."
        labels_by_kind = {"image": {1: "primary character identity"}}
        → "The detective in @image1 (primary character identity) turns."
    """
    if not prompt or not labels_by_kind:
        return prompt
    def replace(m: re.Match) -> str:
        kind = m.group(1).lower()
        idx = int(m.group(2))
        labels = labels_by_kind.get(kind) or {}
        label = labels.get(idx)
        if not label:
            return m.group(0)
        token = m.group(0)
        # Don't double-annotate if already followed by a parenthetical (possibly
        # after whitespace, e.g. "@image1 (label)").
        tail = prompt[m.end():].lstrip()
        if tail.startswith("("):
            return token
        return f"{token} ({label})"
    return _ANCHOR_RE.sub(replace, prompt)


def auto_prepend_default(prompt: str, *, image_count: int) -> str:
    """If at least one image ref exists and the prompt has no anchors at all,
    prepend `@image1` pointing at the primary character/composition ref. Matches
    the Anil-matcha community convention.

    Returns the possibly-modified prompt.
    """
    if image_count < 1:
        return prompt
    if has_any(prompt):
        return prompt
    # Prepend a lightweight subject anchor. Use "The subject in @image1: " so the
    # model reads it as "this scene has a character shown in attached image 1".
    return f"With the character from @image1: {prompt.strip()}"
