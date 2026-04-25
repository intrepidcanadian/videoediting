"""Shared text utilities.

Home for helpers that were formerly duplicated across modules:
  - slug()      — URL/filesystem-safe name coercion
  - now_iso()   — second-precision ISO timestamp
  - redact/safe_err_body — credential scrubbing on error bodies
  - resp_text / strip_json_fences — Claude response parsing
"""

import re
from datetime import datetime


def now_iso() -> str:
    """Second-precision ISO-8601 timestamp, local time. Used for `updated_at`
    fields in state.json and JSONL log rows."""
    return datetime.now().isoformat(timespec="seconds")


def slug(s: str, max_len: int = 40) -> str:
    """Coerce `s` to a URL/filesystem-safe lowercase identifier.
    Default max_len=40 matches pipeline.py's original; callers that need a
    longer bound (e.g. library item names) pass their own."""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", (s or "").strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] or "item"


# Patterns that commonly show up in API error bodies where a provider echoes
# the request headers, a key fragment, or a bearer token. We redact these
# before surfacing the body in an exception or log line.
_REDACT_PATTERNS = [
    # Bearer tokens / generic "Authorization: Bearer ..."
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9_\-\.=]+"), r"\1[REDACTED]"),
    # "Authorization": "..." and variants (with or without quotes)
    (re.compile(r"(?i)(Authorization[\"']?\s*:\s*[\"']?)[^\"'\s,}]+"), r"\1[REDACTED]"),
    # x-api-key / xi-api-key / api-key / api_key / apiKey JSON and header forms
    (re.compile(r"(?i)([\"']?x[i_-]?api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([\"']?api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"), r"\1[REDACTED]"),
    # ark-… and sk-ant-… style key fragments that might appear in echoed payloads
    (re.compile(r"\b(ark-[A-Za-z0-9_\-]{16,})"), "[REDACTED_ARK_KEY]"),
    (re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{16,})"), "[REDACTED_CLAUDE_KEY]"),
    (re.compile(r"\b(sk-[A-Za-z0-9]{20,})"), "[REDACTED_KEY]"),
    # Generic long base64/hex tokens following key-like names
    (re.compile(r"(?i)(token[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]{16,}"), r"\1[REDACTED]"),
]


def redact_secrets(text: str) -> str:
    """Strip anything that looks like a credential from error-response bodies.
    Safe to call on any string; returns the input unchanged if no pattern matches."""
    if not text:
        return text
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return text


def safe_err_body(text: str, max_len: int = 300) -> str:
    """Redact secrets, sanitize control chars, and clamp length — in that order.
    Use this at every `raise RuntimeError(f"... {resp.text[:N]}")` site."""
    return sanitize_for_log(redact_secrets(text or ""), max_len=max_len)


def sanitize_for_log(text: str, max_len: int = 500) -> str:
    """Strip control characters and clamp length before surfacing text in
    error messages or logs. Keeps \\n and \\t for readability, replaces other
    C0/C1 control bytes with '?' so log lines don't render garbled."""
    if not text:
        return ""
    out = []
    for ch in text[:max_len]:
        o = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif o < 0x20 or 0x7F <= o <= 0x9F:
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


def resp_text(content) -> str:
    """Extract text from Claude's response content blocks, with a guard
    against empty or non-text responses."""
    if not content:
        raise RuntimeError("Claude returned empty response")
    block = content[0]
    text = getattr(block, "text", None)
    if text is None:
        raise RuntimeError(f"Claude response block has no text: {type(block).__name__}")
    return text


def strip_json_fences(text: str) -> str:
    """Strip markdown code fences from Claude's JSON output.

    Claude sometimes wraps its JSON in ```json ... ``` even when told not to.
    This handles all observed patterns: bare ```, ```json, trailing ```,
    and the edge case where the opening fence is on the same line as JSON.
    """
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline == -1:
            text = text[3:]
        else:
            text = text[first_newline + 1:]
        text = text.rsplit("```", 1)[0].strip()
    if not text.startswith("{") and not text.startswith("["):
        for prefix in ("json", "JSON"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
    return text
