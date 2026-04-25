"""Tests for forty-first review improvements: HEIC/AVIF upload support (magic
byte detection + auto-conversion), refs-used UI data availability, cast
per-shot matrix data, and batch asset library promotion endpoint."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))


# ── HEIC/AVIF magic byte detection ──────────────────────────────────────


class TestHeicDetection:

    def test_heic_detected_as_image(self):
        import server
        # HEIC: ftyp at offset 4, brand "heic" at offset 8
        data = b"\x00\x00\x00\x20ftypheic" + b"\x00" * 16
        assert server._sniff_kind(data) == "image"

    def test_heix_detected_as_image(self):
        import server
        data = b"\x00\x00\x00\x20ftypheix" + b"\x00" * 16
        assert server._sniff_kind(data) == "image"

    def test_avif_detected_as_image(self):
        import server
        data = b"\x00\x00\x00\x20ftypavif" + b"\x00" * 16
        assert server._sniff_kind(data) == "image"

    def test_mif1_detected_as_image(self):
        import server
        data = b"\x00\x00\x00\x20ftypmif1" + b"\x00" * 16
        assert server._sniff_kind(data) == "image"

    def test_hevc_detected_as_image(self):
        import server
        data = b"\x00\x00\x00\x20ftyphevc" + b"\x00" * 16
        assert server._sniff_kind(data) == "image"

    def test_mp4_isom_still_video(self):
        """MP4 with 'isom' brand should be detected as video, not HEIC image."""
        import server
        data = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16
        assert server._sniff_kind(data) == "video"

    def test_m4a_brand_detected_as_audio(self):
        import server
        data = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 16
        assert server._sniff_kind(data) == "audio"

    def test_mp4_avc1_detected_as_video(self):
        """Standard video MP4 brands fall through to video."""
        import server
        data = b"\x00\x00\x00\x20ftypavc1" + b"\x00" * 16
        assert server._sniff_kind(data) == "video"

    def test_is_heic_helper(self):
        import imgutils
        assert imgutils.is_heic(b"\x00\x00\x00\x20ftypheic" + b"\x00" * 4)
        assert imgutils.is_heic(b"\x00\x00\x00\x20ftypavif" + b"\x00" * 4)
        assert not imgutils.is_heic(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4)
        assert not imgutils.is_heic(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        assert not imgutils.is_heic(b"short")

    def test_heic_mime_types_in_allowed_asset_list(self):
        import server
        allowed = server._ALLOWED_ASSET_MIMES
        assert "image/heic" in allowed
        assert "image/heif" in allowed
        assert "image/avif" in allowed


# ── Batch asset library promotion ────────────────────────────────────────


fastapi_testclient = pytest.importorskip("fastapi.testclient", reason="fastapi not installed")


@pytest.fixture
def client(tmp_output_root):
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app) as c:
        yield c


def _create_run_with_assets(tmp_output_root, assets=None, run_id="test_batch_run"):
    run_dir = tmp_output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "run_id": run_id,
        "status": "storyboard_ready",
        "story": {"title": "Test", "shots": [{"beat": "intro", "keyframe_prompt": "A test"}]},
        "params": {"ratio": "16:9"},
        "references": [],
        "keyframes": [],
        "shots": [],
        "assets": assets or [],
    }
    (run_dir / "state.json").write_text(json.dumps(state))
    return run_id


class TestBatchPromoteAll:

    def test_promote_all_skips_pending(self, client, tmp_output_root, tmp_library_root):
        run_id = _create_run_with_assets(tmp_output_root, assets=[
            {"id": "a1", "name": "Pending Logo", "type": "logo", "status": "pending", "path": None},
        ])
        resp = client.post(f"/api/runs/{run_id}/assets/promote-all")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["promoted"]) == 0
        assert len(body["errors"]) == 0

    def test_promote_all_promotes_uploaded_and_generated(self, client, tmp_output_root, tmp_library_root):
        run_id = _create_run_with_assets(tmp_output_root, assets=[
            {"id": "a1", "name": "Company Logo", "type": "logo",
             "status": "uploaded", "path": "assets/a1.png", "description": "The logo"},
            {"id": "a2", "name": "Elena", "type": "character",
             "status": "generated", "path": "assets/a2.png", "description": "Main character"},
            {"id": "a3", "name": "Skipped Item", "type": "prop",
             "status": "skipped", "path": None},
        ])
        run_dir = tmp_output_root / run_id
        assets_dir = run_dir / "assets"
        assets_dir.mkdir(exist_ok=True)
        (assets_dir / "a1.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (assets_dir / "a2.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        resp = client.post(f"/api/runs/{run_id}/assets/promote-all")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["promoted"]) == 2
        kinds = {p["kind"] for p in body["promoted"]}
        assert "props" in kinds
        assert "characters" in kinds

    def test_promote_all_maps_location_type(self, client, tmp_output_root, tmp_library_root):
        run_id = _create_run_with_assets(tmp_output_root, assets=[
            {"id": "loc1", "name": "Castle", "type": "location",
             "status": "uploaded", "path": "assets/loc1.png", "description": "A castle"},
        ])
        run_dir = tmp_output_root / run_id
        (run_dir / "assets").mkdir(exist_ok=True)
        (run_dir / "assets" / "loc1.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        resp = client.post(f"/api/runs/{run_id}/assets/promote-all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["promoted"][0]["kind"] == "locations"

    def test_promote_all_run_not_found(self, client, tmp_output_root):
        resp = client.post("/api/runs/nonexistent_run/assets/promote-all")
        assert resp.status_code == 404

    def test_promote_all_missing_file_still_creates_entry(self, client, tmp_output_root, tmp_library_root):
        """Assets with paths that don't exist on disk are still promoted (empty library item)."""
        run_id = _create_run_with_assets(tmp_output_root, assets=[
            {"id": "a1", "name": "Ghost", "type": "prop",
             "status": "uploaded", "path": "assets/ghost.png", "description": "Missing file"},
        ])
        resp = client.post(f"/api/runs/{run_id}/assets/promote-all")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["promoted"]) == 1
        assert body["promoted"][0]["kind"] == "props"

    def test_promote_all_no_path_skipped(self, client, tmp_output_root, tmp_library_root):
        """Assets with status uploaded but path=None are skipped."""
        run_id = _create_run_with_assets(tmp_output_root, assets=[
            {"id": "a1", "name": "No Path", "type": "prop",
             "status": "uploaded", "path": None, "description": "No file"},
        ])
        resp = client.post(f"/api/runs/{run_id}/assets/promote-all")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["promoted"]) == 0


# ── HEIC conversion integration ─────────────────────────────────────────


class TestHeicConversion:

    def test_convert_heic_to_jpeg_via_imgutils(self):
        """imgutils.convert_heic_to_jpeg returns jpeg bytes and .jpg extension."""
        import imgutils
        # We can't easily construct real HEIC bytes, but we can test that
        # the function delegates to resize_for_api with correct params.
        with patch.object(imgutils, 'resize_for_api', return_value=(b"fake_jpeg", "image/jpeg")) as mock:
            result_bytes, result_ext = imgutils.convert_heic_to_jpeg(b"fake_heic_data")
            assert result_bytes == b"fake_jpeg"
            assert result_ext == ".jpg"
            mock.assert_called_once_with(b"fake_heic_data", max_side=4096, quality=95)
