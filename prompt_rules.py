"""Deterministic prompt transformer.

Rules are data, not code. They live in `prompt_rules.json` and are editable via
the /api/rules endpoints + the Rules view in the UI. Every API call (Nano Banana
keyframe + edit, Seedance motion) runs its prompt through the active ruleset for
its target, producing a model-compatible version.

Supported rule kinds:
  - strip_regex: remove all matches of a regex
  - strip_phrases: remove literal phrases (case-insensitive)
  - append: add text to the end (optionally skip if `skip_if_present` substring exists)
  - prepend: add text to the start (same skip option)
  - replace_regex: regex → replacement
  - clamp_length: truncate to max_chars, preserving sentence boundaries when possible

Targets are arbitrary strings; by convention:
  - nano_banana_keyframe
  - nano_banana_edit
  - nano_banana_title
  - nano_banana_asset
  - seedance_motion

The transformer returns both the transformed string and the list of rule ids that
fired, so the caller can log + display them.
"""

import json
import re
import threading
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
RULES_PATH = ROOT / "prompt_rules.json"
_lock = threading.Lock()


# ─── Defaults (written on first run if file missing) ─────────────────────

DEFAULT_RULES = {
    "version": 2,
    "rules": [
        # ── Nano Banana keyframe ───────────────────────────────────────
        {
            "id": "nb_strip_sdxl_tokens",
            "name": "Strip SDXL-era tokens",
            "target": "nano_banana_keyframe",
            "kind": "strip_regex",
            "pattern": r"\b(4k|8k|hdr|hd|masterpiece|highly detailed|ultra detailed|trending on artstation)\b[,.]?\s*",
            "flags": "i",
            "enabled": True,
            "notes": "These tokens are SDXL / midjourney-era. Gemini 2.5 Flash Image ignores them at best, gets distracted at worst.",
        },
        {
            "id": "nb_normalize_lens",
            "name": "Normalize lens notation",
            "target": "nano_banana_keyframe",
            "kind": "replace_regex",
            "pattern": r"\b(\d+)\s*mm\b",
            "replacement": r"\1mm",
            "flags": "",
            "enabled": True,
            "notes": "'50 mm' → '50mm'. Gemini reads lens notation more reliably when tight.",
        },
        {
            "id": "nb_normalize_aperture",
            "name": "Normalize aperture",
            "target": "nano_banana_keyframe",
            "kind": "replace_regex",
            "pattern": r"\bf\s*[/]?\s*(\d+(\.\d+)?)\b",
            "replacement": r"f/\1",
            "flags": "i",
            "enabled": True,
            "notes": "'f2.8' or 'f / 2.8' → 'f/2.8'.",
        },
        {
            "id": "nb_add_photoreal_anchor",
            "name": "Add photoreal anchor",
            "target": "nano_banana_keyframe",
            "kind": "append",
            "value": " Photoreal film still. Considered color grade, subtle film grain. No text, no watermarks.",
            "skip_if_present": "photoreal",
            "enabled": True,
            "notes": "Anchors Gemini toward cinematic photographic output when the prompt lacks it.",
        },

        # ── Nano Banana edit mode ──────────────────────────────────────
        {
            "id": "nbe_keep_everything_else",
            "name": "Reinforce 'preserve everything else'",
            "target": "nano_banana_edit",
            "kind": "append",
            "value": " Preserve composition, lighting, and character identity exactly; change only what the edit describes.",
            "skip_if_present": "preserve",
            "enabled": True,
            "notes": "Edit mode drifts without this hint — Gemini tends to re-interpret the whole scene.",
        },

        # ── Nano Banana title card ─────────────────────────────────────
        {
            "id": "nbt_strip_stylization",
            "name": "Strip over-stylization on titles",
            "target": "nano_banana_title",
            "kind": "strip_regex",
            "pattern": r"\b(artstation|concept art|illustration|digital painting|anime|cartoon)\b[,.]?\s*",
            "flags": "i",
            "enabled": True,
            "notes": "Title cards need legible, minimal design. Artistic-style tokens hurt text rendering.",
        },
        {
            "id": "nbt_add_legibility_hint",
            "name": "Add legibility hint",
            "target": "nano_banana_title",
            "kind": "append",
            "value": " Minimal composition, high legibility, tight kerning. Photoreal with subtle grain.",
            "skip_if_present": "legibility",
            "enabled": True,
            "notes": "Gemini handles text better with explicit legibility hints.",
        },

        # ── Seedance motion ────────────────────────────────────────────
        {
            "id": "sd_strip_transitions",
            "name": "Strip transition language",
            "target": "seedance_motion",
            "kind": "strip_phrases",
            "phrases": ["fade to", "fade in", "fade out", "cut to", "dissolve", "match cut", "smash cut", "jump cut"],
            "enabled": True,
            "notes": "Seedance renders one continuous clip. Transition language confuses it — those happen between clips in ffmpeg.",
        },
        {
            "id": "sd_strip_sound_cues",
            "name": "Strip sound / music cues",
            "target": "seedance_motion",
            "kind": "strip_regex",
            "pattern": r"\b(music swells?|drums? kick|bass drops?|silence falls?|audio cuts?|soundtrack)\b[^.,]*[.,]?\s*",
            "flags": "i",
            "enabled": True,
            "notes": "Seedance is silent. Sound cues waste prompt tokens.",
        },
        {
            "id": "sd_strip_generation_tokens",
            "name": "Strip generation-era tokens",
            "target": "seedance_motion",
            "kind": "strip_regex",
            "pattern": r"\b(cinematic|4k|8k|hdr|masterpiece|high quality|photorealistic)\b[,.]?\s*",
            "flags": "i",
            "enabled": False,
            "notes": "Off by default — 'cinematic' sometimes helps Seedance. Enable if you notice the word isn't doing anything.",
        },
        {
            "id": "sd_clamp_length",
            "name": "Clamp motion prompt length",
            "target": "seedance_motion",
            "kind": "clamp_length",
            "max_chars": 400,
            "enabled": True,
            "notes": "Community guidance: Seedance performs best at <60 words + constraints (~360-400 chars). Longer prompts dilute motion signal.",
        },
        {
            "id": "sd_strip_negative_framing",
            "name": "Strip negative framing (will add banlist separately)",
            "target": "seedance_motion",
            "kind": "strip_regex",
            "pattern": r"\b(don'?t (show|include|have)|no (text|watermark|flare)s?|without (text|watermarks?|flares?|artifacts?))\b[^.,]*[.,]?\s*",
            "flags": "i",
            "enabled": True,
            "notes": "Inline negatives hurt — Seedance (community-tested) prefers a dedicated constraints clause at the end. We strip inline negatives and the banlist rule adds a proper one.",
        },
        {
            "id": "sd_append_banlist",
            "name": "Append constraints banlist (Seedance 5-part structure)",
            "target": "seedance_motion",
            "kind": "append",
            "value": " Avoid visible text, watermarks, lens flares, malformed hands, or identity drift.",
            "skip_if_present": "Avoid visible text",
            "enabled": True,
            "notes": "Per Seedance community guide: each shot benefits from an explicit 3-5 item banlist. This is the standard list that blocks the most common artifacts.",
        },
        {
            "id": "sd_normalize_camera_verbs",
            "name": "Normalize camera motion phrasing",
            "target": "seedance_motion",
            "kind": "replace_regex",
            "pattern": r"\b(?:push(?:es|ing|ed)?)\b",
            "replacement": r"dolly",
            "flags": "i",
            "enabled": False,
            "notes": "Off by default — some teams prefer 'push' over 'dolly'. Enable to standardize on Seedance's preferred vocabulary (dolly/track/crane/handheld/gimbal).",
        },

        # ── Nano Banana keyframe — richer from Google Cloud guide ──────
        {
            "id": "nb_strip_generic_quality",
            "name": "Strip generic quality adjectives",
            "target": "nano_banana_keyframe",
            "kind": "strip_regex",
            "pattern": r"\b(beautiful|gorgeous|stunning|amazing|perfect|good|nice)\b[,.]?\s*",
            "flags": "i",
            "enabled": False,
            "notes": "Off by default. Google's guide calls these 'vague good/beautiful' anti-patterns. Enable if you notice Claude leaning on them.",
        },
        {
            "id": "nb_strip_negative_framing",
            "name": "Strip negative framing ('no X', 'without Y')",
            "target": "nano_banana_keyframe",
            "kind": "strip_regex",
            "pattern": r"\b(no |without |don'?t (show|include))\s*(text|watermark|logos?|people|cars?)\b[^.,]*[.,]?\s*",
            "flags": "i",
            "enabled": True,
            "notes": "Gemini guide: use positive framing ('empty street' not 'no cars'). Our photoreal-anchor rule already appends 'No text, no watermarks' positively.",
        },
        {
            "id": "nb_quote_literal_text",
            "name": "Wrap literal titles in quotes",
            "target": "nano_banana_title",
            "kind": "replace_regex",
            "pattern": r"(?<!['\"`])\b([A-Z][A-Z0-9 ]{2,30}[A-Z0-9])\b(?!['\"`])",
            "replacement": r"'\1'",
            "flags": "",
            "enabled": True,
            "notes": "Google guide: enclose rendered text in quotes for reliable typography. Matches all-caps words 4-32 chars (typical title length).",
        },
    ],
}


