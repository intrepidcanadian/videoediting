"""Video provider registry — abstraction for multi-model routing.

Today we call Seedance directly from pipeline.run_shot. This module wraps that
call in a uniform `VideoProvider` interface, and adds STUBS for alternative
models (Veo, Kling, Runway, local Wan) so the pipeline can be routed to the best
model for each shot.

Why not concrete Veo/Kling/Runway adapters already? Each one needs its own API
key + SDK integration. Shipping them as stubs keeps the architecture set up so
adding a provider later is a 50-line drop-in (not a pipeline refactor).

Selection flow:
    state.params.video_provider = "seedance"   # default
    state.shots[i].provider_override = "veo"   # optional per-shot

The provider's `render_shot` signature is uniform; each implementation handles
its own auth + API call + mp4 download.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class VideoProvider(Protocol):
    """Contract every video-generation backend implements."""
    name: str
    label: str
    description: str
    supports_video_refs: bool
    max_image_refs: int
    max_video_refs: int
    min_duration_s: float
    max_duration_s: float

    def is_configured(self) -> bool:
        """Whether this provider has credentials + is ready to call. UI uses this
        to decide whether to show the option as selectable vs 'needs setup'."""
        ...

    async def render_shot(
        self,
        prompt: str,
        reference_images: list[Path],
        *,
        output_path: Path,
        ratio: str,
        duration: int,
        seed: Optional[int] = None,
        quality: Optional[str] = None,
        generate_audio: bool = False,
        reference_video_paths: Optional[list[Path]] = None,
        run_id: Optional[str] = None,
        **kwargs,
    ) -> Path:
        """Render one shot. Returns path to output mp4."""
        ...


# ─── Seedance (concrete, production) ─────────────────────────────────────

class SeedanceProvider:
    """Wraps the existing seedance.render_shot. Production-ready, fully tested."""
    name = "seedance"
    label = "Seedance 2.0"
    description = "BytePlus Ark — strong camera motion + photoreal, 3 quality tiers"
    supports_video_refs = True
    max_image_refs = 8
    max_video_refs = 3
    min_duration_s = 3.0
    max_duration_s = 12.0

    def is_configured(self) -> bool:
        import seedance
        return bool(seedance.KEYS)

    async def render_shot(self, prompt, reference_images, **kwargs):
        import seedance
        return await seedance.render_shot(prompt, reference_images, **kwargs)


# ─── Stubs (architecture scaffolding for future adapters) ────────────────

class _StubProvider:
    """Base for providers that aren't implemented yet. is_configured returns
    False so the UI greys them out; render_shot raises with a useful message."""
    label = "Not implemented"
    description = ""
    supports_video_refs = False
    max_image_refs = 4
    max_video_refs = 0
    min_duration_s = 2.0
    max_duration_s = 10.0

    def is_configured(self) -> bool:
        return False

    async def render_shot(self, prompt, reference_images, **kwargs):
        raise RuntimeError(
            f"Provider '{self.name}' is not yet implemented. "
            f"See providers.py — scaffolding is ready, the API adapter is the missing piece."
        )


class VeoProvider(_StubProvider):
    name = "veo"
    label = "Veo 3 (Google)"
    description = "Best for human performance + lip sync. Requires Vertex AI credentials."
    max_image_refs = 4
    max_video_refs = 1
    min_duration_s = 2.0
    max_duration_s = 8.0


class KlingProvider(_StubProvider):
    name = "kling"
    label = "Kling 2.5 (Kuaishou)"
    description = "Strong on action + human motion detail. Needs Kling API key."
    max_image_refs = 4
    max_video_refs = 1


class RunwayProvider(_StubProvider):
    name = "runway"
    label = "Runway Gen-3 + Act"
    description = "Motion-capture-driven performances. Needs Runway API key."


class WanLocalProvider(_StubProvider):
    name = "wan"
    label = "Wan 2.2 (local)"
    description = "Local, unrestricted, no API calls. Needs a beefy GPU."


# ─── Registry ────────────────────────────────────────────────────────────

_REGISTRY: dict[str, VideoProvider] = {
    "seedance": SeedanceProvider(),
    "veo":      VeoProvider(),
    "kling":    KlingProvider(),
    "runway":   RunwayProvider(),
    "wan":      WanLocalProvider(),
}


def get(name: str) -> VideoProvider:
    p = _REGISTRY.get(name)
    if p is None:
        # Default fallback so a bad name doesn't break a run
        return _REGISTRY["seedance"]
    return p


def list_providers() -> list[dict]:
    """UI-friendly list of all providers + their status."""
    return [
        {
            "id": name,
            "label": p.label,
            "description": p.description,
            "configured": p.is_configured(),
            "supports_video_refs": p.supports_video_refs,
            "max_image_refs": p.max_image_refs,
            "max_video_refs": p.max_video_refs,
            "duration_range": [p.min_duration_s, p.max_duration_s],
        }
        for name, p in _REGISTRY.items()
    ]


def default_provider_name() -> str:
    """Pick the first configured provider; falls back to seedance."""
    for name, p in _REGISTRY.items():
        if p.is_configured():
            return name
    return "seedance"
