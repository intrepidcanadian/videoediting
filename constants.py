"""Centralized configuration.

Historically each module ran `os.getenv(...)` with its own default at import
time, which meant rotating a model name was an 8-file hunt. This module is the
single source of truth — update here, every caller follows.

Do not put secrets here; those stay module-local (and we don't log them).
Put anything that is:
  - A model / API version string
  - A timeout or retry budget
  - A size / length cap
  - A rate-limit threshold
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env once, early. All downstream modules that `import constants`
# transitively get this side effect.
load_dotenv(Path(__file__).parent / ".env", override=True)


# ─── Model names ─────────────────────────────────────────────────────────

# Anthropic Claude — used by storyboard, review, director, ideate, audio (VO
# script + music brief), assets discovery, taste summarization.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Google Gemini 2.5 Flash Image (Nano Banana) — keyframe generation + edit.
# Preview models have rotated once already; verify before pinning.
NANO_BANANA_MODEL = os.getenv("NANO_BANANA_MODEL", "gemini-2.5-flash-image")

# ElevenLabs voice model for TTS.
ELEVENLABS_VOICE_MODEL = os.getenv("ELEVENLABS_VOICE_MODEL", "eleven_multilingual_v2")

# BytePlus Ark Seedance 2.0 video model.
ARK_VIDEO_MODEL = os.getenv("ARK_VIDEO_MODEL", "seedance-2-0-pro")


# ─── API base URLs ───────────────────────────────────────────────────────

GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
ELEVENLABS_BASE_URL = os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io/v1")


# ─── Token / length caps ─────────────────────────────────────────────────

# Maximum user-supplied prompt length at the API boundary. Applies to keyframe
# prompts, motion prompts, edit descriptions, VO lines.
MAX_PROMPT_LEN = int(os.getenv("MAX_PROMPT_LEN", "4000"))

# Claude max_tokens for each phase.
MAX_TOKENS_STORYBOARD = int(os.getenv("MAX_TOKENS_STORYBOARD", "4000"))
MAX_TOKENS_ASSETS = int(os.getenv("MAX_TOKENS_ASSETS", "3000"))
MAX_TOKENS_IDEATE = int(os.getenv("MAX_TOKENS_IDEATE", "1500"))
MAX_TOKENS_REVIEW = int(os.getenv("MAX_TOKENS_REVIEW", "6000"))
MAX_TOKENS_DIRECTOR = int(os.getenv("MAX_TOKENS_DIRECTOR", "2000"))
MAX_TOKENS_AUDIO = int(os.getenv("MAX_TOKENS_AUDIO", "1500"))


# ─── Timeouts (seconds) ──────────────────────────────────────────────────

# Default per-request timeout for Claude text calls.
CLAUDE_TIMEOUT_S = int(os.getenv("CLAUDE_TIMEOUT_S", "300"))
# Gemini image generation / edit.
GEMINI_TIMEOUT_S = int(os.getenv("GEMINI_TIMEOUT_S", "120"))
# Seedance task poll ceiling — video gen can legitimately take minutes.
ARK_TASK_TIMEOUT_S = int(os.getenv("ARK_TASK_TIMEOUT_S", "900"))
# Seedance result download.
ARK_DOWNLOAD_TIMEOUT_S = int(os.getenv("ARK_DOWNLOAD_TIMEOUT_S", "180"))


# ─── Size caps (bytes) ───────────────────────────────────────────────────

MAX_REF_IMAGE_BYTES = int(os.getenv("MAX_REF_IMAGE_BYTES", str(10 * 1024 * 1024)))
MAX_REF_IMAGES_TOTAL_BYTES = int(os.getenv("MAX_REF_IMAGES_TOTAL_BYTES", str(30 * 1024 * 1024)))
MAX_VIDEO_UPLOAD_BYTES = int(os.getenv("MAX_VIDEO_UPLOAD_BYTES", str(500 * 1024 * 1024)))
MAX_AUDIO_UPLOAD_BYTES = int(os.getenv("MAX_AUDIO_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_ASSET_BYTES = int(os.getenv("MAX_ASSET_BYTES", str(10 * 1024 * 1024)))

# Claude image input ceiling (they reject > 5 MB after base64 encoding).
# We downscale anything larger via imgutils.resize_for_api().
CLAUDE_IMAGE_MAX_SIDE = int(os.getenv("CLAUDE_IMAGE_MAX_SIDE", "1568"))


# ─── Video / pipeline heuristics ─────────────────────────────────────────

# Seedance rejects video references longer than 15s. We auto-trim at this limit.
SEEDANCE_MAX_REF_VIDEO_S = int(os.getenv("SEEDANCE_MAX_REF_VIDEO_S", "15"))

# Maximum video reference slots per shot (Seedance API cap).
SEEDANCE_MAX_VIDEO_REFS = int(os.getenv("SEEDANCE_MAX_VIDEO_REFS", "3"))

# Maximum image reference slots per shot (Nano Banana practical cap).
NANO_BANANA_MAX_IMAGE_REFS = int(os.getenv("NANO_BANANA_MAX_IMAGE_REFS", "8"))

# ElevenLabs TTS pricing (USD per 1000 characters).
ELEVENLABS_TTS_PRICE_PER_1K_CHARS = float(os.getenv("ELEVENLABS_TTS_PRICE_PER_1K_CHARS", "0.30"))


__all__ = [
    "ANTHROPIC_MODEL", "NANO_BANANA_MODEL", "ELEVENLABS_VOICE_MODEL", "ARK_VIDEO_MODEL",
    "GEMINI_BASE_URL", "ARK_BASE_URL", "ELEVENLABS_BASE_URL",
    "MAX_PROMPT_LEN",
    "MAX_TOKENS_STORYBOARD", "MAX_TOKENS_ASSETS", "MAX_TOKENS_IDEATE",
    "MAX_TOKENS_REVIEW", "MAX_TOKENS_DIRECTOR", "MAX_TOKENS_AUDIO",
    "CLAUDE_TIMEOUT_S", "GEMINI_TIMEOUT_S", "ARK_TASK_TIMEOUT_S", "ARK_DOWNLOAD_TIMEOUT_S",
    "MAX_REF_IMAGE_BYTES", "MAX_REF_IMAGES_TOTAL_BYTES",
    "MAX_VIDEO_UPLOAD_BYTES", "MAX_AUDIO_UPLOAD_BYTES", "MAX_ASSET_BYTES",
    "CLAUDE_IMAGE_MAX_SIDE",
    "SEEDANCE_MAX_REF_VIDEO_S", "SEEDANCE_MAX_VIDEO_REFS", "NANO_BANANA_MAX_IMAGE_REFS",
    "ELEVENLABS_TTS_PRICE_PER_1K_CHARS",
]
