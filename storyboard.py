"""Claude-powered trailer storyboarding.

Input:  concept text, shot count, per-shot duration, aspect ratio, style intent.
Output: {title, logline, character_sheet, world_sheet, shots: [{beat, duration_s,
         keyframe_prompt, motion_prompt}, ...]}

Two prompts per shot:
  - keyframe_prompt → rendered by Nano Banana as a still (Seedance's reference image)
  - motion_prompt   → Seedance's text input describing what happens in the clip

character_sheet + world_sheet are the identity anchors we prepend to every
Nano Banana call so faces, wardrobe, and lighting stay consistent across shots.
"""

import base64
import json
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import imgutils
import textutils

load_dotenv(Path(__file__).parent / ".env", override=True)

from constants import ANTHROPIC_MODEL, MAX_TOKENS_STORYBOARD

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Seedance's sweet spot is ~300-500 chars per motion prompt; Nano Banana tolerates
# longer keyframe prompts but quality drops past ~1500 chars. Claude can overshoot,
# so clamp at the output boundary before we hand off to downstream renderers.
_MAX_KEYFRAME_PROMPT_CHARS = 1500
_MAX_MOTION_PROMPT_CHARS = 800


def _clamp_prompts(story: dict) -> dict:
    """Mutate story in place: clamp per-shot keyframe/motion prompts to safe lengths."""
    for shot in (story.get("shots") or []):
        if not isinstance(shot, dict):
            continue
        kp = shot.get("keyframe_prompt")
        if isinstance(kp, str) and len(kp) > _MAX_KEYFRAME_PROMPT_CHARS:
            truncated = kp[:_MAX_KEYFRAME_PROMPT_CHARS]
            last_space = truncated.rfind(" ")
            if last_space > _MAX_KEYFRAME_PROMPT_CHARS * 0.5:
                truncated = truncated[:last_space]
            shot["keyframe_prompt"] = truncated.rstrip() + "…"
        mp = shot.get("motion_prompt")
        if isinstance(mp, str) and len(mp) > _MAX_MOTION_PROMPT_CHARS:
            truncated = mp[:_MAX_MOTION_PROMPT_CHARS]
            last_space = truncated.rfind(" ")
            if last_space > _MAX_MOTION_PROMPT_CHARS * 0.5:
                truncated = truncated[:last_space]
            shot["motion_prompt"] = truncated.rstrip() + "…"
    return story


SYSTEM_PROMPT = """You are a theatrical trailer editor. You write multi-shot trailer sequences for feature-length stories.

Output is consumed by a rendering pipeline:
  1. Nano Banana (Gemini 2.5 Flash Image) renders each `keyframe_prompt` as a single cinematic film still.
  2. Seedance 2.0 (BytePlus Ark) animates each keyframe into a clip using `motion_prompt` + the still as a reference image.
  3. ffmpeg concatenates the clips into one trailer.

Structure principles:
  - OPEN with a hook in the first 2 seconds — a striking image, an unanswered question, or motion that commands attention. Never a fade-in.
  - BUILD: establish world → introduce character → first tension beat.
  - ESCALATE: shots get tighter, faster, stakes higher. Cuts on motion.
  - PEAK: the biggest visual moment — a reveal, a collision, a transformation.
  - CLOSE: closing image that holds long enough to land. Title card if the brief asks for one.

Hard rules:
  - `keyframe_prompt` must describe ONE static frame (Nano Banana / Gemini grammar):
      Subject → Action (pose) → Location → Composition → Style.
      Name lighting specifically ("three-point softbox", "chiaroscuro", "golden-hour backlight").
      Use concrete materials ("navy tweed", not "suit"). Lens notation: "50mm f/1.8" format.
      No motion, no time passing, no multiple states. Movie-still thinking.
  - `motion_prompt` uses Seedance 2.0's 5-part structure, kept under ~60 words:
      Subject · Action (ONE verb) · Camera (dolly/track/crane/handheld/gimbal + distance) · Style (lighting/palette) · Constraints optional.
      Example: "The detective turns toward the camera as rain intensifies. Slow dolly-in, 2 feet. Dramatic side lighting, muted teal grade. Ends on a tight close-up of his eyes."
      Prefer ONE camera verb per shot. Specific ("dramatic side lighting") beats generic ("good lighting").
  - Describe recurring characters with the SAME physical details every time they appear (face, build, hair, wardrobe). Inconsistency breaks the trailer.
  - No transition language: no "fade to", "cut to", "dissolve", "match cut". Seedance renders one continuous clip, not transitions.
  - No sound / music / VO cues — the pipeline is silent.
  - No on-screen text in keyframe_prompt unless it's a title card shot.

Output: ONLY valid JSON matching the requested schema. No markdown fences, no prose, no commentary."""


