"""BytePlus Ark / Seedance 2.0 client.

Supports a pool of API keys so multiple shots render concurrently — each concurrent
task is issued under a different key to dodge per-key rate limits. Falls back to a
single key if only ARK_API_KEY is set.

Endpoint contract (from comfyuiseedance/server.py):
  POST {BASE}/contents/generations/tasks     → {"id": "<task_id>"}
  GET  {BASE}/contents/generations/tasks/{id} → {"status": "...", "content": [...]}

All reference images use role="reference_image". Mixing with first_frame/last_frame
role causes a "cannot be mixed with reference media content" error.
"""

import asyncio
import base64
import io
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

import anchors as anchors_mod
import prompt_rules
import retry as retry_mod
import textutils

load_dotenv(Path(__file__).parent / ".env", override=True)

ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3")
ARK_MODEL = os.getenv("ARK_MODEL", "dreamina-seedance-2-0-260128")

# Quality tier routing — Ark offers three Seedance 2.0 tiers at different cost/quality.
# Users can override any tier's model ID via env to match their deployment.
ARK_MODEL_FAST = os.getenv("ARK_MODEL_FAST", "dreamina-seedance-2-0-fast-250924")
ARK_MODEL_STANDARD = os.getenv("ARK_MODEL_STANDARD", ARK_MODEL)
ARK_MODEL_PRO = os.getenv("ARK_MODEL_PRO", "dreamina-seedance-2-0-pro-250924")

QUALITY_TIERS = {
    "fast":     {"model": ARK_MODEL_FAST,     "label": "Fast — draft quality, ~2x faster, ~0.5x cost"},
    "standard": {"model": ARK_MODEL_STANDARD, "label": "Standard — balanced (default)"},
    "pro":      {"model": ARK_MODEL_PRO,      "label": "Pro — 2K ready, ~1.5x slower, ~2x cost"},
}


def resolve_model(quality: Optional[str] = None) -> str:
    if not quality: return ARK_MODEL
    tier = QUALITY_TIERS.get(quality)
    return tier["model"] if tier else ARK_MODEL


POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))
TIMEOUT = int(os.getenv("TIMEOUT", "900"))


def _load_keys() -> list[str]:
    multi = os.getenv("ARK_API_KEYS", "")
    if multi:
        keys = [k.strip() for k in multi.split(",") if k.strip()]
        if keys:
            return keys
    single = os.getenv("ARK_API_KEY", "")
    return [single] if single else []


KEYS = _load_keys()
_key_idx = 0
_key_lock = threading.Lock()


def next_key() -> str:
    global _key_idx
    if not KEYS:
        raise RuntimeError(
            "No Ark API key configured. Set ARK_API_KEY or ARK_API_KEYS in .env."
        )
    with _key_lock:
        key = KEYS[_key_idx % len(KEYS)]
        _key_idx += 1
    return key


def image_to_data_url(data: bytes, max_side: int = 1024, quality: int = 88) -> str:
    """Resize → JPEG q88 → base64. Proven recipe from comfyuiseedance/server.py:1746."""
    from PIL import Image

    try:
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
        return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
    except Exception as e:
        print(f"[seedance] JPEG conversion failed, falling back to raw PNG: {e}", file=sys.stderr)
        return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


async def _create_task(client: httpx.AsyncClient, api_key: str, payload: dict) -> str:
    resp = await client.post(
        f"{ARK_BASE_URL}/contents/generations/tasks",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Ark task creation failed ({resp.status_code}): {textutils.safe_err_body(resp.text)}"
        )
    data = resp.json()
    task_id = data.get("id") or data.get("task_id")
    if not task_id:
        raise RuntimeError(f"Ark did not return task id: {textutils.safe_err_body(str(data))}")
    return task_id


