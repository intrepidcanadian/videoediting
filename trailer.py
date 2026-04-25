#!/usr/bin/env python3
"""Trailer maker: concept → storyboard → keyframes → Seedance shots → stitched trailer.

Examples:
  # Fresh trailer from a concept string:
  python trailer.py "A lone detective hunts a ghost through 1940s Shanghai"

  # Longer trailer with style + character references:
  python trailer.py --concept-file concept.txt --shots 8 --shot-duration 6 \
      --style "Roger Deakins lighting, Dune (2021) palette" \
      --ref-image ./portrait.jpg --ref-image ./location.jpg \
      --title "Shanghai Ghost"

  # Vertical trailer for Shorts/Reels:
  python trailer.py "..." --ratio 9:16 --shots 5

  # Resume a partial run:
  python trailer.py --resume outputs/20260422_153012_shanghai_ghost
"""

import argparse
import asyncio
import sys
from pathlib import Path

import pipeline


def main():
    ap = argparse.ArgumentParser(
        description="Generate cinematic trailers via Nano Banana keyframes + Seedance 2.0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("concept", nargs="?", help="Concept text (or use --concept-file)")
    ap.add_argument("--concept-file", type=Path, help="Read concept from a file")

    ap.add_argument("--shots", type=int, default=6, help="Number of shots (3–10). Default 6.")
    ap.add_argument(
        "--shot-duration",
        type=int,
        default=5,
        help="Target seconds per shot (3–10). Default 5.",
    )
    ap.add_argument(
        "--ratio",
        default="16:9",
        choices=["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        help="Aspect ratio. Default 16:9.",
    )
    ap.add_argument("--style", default="", help="Style intent (e.g. 'Blade Runner neon noir').")
    ap.add_argument(
        "--ref-image",
        action="append",
        type=Path,
        help="Reference image for identity (repeatable). Used for every keyframe.",
    )
    ap.add_argument(
        "--title",
        default="",
        help="Trailer title — used in output folder name.",
    )
    ap.add_argument(
        "--crossfade",
        action="store_true",
        help="Crossfade between shots (re-encodes). Default: hard cuts.",
    )
    ap.add_argument(
        "--resume",
        type=Path,
        help="Resume a partial run from its output directory.",
    )

    args = ap.parse_args()

    if args.resume:
        asyncio.run(pipeline.resume_trailer(args.resume, crossfade=args.crossfade or None))
        return

    concept = args.concept
    if args.concept_file:
        concept = args.concept_file.read_text(encoding="utf-8")
    if not concept or not concept.strip():
        ap.error("concept required (positional arg, --concept-file, or --resume)")

    if args.shots < 3 or args.shots > 10:
        ap.error("--shots must be between 3 and 10")
    if args.shot_duration < 3 or args.shot_duration > 10:
        ap.error("--shot-duration must be between 3 and 10")

    for p in args.ref_image or []:
        if not p.exists():
            ap.error(f"reference image not found: {p}")

    try:
        asyncio.run(
            pipeline.make_trailer(
                concept=concept,
                num_shots=args.shots,
                shot_duration=args.shot_duration,
                ratio=args.ratio,
                style_intent=args.style,
                reference_images=args.ref_image or [],
                title=args.title,
                crossfade=args.crossfade,
            )
        )
    except KeyboardInterrupt:
        print("\n✗ interrupted. Resume with --resume <output_dir>", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
