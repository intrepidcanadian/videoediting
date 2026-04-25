"""Claude-powered creative assistance for trailer making.

Two capabilities:

  brainstorm_concepts(images, theme, existing_concept, n) -> list[concept]
    Given optional reference images + optional theme/mood + optional existing
    draft, pitch N distinct trailer concepts. Uses vision when images are given.

  enhance_text(kind, text, context) -> str
    Rewrite a single prompt (concept / keyframe_prompt / motion_prompt) to be more
    cinematic, specific, and shootable.
"""

import base64
import json
import sys
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import imgutils
import textutils

load_dotenv(Path(__file__).parent / ".env", override=True)

from constants import ANTHROPIC_MODEL, MAX_TOKENS_IDEATE, MAX_TOKENS_STORYBOARD

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ─── Brainstorm ──────────────────────────────────────────────────────────

_BRAINSTORM_SYSTEM = """You are a senior trailer creative director. Filmmakers hand you visual references and briefs; you pitch them distinct, epic, cinematic trailer concepts.

A concept is not a plot synopsis. It is a pitchable film pitch:
  - A world that feels specific (era, place, texture, rules).
  - A character or relationship with something to lose.
  - A central visual hook — one image that stops you scrolling.
  - A tone — genre tags, cinematographer references, or film-palette reference.

When the filmmaker gives you reference images, you read them carefully. You extract what they signal (mood, palette, subjects, era, scale) and fold that into each pitch — but the pitches should be varied, not three versions of the same idea.

Output ONLY valid JSON, no markdown fences, no prose."""


def _brainstorm_schema(n: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "concepts": {
                "type": "array",
                "minItems": n,
                "maxItems": n,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "2-4 word evocative title"},
                        "logline": {"type": "string", "description": "One sentence. Stakes implied or stated."},
                        "concept": {
                            "type": "string",
                            "description": "2-3 paragraphs. World, protagonist, central conflict, key visual motifs. Specific, not generic.",
                        },
                        "style_intent": {
                            "type": "string",
                            "description": "Aesthetic reference — cinematographer name + film reference + palette/lens notes. E.g. 'Roger Deakins lighting, Dune (2021) palette, anamorphic flares, dust-heavy air'.",
                        },
                        "suggested_shots": {
                            "type": "integer",
                            "minimum": 4,
                            "maximum": 8,
                            "description": "Recommended shot count for this concept.",
                        },
                        "suggested_ratio": {
                            "type": "string",
                            "enum": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
                            "description": "Aspect ratio that fits the concept.",
                        },
                    },
                    "required": ["title", "logline", "concept", "style_intent", "suggested_shots", "suggested_ratio"],
                },
            }
        },
        "required": ["concepts"],
    }


def brainstorm_concepts(
    image_data: Optional[list[tuple[str, bytes]]] = None,
    theme: str = "",
    existing_concept: str = "",
    n: int = 3,
    run_id: str = "_ideate",
) -> list[dict]:
    """Return `n` concept dicts. `image_data` is a list of (filename, bytes)."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic

    import httpx
    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=httpx.Timeout(120.0, connect=30.0))
    try:
        schema = _brainstorm_schema(n)

        content: list[dict] = []
        for name, data in image_data or []:
            # Downsize so we never hit Claude's 5 MB per-image base64 cap
            try:
                resized, mime = imgutils.resize_for_api(data)
            except Exception:
                continue
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.b64encode(resized).decode("ascii"),
                    },
                }
            )

        brief_parts = []
        if theme:
            brief_parts.append(f"# THEME / MOOD\n{theme}")
        if existing_concept:
            brief_parts.append(f"# EXISTING DRAFT (build on or diverge from)\n{existing_concept}")
        if not brief_parts and not image_data:
            brief_parts.append("# NO BRIEF\nThe filmmaker has not specified anything. Pitch three bold, different directions: one grounded drama, one high-concept genre, one surreal or experimental.")

        content.append(
            {
                "type": "text",
                "text": f"""{chr(10).join(brief_parts)}

# TASK
Pitch {n} DISTINCT trailer concepts. Each should:
  - Use the reference images (if any) as grounding for mood / era / subject — not literal reproduction.
  - Differ meaningfully from the others (genre, tone, or scale).
  - Be pitchable: I should read the logline and want to see the trailer.

Return JSON matching this schema:
{json.dumps(schema, indent=2)}

