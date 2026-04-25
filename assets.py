"""Asset discovery — Claude scans an approved storyboard for concrete, named assets
(logos, branded products, specific locations, named character likenesses, signature
props) that benefit from being SOURCED or GENERATED separately before keyframe rendering.

For each asset the user gets three choices:
  1. Upload the real thing (best for logos, real products, real actor headshots)
  2. Generate with Nano Banana (fast placeholder — character/world sheet informed)
  3. Skip — let the rendering pipeline improvise (fine for nice-to-haves)

Assets that make it through join the run's reference pool: when a keyframe for a
specific shot is generated, any asset flagged for that shot is passed as an inline
reference image alongside the user's character refs.
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import textutils
from constants import ANTHROPIC_MODEL, MAX_TOKENS_ASSETS

load_dotenv(Path(__file__).parent / ".env", override=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


_DISCOVERY_SYSTEM = """You scan film / trailer storyboards for CONCRETE assets that should be sourced or generated separately BEFORE keyframes are rendered — because without them, the final output will drift or look wrong.

## ALWAYS flag these (they must be exactly right):
  - Specific logos or brand marks (Nike swoosh, bank logo, studio ident)
  - Branded products where the exact model matters (iPhone 15 Pro, specific handgun, vintage Coke bottle)
  - Named real-world locations (Golden Gate Bridge, Eiffel Tower, specific building)
  - Character likenesses requiring recognition of a specific real/fictional person
  - Signature props (a 1967 Impala, a specific medallion, a flag with exact insignia)

## CONDITIONAL: recurring characters (people/creatures)
If the user supplied ZERO reference images AND the storyboard has a recurring character who appears in multiple shots, FLAG THEM as a "character" asset. Without an anchor, Nano Banana will draw a different-looking person in every keyframe and identity will drift across the trailer.

If the user DID supply reference images, assume those cover the main characters and do NOT flag them again.

## NEVER flag:
  - Generic categories the user hasn't described specifically ("a car", "a coffee cup")
  - Background extras that only appear once and briefly
  - Atmospheric elements (rain, fog, smoke, lens flares)
  - Abstract things (memories, feelings)
  - Aesthetic/mood cues — those belong in prompts, not assets

## Rule of thumb
If Nano Banana or Seedance could plausibly get it right from text alone, don't flag it. Only flag things where seeing the REAL asset (uploaded or pre-generated) would meaningfully change the output.

For each asset, write a `suggested_generation_prompt` — a Nano Banana prompt that would produce a clean, reusable placeholder IF the user chooses to generate rather than upload. Tailor the prompt to the asset TYPE:
  - logo: "Minimalist logo: [brand]. Clean vector style on plain white background. Geometric simplicity."
  - product: "Product photography of [item]. Studio lighting, neutral background, high detail."
  - location: "Establishing shot photograph of [location]. Natural light, cinematic wide angle."
  - character: "Head-and-shoulders portrait photograph of [specific physical description from the character sheet]. Neutral studio lighting, plain background, direct gaze, photoreal, sharp focus, cinematic grade. No stylization — this will anchor character identity across every keyframe."
  - prop: "Clean product photography of [prop]. Isolated on neutral background, detailed."

For characters specifically: the description in suggested_generation_prompt must be concrete enough to regenerate consistently — age, build, hair, distinguishing features, wardrobe. Pull those from the storyboard's character_sheet verbatim where possible.

Be conservative on everything EXCEPT recurring characters with no refs — for those, err on the side of flagging, since identity-drift is the #1 complaint without an anchor.

Output ONLY valid JSON."""


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "assets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short label, e.g. 'XYZ Corp logo' or '1967 Chevy Impala'"},
                        "type": {
                            "type": "string",
                            "enum": ["logo", "product", "location", "character", "prop"],
                        },
                        "shots": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0},
                            "description": "0-indexed shot numbers that reference this asset",
                        },
                        "description": {"type": "string", "description": "One sentence description shown in the UI"},
                        "suggested_generation_prompt": {"type": "string"},
                    },
                    "required": ["name", "type", "shots", "description", "suggested_generation_prompt"],
                },
            },
            "reasoning": {"type": "string", "description": "One sentence on why these specifically (or why none)"},
        },
        "required": ["assets", "reasoning"],
    }


def discover_assets(
    story: dict,
    *,
    existing_reference_count: int = 0,
    cast_names: Optional[list[str]] = None,
    location_names: Optional[list[str]] = None,
    prop_names: Optional[list[str]] = None,
    run_id: str = "_unknown",
) -> dict:
    """Return {'assets': [...], 'reasoning': '...'}. `existing_reference_count` tells
    Claude how many reference images the user already supplied, so it can skip
    flagging things they've likely already covered. cast_names/location_names/prop_names
    list library items already attached to this run — Claude should not re-flag these."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic

    import httpx
    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=httpx.Timeout(120.0, connect=30.0))
    try:
        schema = _schema()

        shots_block = []
        for i, shot in enumerate(story.get("shots", [])):
            shots_block.append(
                f"\n## Shot {i}  —  beat: {shot.get('beat', '')}  —  {shot.get('duration_s', 5)}s\n"
                f"Keyframe: {shot.get('keyframe_prompt', '')}\n"
                f"Motion: {shot.get('motion_prompt', '')}"
            )

        ref_status = (
            f"User supplied {existing_reference_count} reference image(s) — assume they cover the main recurring characters."
            if existing_reference_count > 0
            else "⚠ User supplied ZERO reference images. Any recurring character in the storyboard MUST be flagged as a 'character' asset so we can pre-generate an identity anchor before keyframe rendering. Without this, every keyframe will invent a different-looking person and the trailer's character identity will drift shot-to-shot."
        )
        library_lines: list[str] = []
        if cast_names:
            library_lines.append(f"Characters already in library with reference images: {', '.join(cast_names)}. Do NOT flag these as character assets — they are covered.")
        if location_names:
            library_lines.append(f"Locations already in library with reference images: {', '.join(location_names)}. Do NOT flag these as location assets — they are covered.")
        if prop_names:
            library_lines.append(f"Props already in library with reference images: {', '.join(prop_names)}. Do NOT flag these as prop assets — they are covered.")
        library_block = "\n".join(library_lines) if library_lines else ""
        user_msg = f"""# STORYBOARD CONTEXT
Title: {story.get('title', '')}
Logline: {story.get('logline', '')}
Characters: {story.get('character_sheet', '')}
World: {story.get('world_sheet', '')}

{ref_status}
{library_block}

# SHOTS
{''.join(shots_block)}

# TASK
Flag concrete assets that should be sourced or generated separately before keyframe rendering. Follow the rules in your system prompt.

Return JSON matching this schema:
{json.dumps(schema, indent=2)}

Return ONLY the JSON object."""

        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS_ASSETS,
            system=[{"type": "text", "text": _DISCOVERY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        try:
            import costs
            usage = resp.usage
            costs.log_text(
                run_id,
                model=ANTHROPIC_MODEL, phase="assets",
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
        except Exception as e: print(f"[costs] assets cost log failed: {e}", file=sys.stderr)

        raw = textutils.strip_json_fences(textutils.resp_text(resp.content))

        try:
            out = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Asset discovery returned non-JSON: {e}\n---\n{textutils.sanitize_for_log(raw)}")
        return out
    finally:
        try: client.close()
        except Exception: pass