async def _poll_task(client: httpx.AsyncClient, api_key: str, task_id: str) -> dict:
    elapsed = 0
    while elapsed < TIMEOUT:
        resp = await client.get(
            f"{ARK_BASE_URL}/contents/generations/tasks/{task_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Ark poll failed ({resp.status_code}): {textutils.safe_err_body(resp.text)}")
        data = resp.json()
        status = (data.get("status") or "").lower()
        if status in ("succeeded", "success", "completed"):
            return data
        if status in ("failed", "error", "cancelled", "canceled"):
            raise RuntimeError(f"Ark task failed: {textutils.safe_err_body(str(data))}")
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
    raise RuntimeError(f"Ark task {task_id} timed out after {TIMEOUT}s")


def _extract_video_url(result: dict) -> Optional[str]:
    if "video_url" in result:
        v = result["video_url"]
        return v.get("url") if isinstance(v, dict) else v
    content = result.get("content")
    if isinstance(content, dict) and "video_url" in content:
        v = content["video_url"]
        return v.get("url") if isinstance(v, dict) else v
    if isinstance(content, list):
        for item in content:
            if item.get("type") == "video_url":
                return item.get("video_url", {}).get("url")
    output = result.get("output")
    if isinstance(output, dict) and "video_url" in output:
        v = output["video_url"]
        return v.get("url") if isinstance(v, dict) else v
    return None