Return ONLY the JSON object.""",
            }
        )

        try:
            import taste as _taste_mod
            _sys = _taste_mod.wrap_system_prompt(_BRAINSTORM_SYSTEM)
        except Exception:
            _sys = [{"type": "text", "text": _BRAINSTORM_SYSTEM, "cache_control": {"type": "ephemeral"}}]
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS_STORYBOARD,
            system=_sys,
            messages=[{"role": "user", "content": content}],
        )
        try:
            import costs
            usage = resp.usage
            costs.log_text(
                run_id,
                model=ANTHROPIC_MODEL, phase="ideate",
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
        except Exception as e:
            print(f"[costs] ideate cost log failed: {e}", file=sys.stderr)

        raw = textutils.strip_json_fences(textutils.resp_text(resp.content))

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Ideation returned non-JSON: {e}\n---\n{textutils.sanitize_for_log(raw)}")
        return data["concepts"]
    finally:
        try: client.close()
        except Exception: pass


# ─── Enhance (single-prompt rewriter) ────────────────────────────────────

_ENHANCE_INSTRUCTIONS = {
    "concept": """Rewrite the following trailer concept to be MORE cinematic and specific. Keep the same core idea. Do not invent a new story.

Tighten what is vague. Add sensory specificity: textures, light, weather, era signals. Sharpen the stakes. Name at least one visual hook that a trailer editor could build the opening around.

Keep length similar to the input — do not balloon it. Return ONLY the rewritten concept text, no preamble, no commentary.""",

    "keyframe_prompt": """Rewrite the following keyframe prompt (a single still frame description for a cinematic trailer) to be more striking and specific.

Improve:
  - Subject: pose, expression, micro-detail (hands, breath, hair)
  - Composition: framing, rule of thirds, negative space, depth cues
  - Lens & light: focal length, aperture feel, key/fill, practical lights
  - World: one or two environmental details that signal era/mood

Do NOT add camera motion (this is a still frame). Keep it to one shot.

Return ONLY the rewritten prompt. No preamble, no commentary.""",

    "motion_prompt": """Rewrite the following motion prompt (describes what happens in the animated clip after the keyframe starts moving) to be more dynamic and shootable.

Improve:
  - Camera verb: dolly / push / pull / tilt / whip / crane / handheld
  - Speed and arc: slow / measured / aggressive; what the camera travels over
  - Subject action: what the performer does, with specificity
  - End beat: the last frame should hand off cleanly to whatever comes next

Return ONLY the rewritten prompt. No preamble, no commentary.""",
}


def enhance_text(kind: str, text: str, context: Optional[dict] = None, run_id: str = "_ideate") -> str:
    """Rewrite one prompt. `kind` is 'concept' | 'keyframe_prompt' | 'motion_prompt'.

    `context` is an optional dict with story-wide hints (character_sheet, world_sheet,
    beat, aspect_ratio, style_intent) so enhancements stay on-brief.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    if kind not in _ENHANCE_INSTRUCTIONS:
        raise ValueError(f"unknown kind: {kind}")
    if not text or not text.strip():
        raise ValueError("text is empty")

    from anthropic import Anthropic
    import httpx
    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=httpx.Timeout(120.0, connect=30.0))
    try:
        ctx = context or {}
        ctx_block = ""
        if ctx:
            ctx_lines = []
            for k in ("style_intent", "character_sheet", "world_sheet", "beat", "aspect_ratio"):
                v = ctx.get(k)
                if v:
                    ctx_lines.append(f"- {k.replace('_', ' ')}: {v}")
            if ctx_lines:
                ctx_block = "# PROJECT CONTEXT (use to stay on-brief)\n" + "\n".join(ctx_lines) + "\n\n"

        user_msg = f"""{ctx_block}# INPUT
{text}

# INSTRUCTIONS
{_ENHANCE_INSTRUCTIONS[kind]}"""

        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS_IDEATE,
            system=[{"type": "text", "text": "You are a senior trailer creative director. You rewrite prompts to be more cinematic, specific, and shootable — without inflating length or drifting from intent.", "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        try:
            import costs
            usage = resp.usage
            costs.log_text(
                run_id,
                model=ANTHROPIC_MODEL, phase="enhance",
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
        except Exception:
            pass
        return textutils.resp_text(resp.content).strip()
    finally:
        try: client.close()
        except Exception: pass