# ─── Load / save ─────────────────────────────────────────────────────────

def _ensure_defaults():
    if not RULES_PATH.exists():
        RULES_PATH.write_text(json.dumps(DEFAULT_RULES, indent=2))


def load_rules() -> dict:
    _ensure_defaults()
    with _lock:
        try:
            return json.loads(RULES_PATH.read_text())
        except Exception:
            return dict(DEFAULT_RULES)


def save_rules(data: dict) -> dict:
    """Persist the rules object. Does minimal validation (shape only)."""
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise ValueError("rules payload must be {'rules': [...]}")
    # Validate each rule's shape
    for i, r in enumerate(data["rules"]):
        if not isinstance(r, dict):
            raise ValueError(f"rule {i} must be an object")
        for required in ("id", "name", "target", "kind"):
            if required not in r:
                raise ValueError(f"rule {i} missing '{required}'")
        if r["kind"] not in ("strip_regex", "strip_phrases", "append", "prepend", "replace_regex", "clamp_length"):
            raise ValueError(f"rule {i} has unknown kind '{r['kind']}'")
    with _lock:
        tmp = RULES_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(RULES_PATH)
    return data


def reset_to_defaults() -> dict:
    save_rules(dict(DEFAULT_RULES))
    return DEFAULT_RULES