def _schema(num_shots: int, shot_duration: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short trailer title (used as filename slug too)"},
            "logline": {"type": "string", "description": "One-sentence logline"},
            "character_sheet": {
                "type": "string",
                "description": "Physical descriptions of recurring characters. Be specific about face, hair, clothing, build, age. This gets prepended to every keyframe prompt — accuracy here is what makes characters consistent across shots. If no human characters, describe the recurring visual motif (creature, object, vehicle).",
            },
            "world_sheet": {
                "type": "string",
                "description": "Aesthetic bible: lighting style, color palette, era, production design, camera language. Applied to every shot.",
            },
            "prop_sheet": {
                "type": "string",
                "description": "Descriptions of recurring signature props/objects that must look consistent across shots. Be specific about material, color, size, distinguishing marks. If no recurring props, return an empty string.",
            },
            "shots": {
                "type": "array",
                "minItems": num_shots,
                "maxItems": num_shots,
                "items": {
                    "type": "object",
                    "properties": {
                        "beat": {
                            "type": "string",
                            "description": "Role of this shot: hook / world / character / inciting / escalation / peak / reveal / title",
                        },
                        "duration_s": {
                            "type": "integer",
                            "minimum": 3,
                            "maximum": 10,
                            "default": shot_duration,
                        },
                        "keyframe_prompt": {
                            "type": "string",
                            "description": "Single-frame description for Nano Banana.",
                        },
                        "motion_prompt": {
                            "type": "string",
                            "description": "What happens in the clip when the keyframe animates, for Seedance.",
                        },
                        "featured_characters": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Names of cast members from the character_sheet who appear in this shot. Empty array if none (e.g. pure landscape/object shots). Must match cast names exactly.",
                        },
                        "featured_props": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Names of props from the prop_sheet that appear in this shot. Empty array if none. Must match prop_sheet names exactly.",
                        },
                        "featured_locations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Names of pre-defined locations that appear in this shot. Empty array if none. Must match location names exactly.",
                        },
                    },
                    "required": [
                        "beat",
                        "duration_s",
                        "keyframe_prompt",
                        "motion_prompt",
                        "featured_characters",
                        "featured_props",
                        "featured_locations",
                    ],
                },
            },
        },
        "required": ["title", "logline", "character_sheet", "world_sheet", "shots"],
    }


# ─── Shot prompt sweep (for variants — distinct prompts, not just different seeds) ──

_SWEEP_SYSTEM = """You generate alternative motion prompts for a single trailer shot. Each alternative is rendered as a separate take and the user picks the winner.

Each alternative must:
  - Start with a DIFFERENT camera verb than the others (dolly / handheld / whip pan / tilt / crane / static)
  - Land a DIFFERENT energy (contemplative / tense / aggressive / measured)
  - End on a DIFFERENT beat (close-up / wide reveal / look off-camera / return-to-start)
  - Stay faithful to the keyframe: don't invent new subjects or locations

Output JSON only, no prose."""


def _sweep_schema(n: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "variants": {
                "type": "array",
                "minItems": n, "maxItems": n,
                "items": {
                    "type": "object",
                    "properties": {
                        "motion_prompt": {"type": "string"},
                        "camera_verb": {"type": "string"},
                        "why_different": {"type": "string"},
                    },
                    "required": ["motion_prompt", "camera_verb", "why_different"],
                },
            },
        },
        "required": ["variants"],
    }


def sweep_motion_prompts(
    *,
    shot: dict,
    story: dict,
    n: int = 3,
    run_id: str = "_unknown",
) -> list[dict]:
    """Claude writes n distinct motion prompts for one shot. Returns list of
    {motion_prompt, camera_verb, why_different}."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic
    import httpx as _httpx

    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=_httpx.Timeout(60.0, connect=30.0))
    user_msg = f"""# KEYFRAME
{shot.get('keyframe_prompt', '')}

# BEAT
{shot.get('beat', '')}

# ORIGINAL MOTION
{shot.get('motion_prompt', '')}

# CONTEXT
Title: {story.get('title', '')}
World: {story.get('world_sheet', '')}

# TASK
Write {n} distinct alternatives to the original motion prompt. Use the rules in your system prompt.

Return JSON matching:
{json.dumps(_sweep_schema(n), indent=2)}

