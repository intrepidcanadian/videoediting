"""Image utilities shared across cloud-API callers.

Both Claude (5 MB/image base64 limit) and Gemini (similar limits) reject large
uploads. We resize to ≤1568px long-side and JPEG-encode at q88 — a near-lossless
drop in size that matches Anthropic's own vision-input recommendation.
"""

import io
from pathlib import Path

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def resize_for_api(data: bytes, max_side: int = 1568, quality: int = 88) -> tuple[bytes, str]:
    """Return (jpeg_bytes, mime) resized to ≤max_side and re-encoded as JPEG.

    Flattens alpha to white. Small images pass through unchanged dimensions but
    still get re-encoded so the caller doesn't need to branch on format.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode in ("RGBA", "LA"):
            bg.paste(img, mask=img.split()[-1])
        else:
            bg.paste(img.convert("RGBA"))
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), "image/jpeg"


def resize_path(path: Path, max_side: int = 1568, quality: int = 88) -> tuple[bytes, str]:
    """Convenience: read a path and resize."""
    return resize_for_api(path.read_bytes(), max_side=max_side, quality=quality)


HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif"}


def is_heic(data: bytes) -> bool:
    """Check if data bytes represent a HEIC/HEIF/AVIF image (ISO base-media with image brand)."""
    head = data[:12]
    return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in HEIC_BRANDS


def convert_heic_to_jpeg(data: bytes) -> tuple[bytes, str]:
    """Convert HEIC/AVIF bytes to high-quality JPEG. Returns (jpeg_bytes, ".jpg")."""
    jpeg_bytes, _ = resize_for_api(data, max_side=4096, quality=95)
    return jpeg_bytes, ".jpg"