# ─── Transformer ─────────────────────────────────────────────────────────

def _compile_flags(flag_str: str) -> int:
    flags = 0
    if "i" in flag_str.lower(): flags |= re.IGNORECASE
    if "m" in flag_str.lower(): flags |= re.MULTILINE
    if "s" in flag_str.lower(): flags |= re.DOTALL
    return flags


def _apply_rule(text: str, rule: dict) -> str:
    kind = rule["kind"]

    if kind == "strip_regex":
        try:
            return re.sub(rule["pattern"], "", text, flags=_compile_flags(rule.get("flags", "")))
        except re.error:
            return text

    if kind == "strip_phrases":
        out = text
        for phrase in rule.get("phrases") or []:
            # Case-insensitive literal removal with optional surrounding commas
            out = re.sub(
                r"[,;]?\s*" + re.escape(phrase) + r"[,;]?\s*",
                " ",
                out,
                flags=re.IGNORECASE,
            )
        return re.sub(r"\s{2,}", " ", out).strip()

    if kind == "append":
        skip = rule.get("skip_if_present")
        if skip and skip.lower() in text.lower():
            return text
        value = rule.get("value", "")
        # Don't double-space
        sep = "" if text.endswith((" ", "\n")) or value.startswith((" ", "\n")) else " "
        return text + sep + value

    if kind == "prepend":
        skip = rule.get("skip_if_present")
        if skip and skip.lower() in text.lower():
            return text
        value = rule.get("value", "")
        sep = "" if text.startswith((" ", "\n")) or value.endswith((" ", "\n")) else " "
        return value + sep + text

    if kind == "replace_regex":
        try:
            return re.sub(
                rule["pattern"],
                rule.get("replacement", ""),
                text,
                flags=_compile_flags(rule.get("flags", "")),
            )
        except re.error:
            return text

    if kind == "clamp_length":
        max_chars = int(rule.get("max_chars", 500))
        if len(text) <= max_chars:
            return text
        cut = text[:max_chars]
        floor = int(max_chars * 0.5)
        for sep in [". ", "! ", "? ", "; ", ", "]:
            idx = cut.rfind(sep)
            if idx >= floor:
                return cut[:idx + 1].rstrip()
        last_space = cut.rfind(" ")
        if last_space >= floor:
            return cut[:last_space].rstrip()
        return cut.rstrip()

    return text


def transform(prompt: str, target: str) -> dict:
    """Run `prompt` through all enabled rules matching `target`. Returns:
      {
        "original":     <input>,
        "transformed":  <output>,
        "applied":      [{id, name, changed: True}, ...]   # only rules that fired
        "rules_considered": N,
      }
    """
    data = load_rules()
    applied: list[dict] = []
    current = prompt
    considered = 0
    for rule in data.get("rules") or []:
        if rule.get("target") != target:
            continue
        if not rule.get("enabled", True):
            continue
        considered += 1
        before = current
        try:
            current = _apply_rule(current, rule)
        except Exception:
            continue
        if current != before:
            applied.append({"id": rule.get("id"), "name": rule.get("name"), "kind": rule.get("kind")})
    # Final whitespace cleanup
    current = re.sub(r"\s{2,}", " ", current).strip()
    return {
        "original": prompt,
        "transformed": current,
        "applied": applied,
        "rules_considered": considered,
    }