Output ONLY the JSON."""

    try:
        import taste as _taste_mod
        sys_blocks = _taste_mod.wrap_system_prompt(_SWEEP_SYSTEM)
    except Exception:
        sys_blocks = [{"type": "text", "text": _SWEEP_SYSTEM, "cache_control": {"type": "ephemeral"}}]

    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        system=sys_blocks,
        messages=[{"role": "user", "content": user_msg}],
    )
    try:
        import costs
        u = resp.usage
        costs.log_text(run_id, model=ANTHROPIC_MODEL, phase="sweep",
            input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            cached_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
    except Exception as e: print(f"[costs] sweep cost log failed: {e}", file=sys.stderr)

    raw = textutils.strip_json_fences(textutils.resp_text(resp.content))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Sweep returned non-JSON: {e}\n---\n{textutils.sanitize_for_log(raw)}")
    try: client.close()
    except Exception: pass
    return data.get("variants") or []


def _multi_schema(num_shots: int, shot_duration: int, n_options: int) -> dict:
    single = _schema(num_shots, shot_duration)
    return {
        "type": "object",
        "properties": {
            "options": {
                "type": "array",
                "minItems": n_options, "maxItems": n_options,
                "items": single,
            },
        },
        "required": ["options"],
    }


def _format_cast_block(cast: Optional[list[dict]]) -> str:
    """Build the prompt section that constrains Claude to a pre-defined cast.
    Returns '' when no cast is set so the original free-invent behavior is
    untouched for runs that don't use this feature."""
    if not cast:
        return ""
    lines = ["# CAST — YOU MUST USE ONLY THESE CHARACTERS",
             "The user has pre-defined the recurring cast. You MUST:",
             "  - Use only these named characters as recurring / named roles",
             "  - Do NOT invent additional recurring characters",
             "  - Refer to each character by their EXACT name below when describing keyframes",
             "  - Describe their physical appearance using the provided description verbatim — these descriptions lock identity across shots, so do not paraphrase",
             "  - Background extras (crowds, waiters, bystanders who appear once) are fine to invent",
             "  - If the concept needs more characters than the cast covers, bend the concept to fit the cast, not the other way around",
             "",
             "Cast:"]
    for i, c in enumerate(cast):
        name = (c.get("name") or f"Character {i+1}").strip()
        desc = (c.get("description") or "").strip() or "(no description provided)"
        lines.append(f"  {i+1}. {name}")
        lines.append(f"     Physical description: {desc}")
    lines.append("")
    lines.append("Your `character_sheet` field in the returned JSON MUST list each cast member by name with their description, matching the list above.")
    lines.append("Each shot's `featured_characters` array MUST list which cast members appear in that shot, using their EXACT names from the cast list above.")
    return "\n".join(lines) + "\n\n"


def _format_locations_block(locations: Optional[list[dict]]) -> str:
    """Build prompt section that tells Claude which pre-defined locations to use.
    Returns '' when no locations are set."""
    if not locations:
        return ""
    lines = ["# LOCATIONS — PRE-DEFINED SETTINGS",
             "The user has pre-defined these recurring locations. You SHOULD:",
             "  - Use these locations as the primary settings for shots where they fit",
             "  - Describe each location using the provided description verbatim in keyframe_prompt",
             "  - You may invent additional minor locations if the story demands, but prefer these",
             ""]
    for i, loc in enumerate(locations):
        name = (loc.get("name") or f"Location {i+1}").strip()
        desc = (loc.get("description") or "").strip() or "(no description provided)"
        lines.append(f"  {i+1}. {name}")
        lines.append(f"     Visual description: {desc}")
    lines.append("")
    return "\n".join(lines) + "\n\n"


def _format_props_block(props: Optional[list[dict]]) -> str:
    """Build prompt section that tells Claude which pre-defined props to feature.
    Returns '' when no props are set."""
    if not props:
        return ""
    lines = ["# PROPS — KEY OBJECTS",
             "The user has pre-defined these signature props/objects. You SHOULD:",
             "  - Feature these props in shots where they naturally fit",
             "  - Describe each prop using the provided description verbatim",
             "  - These are specific objects that must look consistent across shots",
             ""]
    for i, prop in enumerate(props):
        name = (prop.get("name") or f"Prop {i+1}").strip()
        desc = (prop.get("description") or "").strip() or "(no description provided)"
        lines.append(f"  {i+1}. {name}")
        lines.append(f"     Visual description: {desc}")
    lines.append("")
    return "\n".join(lines) + "\n\n"


