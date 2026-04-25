"""E-commerce URL importer — fetches a product page, has Claude extract structured
product info, downloads hero images, and returns a packet the create-run form can
auto-populate.

Why Claude vs. site-specific scrapers: every Shopify/WooCommerce/Amazon/Etsy/custom
storefront lays out HTML differently. Claude is tolerant of layout variation, can
read JSON-LD + OpenGraph + visible text simultaneously, and writes the ad concept
in one pass. Cost is ~$0.01-0.03 per extraction — cheap relative to the trailer
render that follows.

Why not Playwright: heavy dependency for marginal coverage gain. Most product pages
ship OG meta + JSON-LD product schema in static HTML. JS-only sites fall back to
manual entry — acceptable for v1.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv

import imgutils
import textutils

load_dotenv(Path(__file__).parent / ".env", override=True)

from constants import ANTHROPIC_MODEL, MAX_TOKENS_IDEATE, MAX_REF_IMAGE_BYTES

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Hard caps to keep the Claude call bounded and avoid pathological pages.
_MAX_HTML_BYTES = 1 * 1024 * 1024          # 1 MB of HTML is plenty after stripping
_MAX_IMAGE_DOWNLOAD_BYTES = 8 * 1024 * 1024  # per image
_MAX_IMAGES_RETURNED = 4                    # we only need a few hero shots
_FETCH_TIMEOUT_S = 20.0

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


_EXTRACT_SYSTEM = """You are an ad creative director. The user pastes a product page; you read the HTML and turn it into a brief that a 6-shot cinematic ad trailer could be built from.

You are tolerant of messy HTML — read JSON-LD product schema, OpenGraph tags, visible product copy, and pricing/badges all at once. If the page is clearly NOT a product (a category list, a 404, a paywalled article), say so in `extraction_notes` and leave the product fields blank.

Output ONLY valid JSON, no markdown fences, no prose."""


def _extract_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "product_name": {"type": "string", "description": "The product's name. Empty if not a product page."},
            "brand": {"type": "string", "description": "Brand or seller name. Empty if unknown."},
            "price": {"type": "string", "description": "Price as displayed (with currency). Empty if unknown."},
            "category": {"type": "string", "description": "One short phrase: 'wireless earbuds', 'leather tote', 'smart kettle'."},
            "key_selling_points": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-6 short bullets — features, materials, occasion, audience. Direct from the copy.",
            },
            "image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 4 absolute URLs to high-quality product hero images. Prefer JSON-LD `image` field, then OG image, then largest <img> tags. Skip sprite sheets, icons, logos, payment badges.",
            },
            "ad_concept": {
                "type": "string",
                "description": "2-3 paragraphs. Cinematic ad concept for THIS product. Lead with a visual hook — one image that stops you scrolling. Specific tone, era, palette. Treat the product as the hero. Show, don't list features.",
            },
            "style_intent": {
                "type": "string",
                "description": "Aesthetic reference: cinematographer / film / palette. E.g. 'Apple-style minimal product photography, soft top light, deep matte blacks, anamorphic lens flares for highlights'.",
            },
            "suggested_title": {"type": "string", "description": "2-4 word ad title or tagline. Punchy."},
            "suggested_shots": {"type": "integer", "minimum": 4, "maximum": 8},
            "suggested_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]},
            "extraction_notes": {"type": "string", "description": "One sentence — what you found, or why this isn't a product page."},
        },
        "required": [
            "product_name", "brand", "price", "category", "key_selling_points",
            "image_urls", "ad_concept", "style_intent", "suggested_title",
            "suggested_shots", "suggested_ratio", "extraction_notes",
        ],
    }


def _strip_html(html: str) -> str:
    """Remove <script>/<style> blocks (keep JSON-LD), comments, and excess whitespace
    so we send Claude semantic content not boilerplate. Keeps JSON-LD because it
    carries structured product data."""
    # Drop <script type="text/javascript"> and <style> but preserve JSON-LD.
    def _script_repl(m: re.Match) -> str:
        block = m.group(0)
        if 'application/ld+json' in block.lower():
            return block
        return ''
    html = re.sub(r'<script\b[^>]*>.*?</script>', _script_repl, html, flags=re.S | re.I)
    html = re.sub(r'<style\b[^>]*>.*?</style>', '', html, flags=re.S | re.I)
    html = re.sub(r'<!--.*?-->', '', html, flags=re.S)
    # Collapse whitespace
    html = re.sub(r'\s+', ' ', html)
    return html.strip()


def _absolute_url(base: str, maybe_relative: str) -> str:
    """Turn a possibly-relative image URL into an absolute one."""
    if not maybe_relative:
        return ""
    if maybe_relative.startswith(('http://', 'https://', 'data:')):
        return maybe_relative
    if maybe_relative.startswith('//'):
        return urlparse(base).scheme + ':' + maybe_relative
    return urljoin(base, maybe_relative)


def fetch_html(url: str) -> str:
    """Fetch a URL and return cleaned HTML. Raises ValueError on bad URL,
    RuntimeError on fetch errors."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r} (must be http/https)")
    if not parsed.netloc:
        raise ValueError("invalid URL — missing host")

    import httpx
    try:
        with httpx.Client(
            timeout=_FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            r = client.get(url)
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"failed to fetch {url}: {e}") from e

    raw = r.text[:_MAX_HTML_BYTES * 2]  # read up to 2x to allow stripping
    cleaned = _strip_html(raw)
    return cleaned[:_MAX_HTML_BYTES]


