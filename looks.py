"""Named color-grade "looks" applied at stitch time via ffmpeg filters.

Two modes:
  - Named presets using ffmpeg's built-in `eq` / `curves` / `colorbalance` /
    `hue` filters. No external files needed; fast; cross-platform.
  - LUT file mode (future): user drops a .cube into `luts/`, we use `lut3d=`.

Looks reference real films + cinematographers' signature grades (crowd-sourced +
community-validated values). Applied as a post-stitch filter chain right before
audio mux.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# Each look is an ffmpeg filter chain applied to video stream.
# Values tuned against the named reference films — not slavish copies, but the
# signature move that makes each look recognizable.
LOOKS = {
    "none": {
        "label": "None (raw)",
        "description": "No grade — whatever Seedance gave us",
        "filter": None,
    },
    "dune": {
        "label": "Dune (2021) — Fraser",
        "description": "Bronze highlights, crushed deep shadows, desert ochre",
        "filter": (
            "colorbalance=rm=0.08:gm=-0.03:bm=-0.12:"
            "rh=0.12:gh=0.04:bh=-0.08:"
            "rs=0.05:gs=-0.02:bs=-0.10,"
            "curves=all='0/0 0.3/0.22 0.7/0.78 1/1',"
            "eq=saturation=0.92:gamma=0.96"
        ),
    },
    "bladerunner2049": {
        "label": "Blade Runner 2049 — Deakins",
        "description": "Amber / teal split, lifted blacks, neon warmth",
        "filter": (
            "colorbalance=rs=0.08:bs=-0.08:rh=-0.05:bh=0.15:rm=0.06:bm=-0.05,"
            "curves=all='0/0.03 0.25/0.25 0.5/0.55 0.75/0.8 1/0.95',"
            "eq=saturation=0.95:gamma=1.02"
        ),
    },
    "drive": {
        "label": "Drive (2011) — Sigel",
        "description": "Pink / magenta neon, electric cyan, high contrast",
        "filter": (
            "colorbalance=rh=0.1:bh=0.08:gh=-0.05:"
            "rm=0.04:bm=0.02:gs=-0.05,"
            "curves=all='0/0 0.2/0.15 0.8/0.88 1/1',"
            "eq=saturation=1.12:contrast=1.08"
        ),
    },
    "moonlight": {
        "label": "Moonlight — Laxton",
        "description": "Skin-tone protect + cool cyan shadows, velvet blacks",
        "filter": (
            "colorbalance=bs=0.12:gs=0.03:rs=-0.05:"
            "rm=0.03:bm=0.03,"
            "curves=all='0/0 0.3/0.24 0.7/0.76 1/1',"
            "eq=saturation=1.02"
        ),
    },
    "joker": {
        "label": "Joker (2019) — Sher",
        "description": "Bilious sickly green, crushed blacks, yellow skin",
        "filter": (
            "colorbalance=gm=0.08:gh=0.05:rm=-0.02:bm=-0.06:bh=-0.05,"
            "curves=all='0/0.02 0.5/0.52 1/0.96',"
            "eq=saturation=0.88:contrast=1.1"
        ),
    },
    "the_batman": {
        "label": "The Batman (2022) — Fraser",
        "description": "Blood-orange skies, nearly-black shadows, dense fog",
        "filter": (
            "colorbalance=rh=0.15:gh=0.02:bh=-0.15:"
            "rs=-0.05:gs=-0.08:bs=-0.15,"
            "curves=all='0/0 0.4/0.3 0.8/0.78 1/0.9',"
            "eq=saturation=0.8:contrast=1.15:gamma=0.92"
        ),
    },
    "mad_max_fury_road": {
        "label": "Mad Max: Fury Road — Seale",
        "description": "Hyper-saturated orange + turquoise, sand + sky",
        "filter": (
            "colorbalance=rh=0.18:gh=-0.03:bh=-0.18:"
            "rs=-0.08:gs=-0.02:bs=0.12,"
            "curves=all='0/0 0.3/0.28 0.7/0.76 1/1',"
            "eq=saturation=1.35:contrast=1.1"
        ),
    },
    "a24_horror": {
        "label": "A24 Horror — desaturated pallid",
        "description": "Pastor, Hereditary, Midsommar vibe: desat, muted, clinical",
        "filter": (
            "colorbalance=rm=0.02:gm=0.02:bm=0.02,"
            "curves=all='0/0.02 0.5/0.5 1/0.97',"
            "eq=saturation=0.68:contrast=1.04"
        ),
    },
    "kodak_2383": {
        "label": "Kodak 2383 print emulation",
        "description": "Generic film-print look — warm shadows, toe roll-off",
        "filter": (
            "colorbalance=rs=0.08:bs=-0.05:rh=-0.03:bh=-0.05,"
            "curves=all='0/0.02 0.1/0.08 0.9/0.88 1/0.97',"
            "eq=saturation=0.95:gamma=0.98"
        ),
    },
    "teal_orange": {
        "label": "Hollywood teal + orange",
        "description": "Generic commercial blockbuster grade",
        "filter": (
            "colorbalance=rh=0.12:bh=-0.1:gm=-0.03:"
            "rs=-0.08:bs=0.12,"
            "eq=saturation=1.15:contrast=1.05"
        ),
    },
    "bleach_bypass": {
        "label": "Bleach bypass — Saving Private Ryan",
        "description": "Desaturated, high contrast, silver-retained look",
        "filter": (
            "curves=all='0/0 0.3/0.2 0.7/0.8 1/1',"
            "eq=saturation=0.35:contrast=1.2"
        ),
    },
}


def list_looks() -> list[dict]:
    """UI-friendly list of available looks."""
    return [
        {"id": k, "label": v["label"], "description": v["description"]}
        for k, v in LOOKS.items()
    ]


def get_filter(look_id: str) -> Optional[str]:
    """Return the ffmpeg filter string for a named look, or None for 'none' / unknown."""
    look = LOOKS.get(look_id)
    if not look:
        return None
    return look.get("filter")