def generate_storyboard(
    concept: str,
    num_shots: int = 6,
    shot_duration: int = 5,
    ratio: str = "16:9",
    style_intent: str = "",
    n_options: int = 1,
    run_id: str = "_unknown",
    genre: str = "neutral",
    cast: Optional[list[dict]] = None,
    locations: Optional[list[dict]] = None,
    props: Optional[list[dict]] = None,
) -> dict:
    """Call Claude, parse JSON, return the storyboard dict. With n_options > 1,
    returns {"options": [story1, story2, ...]} — caller picks one."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic

    import httpx
    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=httpx.Timeout(120.0, connect=30.0))
    if n_options > 1:
        schema = _multi_schema(num_shots, shot_duration, n_options)
    else:
        schema = _schema(num_shots, shot_duration)
    total = num_shots * shot_duration

    if n_options > 1:
        task_block = f"""# TASK
Design {n_options} DISTINCT {num_shots}-shot theatrical trailer storyboards for the same concept. Each option should take a genuinely different creative direction — different opening hook, different emotional beat, different genre emphasis. Don't make them three near-copies.

Return JSON with an "options" array:
{json.dumps(schema, indent=2)}"""
    else:
        task_block = f"""# TASK
Design a {num_shots}-shot theatrical trailer.

Return JSON matching this schema:
{json.dumps(schema, indent=2)}"""

    cast_block = _format_cast_block(cast)
    locations_block = _format_locations_block(locations)
    props_block = _format_props_block(props)

    user_msg = f"""# CONCEPT
{concept}

# BRIEF
- Total shots: {num_shots}
- Target duration per shot: ~{shot_duration}s (trailer runs ~{total}s)
- Aspect ratio: {ratio}
- Style intent: {style_intent or "pick what fits the concept — be specific about genre and visual language"}

{cast_block}{locations_block}{props_block}{task_block}

Return ONLY the JSON object. No markdown fences, no commentary."""

    # Genre pacing block (empty string if neutral)
    try:
        import genre_pacing as _gp
        genre_block = _gp.system_prompt_block(genre)
    except Exception as e:
        print(f"[storyboard] genre_pacing load failed, using neutral pacing: {e}", file=sys.stderr)
        genre_block = ""
    base_system = (genre_block + "\n\n---\n\n" + SYSTEM_PROMPT) if genre_block else SYSTEM_PROMPT

    try:
        import taste as _taste
        sys_blocks = _taste.wrap_system_prompt(base_system)
    except Exception:
        sys_blocks = [{"type": "text", "text": base_system, "cache_control": {"type": "ephemeral"}}]
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4000 * max(1, n_options),
        # Prompt cache — system prompt is long and never changes across calls
        system=sys_blocks,
        messages=[{"role": "user", "content": user_msg}],
    )

    try:
        import costs
        usage = resp.usage
        costs.log_text(
            run_id,
            model=ANTHROPIC_MODEL, phase="storyboard",
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
    except Exception as e:
        print(f"[costs] storyboard cost log failed: {e}", file=sys.stderr)

    raw = textutils.strip_json_fences(textutils.resp_text(resp.content))

    try:
        out = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Storyboard returned non-JSON: {e}\n---\n{textutils.sanitize_for_log(raw)}")
    if isinstance(out, dict):
        if "options" in out and isinstance(out["options"], list):
            for opt in out["options"]:
                if isinstance(opt, dict):
                    _clamp_prompts(opt)
        else:
            _clamp_prompts(out)
    try: client.close()
    except Exception: pass
    return out


# ─── Rip-o-matic — storyboard driven by an existing trailer ───────────────

RIP_SYSTEM_PROMPT = """You are a trailer editor doing a rip-o-matic: translating an existing trailer's shot grammar into new content. The source trailer's CAMERA / RHYTHM / COMPOSITION is the template. The user's concept provides the SUBJECT / WORLD / STAKES.

For each source segment, you will see first and last frames. You write a new keyframe_prompt + motion_prompt that:
  - Preserves the source segment's composition (framing, depth, scale of subject in frame)
  - Preserves its camera intent (wide establishing / medium / close; push-in / handheld / tracking / static)
  - Preserves its energy (slow contemplative / tense build / aggressive action)
  - REPLACES the source's subjects, setting, and era with the user's concept

Never describe the source trailer's actual content in your prompts — the rendering pipeline doesn't see it. Describe the USER'S content positioned and moved LIKE the source shot.

If a source segment is visual chaos or extreme close-up with no clear subject, just describe a moody abstract frame in the user's world that matches the energy. Do not force a subject that isn't supported.

Hard rules (same as the normal storyboard writer):
  - keyframe_prompt describes ONE still frame. No motion language there.
  - motion_prompt describes what happens in the clip: camera verb + subject action + end state. 1–3 sentences.
  - Describe recurring characters with the SAME physical details every time they appear.
  - No transition language, no sound cues, no on-screen text (unless the source clearly has title-card energy).