@retry_mod.retry_on_transient(tries=3, base=3.0, max_wait=60.0)
async def render_shot(
    prompt: str,
    reference_images: list[Path],
    *,
    output_path: Path,
    ratio: str = "16:9",
    duration: int = 5,
    generate_audio: bool = False,
    watermark: bool = False,
    seed: Optional[int] = None,
    api_key: Optional[str] = None,
    reference_video_path: Optional[Path] = None,
    reference_video_url: Optional[str] = None,
    reference_video_paths: Optional[list[Path]] = None,
    run_id: Optional[str] = None,
    quality: Optional[str] = None,
) -> Path:
    """Submit one Seedance task, poll to completion, download mp4 to output_path.

    Key choice: pass api_key to pin a specific pool key (e.g. for rotation across
    concurrent shots). Otherwise pulls the next key from the cycle.

    Video references:
      - reference_video_path: local mp4 file. Encoded as data URL in the payload.
      - reference_video_url: publicly reachable URL (Seedance's API pulls it).
      Seedance uses the reference video for camera motion / pacing (NOT content).
      Per their docs, the reference video must be <15s.
    """
    key = api_key or next_key()

    try:
        st = os.statvfs(str(output_path.parent.resolve()))
        free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
        if free_mb < 100:
            raise RuntimeError(f"low disk space ({free_mb:.0f} MB free) — need at least 100 MB for video rendering")
    except OSError as e:
        import sys
        print(f"[seedance] warning: disk space check failed ({e}), proceeding anyway", file=sys.stderr)

    # Apply deterministic prompt rules (strip transitions, clamp length, etc)
    tr = prompt_rules.transform(prompt, "seedance_motion")
    if tr["applied"] and run_id:
        try:
            import logger
            logger.info(run_id, "shots", f"motion-prompt rules applied: {', '.join(a['name'] for a in tr['applied'])}")
        except Exception:
            pass
    final_prompt = tr["transformed"]

    # Validate anchors + auto-prepend default if bare prompt with refs available
    all_video_paths: list[Path] = list(reference_video_paths or [])
    if reference_video_path is not None:
        all_video_paths.insert(0, reference_video_path)
    img_ct = min(8, len(reference_images or []))
    vid_ct = min(3, len(all_video_paths) + (1 if reference_video_url else 0))
    final_prompt = anchors_mod.auto_prepend_default(final_prompt, image_count=img_ct)
    warnings = anchors_mod.validate(final_prompt, image_count=img_ct, video_count=vid_ct)
    if warnings and run_id:
        try:
            import logger
            for w in warnings:
                logger.warn(run_id, "shots", f"anchor issue: {w}")
        except Exception:
            pass

    content: list[dict] = [{"type": "text", "text": final_prompt}]
    # BytePlus docs: Seedance 2.0 supports up to 9 reference images. 8 is our cap.
    _MAX_REF_BYTES = 100 * 1024 * 1024  # 100 MB safety cap
    for ref_path in reference_images[:8]:
        try:
            ref_size = ref_path.stat().st_size
            if ref_size > _MAX_REF_BYTES:
                print(f"[seedance] skipping oversized reference image {ref_path} ({ref_size} bytes)", file=sys.stderr)
                continue
            data = ref_path.read_bytes()
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_to_data_url(data)},
                    "role": "reference_image",
                }
            )
        except Exception as e:
            print(f"[seedance] skipping reference image {ref_path}: {e}", file=sys.stderr)
            continue

    # Video references — up to 3 per Ark docs. Accept the legacy single
    # reference_video_path, the new list reference_video_paths, or a public URL.
    videos_to_send: list[Path] = list(reference_video_paths or [])
    if reference_video_path is not None and reference_video_path not in videos_to_send:
        videos_to_send.insert(0, reference_video_path)
    videos_to_send = videos_to_send[:3]
    for vp in videos_to_send:
        try:
            vp_size = vp.stat().st_size
            if vp_size > _MAX_REF_BYTES:
                print(f"[seedance] skipping oversized video ref {vp} ({vp_size} bytes)", file=sys.stderr)
                continue
            vdata = vp.read_bytes()
            vdata_url = f"data:video/mp4;base64,{base64.b64encode(vdata).decode('ascii')}"
            content.append(
                {
                    "type": "video_url",
                    "video_url": {"url": vdata_url},
                    "role": "reference_video",
                }
            )
        except Exception as e:
            if run_id:
                try:
                    import logger
                    logger.warn(run_id, "shots", f"skipping video ref {vp.name}: {e}")
                except Exception:
                    print(f"[seedance] skipping video ref {vp.name}: {e}", file=sys.stderr)
            else:
                print(f"[seedance] skipping video ref {vp.name}: {e}", file=sys.stderr)
            continue
    if not videos_to_send and reference_video_url:
        content.append(
            {
                "type": "video_url",
                "video_url": {"url": reference_video_url},
                "role": "reference_video",
            }
        )

    has_ref = any(c.get("type") in ("image_url", "video_url") for c in content)
    if not has_ref:
        raise RuntimeError("render_shot: no valid reference images or video — Seedance r2v needs at least one")

    if ratio not in ("16:9", "9:16", "1:1"):
        print(f"WARNING: Seedance ratio '{ratio}' is non-standard — 16:9, 9:16, 1:1 are the well-tested options", file=sys.stderr)

    payload: dict = {
        "model": resolve_model(quality),
        "content": content,
        "generate_audio": generate_audio,
        "ratio": ratio,
        "duration": duration,
        "watermark": watermark,
    }
    if seed is not None:
        payload["seed"] = seed

    timeout = httpx.Timeout(connect=30.0, read=120.0, write=600.0, pool=60.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        task_id = await _create_task(client, key, payload)
        result = await _poll_task(client, key, task_id)

    video_url = _extract_video_url(result)
    if not video_url:
        print(f"[seedance] no video_url in result: {str(result)[:500]}", file=sys.stderr)
        raise RuntimeError("Seedance render completed but returned no video URL")

    async with httpx.AsyncClient(timeout=180.0) as client:
        vresp = await client.get(video_url)
        vresp.raise_for_status()
        vdata = vresp.content
        if len(vdata) < 12 or (vdata[4:8] != b"ftyp" and vdata[:4] != b"\x1a\x45\xdf\xa3"):
            raise RuntimeError(f"Seedance download is not a valid video ({len(vdata)} bytes, magic={vdata[:8]!r})")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        try:
            tmp.write_bytes(vdata)
            tmp.rename(output_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    return output_path