def _download_image(url: str) -> Optional[tuple[str, bytes]]:
    """Download one image. Returns (filename, bytes) or None on any failure.
    Caps at _MAX_IMAGE_DOWNLOAD_BYTES."""
    if not url or url.startswith('data:'):
        return None
    import httpx
    try:
        with httpx.Client(
            timeout=_FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.content
    except (httpx.HTTPError, ValueError):
        return None
    if not data or len(data) > _MAX_IMAGE_DOWNLOAD_BYTES:
        return None
    # Sniff: must be a recognised image (PNG/JPEG/WEBP/GIF/HEIC).
    head = data[:32]
    is_image = (
        head[:8] == b'\x89PNG\r\n\x1a\n'
        or head[:3] == b'\xff\xd8\xff'
        or head[:6] in (b'GIF87a', b'GIF89a')
        or (head[:4] == b'RIFF' and len(head) >= 12 and head[8:12] == b'WEBP')
        or imgutils.is_heic(data)
    )
    if not is_image:
        return None
    # If HEIC, convert to JPEG so the rest of the pipeline can handle it.
    if imgutils.is_heic(data):
        try:
            data, _ = imgutils.convert_heic_to_jpeg(data)
            ext = '.jpg'
        except Exception:
            return None
    else:
        ext = _ext_for_image(head)
    # Re-encode oversized images down to fit the reference cap.
    if len(data) > MAX_REF_IMAGE_BYTES:
        try:
            data, _ = imgutils.resize_for_api(data, max_side=2048, quality=88)
            ext = '.jpg'
        except Exception:
            return None
    # Synthesize a filename from the URL stem.
    stem = Path(urlparse(url).path).stem or 'product'
    stem = re.sub(r'[^a-zA-Z0-9_-]', '_', stem)[:40] or 'product'
    return (f"{stem}{ext}", data)


def _ext_for_image(head: bytes) -> str:
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png'
    if head[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if head[:4] == b'RIFF':
        return '.webp'
    return '.jpg'


def extract_product(url: str, run_id: str = "_ecommerce") -> dict:
    """Full pipeline: fetch URL → Claude extracts → download top images.

    Returns a dict with product info, an `images` field of [{filename, b64}],
    and a `concept`/`style_intent`/`title`/`num_shots`/`ratio` block ready to
    drop into the create-run form.

    Raises ValueError on bad URL, RuntimeError on fetch/parse errors.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")

    html = fetch_html(url)
    if not html:
        raise RuntimeError("page returned empty body")

    from anthropic import Anthropic
    import httpx
    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=httpx.Timeout(120.0, connect=30.0))
    try:
        schema = _extract_schema()
        user_msg = f"""# PRODUCT PAGE URL
{url}

# PAGE HTML (cleaned — scripts and styles stripped, JSON-LD preserved)
{html}

# TASK
Read the HTML and extract the product. Then write a cinematic ad brief for it.

The ad should NOT be a feature list. Pick one or two visual hooks the product enables (the moment it gets used, the texture of the material, the satisfaction of a result) and build around that. Specific lighting, palette, and energy — like a real ad director would brief.

Return JSON matching this schema:
{json.dumps(schema, indent=2)}

Return ONLY the JSON object."""

        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS_IDEATE * 2,
            system=[{"type": "text", "text": _EXTRACT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )

        try:
            import costs
            usage = resp.usage
            costs.log_text(
                run_id,
                model=ANTHROPIC_MODEL, phase="ecommerce",
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
        except Exception as e:
            print(f"[costs] ecommerce cost log failed: {e}", file=sys.stderr)

        raw = textutils.strip_json_fences(textutils.resp_text(resp.content))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Extraction returned non-JSON: {e}\n---\n{textutils.sanitize_for_log(raw)}"
            )
    finally:
        try: client.close()
        except Exception: pass

    if not data.get("product_name"):
        # Not a product page — return what we got, let the UI show the note.
        data["images"] = []
        return data

    # Resolve relative image URLs against the page URL, then download.
    image_urls = [_absolute_url(url, u) for u in (data.get("image_urls") or [])]
    image_urls = [u for u in image_urls if u][:_MAX_IMAGES_RETURNED]
    images = []
    for img_url in image_urls:
        result = _download_image(img_url)
        if result is None:
            continue
        filename, img_bytes = result
        images.append({
            "filename": filename,
            "b64": base64.b64encode(img_bytes).decode("ascii"),
            "source_url": img_url,
        })
    data["images"] = images
    data["resolved_image_urls"] = image_urls
    return data