Output ONLY valid JSON. No fences, no prose."""


def _rip_schema(n: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "logline": {"type": "string"},
            "character_sheet": {"type": "string"},
            "world_sheet": {"type": "string"},
            "shots": {
                "type": "array",
                "minItems": n, "maxItems": n,
                "items": {
                    "type": "object",
                    "properties": {
                        "beat": {"type": "string"},
                        "duration_s": {"type": "integer", "minimum": 3, "maximum": 12},
                        "keyframe_prompt": {"type": "string"},
                        "motion_prompt": {"type": "string"},
                        "source_camera_note": {
                            "type": "string",
                            "description": "one-line description of the source segment's camera/composition you are preserving — e.g. 'wide establishing, slow push-in, subject centered in bottom third'",
                        },
                        "featured_characters": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Names of cast members who appear in this shot. Empty array if none.",
                        },
                        "featured_props": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Names of props from the prop_sheet that appear in this shot. Empty array if none.",
                        },
                        "featured_locations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Names of pre-defined locations that appear in this shot. Empty array if none.",
                        },
                    },
                    "required": ["beat", "duration_s", "keyframe_prompt", "motion_prompt", "source_camera_note", "featured_characters", "featured_props", "featured_locations"],
                },
            },
        },
        "required": ["title", "logline", "character_sheet", "world_sheet", "shots"],
    }


def generate_from_source(
    *,
    concept: str,
    segments: list[dict],
    ratio: str = "16:9",
    style_intent: str = "",
    title_hint: str = "",
    run_id: str = "_unknown",
    cast: Optional[list[dict]] = None,
    locations: Optional[list[dict]] = None,
    props: Optional[list[dict]] = None,
) -> dict:
    """Claude reads segment frames from a source trailer + user's concept, returns a
    translated storyboard with exactly len(segments) shots.

    `segments` items need 'first_frame_path' (Path or str), 'duration', 'idx'. Optionally
    'last_frame_path' if available — we'll send it too when present for camera arc hints.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic

    import httpx
    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=httpx.Timeout(120.0, connect=30.0))
    n = len(segments)
    schema = _rip_schema(n)

    cast_block = _format_cast_block(cast)
    locations_block = _format_locations_block(locations)
    props_block = _format_props_block(props)

    content: list[dict] = [
        {
            "type": "text",
            "text": f"""# USER'S CONCEPT
{concept}

# BRIEF
- Aspect ratio: {ratio}
- Style intent: {style_intent or '(match the source trailer mood)'}
- Title hint: {title_hint or '(you pick)'}
- Source trailer: {n} segments you will see below, in order. Each has a first frame (and sometimes a last frame) plus a duration.

{cast_block}{locations_block}{props_block}# TASK
Write a {n}-shot storyboard that translates the source's grammar to the user's concept.

For each source segment, study the frame(s), note what the source is doing compositionally and kinetically, and write a keyframe_prompt + motion_prompt that stages the USER'S content in the same way.

Return JSON matching this schema:
{json.dumps(schema, indent=2)}

Return ONLY the JSON object.""",
        }
    ]

    for seg in segments:
        first = seg.get("first_frame_path")
        last = seg.get("last_frame_path")
        idx = seg.get("idx", 0)
        duration = seg.get("duration", 5)
        content.append(
            {
                "type": "text",
                "text": f"\n## SOURCE SEGMENT {idx + 1}  —  duration {duration:.2f}s",
            }
        )
        for label, p in (("first frame", first), ("last frame", last)):
            if not p:
                continue
            try:
                resized, mime = imgutils.resize_path(Path(p), max_side=1024)
            except Exception as e:
                print(f"[storyboard] frame resize failed for {p}: {e}", file=sys.stderr)
                continue
            content.append({"type": "text", "text": f"{label}:"})
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

    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=6000,
        system=[{"type": "text", "text": RIP_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
    )

    try:
        import costs
        usage = resp.usage
        costs.log_text(
            run_id,
            model=ANTHROPIC_MODEL, phase="storyboard",
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
    except Exception as e: print(f"[costs] storyboard cost log failed: {e}", file=sys.stderr)

    raw = textutils.strip_json_fences(textutils.resp_text(resp.content))

    try:
        story = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Rip storyboard returned non-JSON: {e}\n---\n{textutils.sanitize_for_log(raw)}")

    # Force each shot's duration to match its source segment (exact grammar preservation)
    for i, shot in enumerate(story.get("shots", [])):
        if i < len(segments):
            shot["duration_s"] = max(3, min(12, int(round(segments[i]["duration"]))))
    _clamp_prompts(story)
    try: client.close()
    except Exception: pass
    return story
