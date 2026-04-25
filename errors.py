"""Shared exception hierarchy for the trailer pipeline.

Most modules historically raise plain RuntimeError or ValueError. Using these
semantic types instead lets the HTTP layer map errors to correct status codes
and lets callers catch only the class of failure they can recover from.

Mapping convention used in server.py:
  TrailerError         → 500 (generic internal)
  TrailerUserError     → 400 (bad input / prerequisite)
  TrailerNotReady      → 409 (preconditions not met, e.g. "storyboard not generated yet")
  TrailerNotFound      → 404
  ExternalServiceError → 502 (Claude / Seedance / ElevenLabs failure)
"""


class TrailerError(Exception):
    """Base class for all pipeline-specific errors."""


class TrailerUserError(TrailerError):
    """Invalid input or a precondition the user can fix (bad shot_idx, malformed JSON, etc.)."""


class TrailerNotFound(TrailerError):
    """A run, shot, asset, or other referenced entity does not exist."""


class TrailerNotReady(TrailerError):
    """A workflow step ran before its prerequisite (e.g. shots before keyframes)."""


class ExternalServiceError(TrailerError):
    """An upstream AI service (Claude, Seedance, ElevenLabs, Nano Banana) failed."""
