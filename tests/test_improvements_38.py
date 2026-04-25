"""Tests for thirty-eighth review improvements: generate_all status fix,
upload_asset lock fix, attach_music lock fix, generate-all UI wiring."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))


# ── generate_all_assets status filter ─────────────────────────────────────

class TestGenerateAllStatusFilter:

    @pytest.fixture
    def run_with_mixed_assets(self, tmp_output_root):
        import pipeline
        run_id = "test_gen_all_status"
        run_dir = tmp_output_root / run_id
        run_dir.mkdir()
        state = {
            "run_id": run_id,
            "status": "new",
            "story": {"title": "test"},
            "assets": [
                {"id": "a1", "name": "Logo", "status": "pending"},
                {"id": "a2", "name": "Shield", "status": "uploaded", "path": "assets/shield.png"},
                {"id": "a3", "name": "Map", "status": "generated", "path": "assets/map.png"},
                {"id": "a4", "name": "Sword", "status": "failed"},
                {"id": "a5", "name": "Crest", "status": "skipped"},
                {"id": "a6", "name": "Banner", "status": "generating"},
            ],
            "params": {},
        }
        (run_dir / "state.json").write_text(json.dumps(state))
        return run_id

    def test_picks_pending_and_failed_only(self, run_with_mixed_assets):
        import pipeline
        generated = []

        async def mock_generate(run_id, asset_id, *, prompt_override=None):
            generated.append(asset_id)
            return {"id": asset_id, "status": "generated"}

        async def _run():
            with patch.object(pipeline, "generate_asset", side_effect=mock_generate):
                return await pipeline.generate_all_assets(run_with_mixed_assets)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert set(generated) == {"a1", "a4"}
        assert len(result) == 2

    def test_skips_uploaded_generated_skipped_generating(self, run_with_mixed_assets):
        import pipeline
        generated = []

        async def mock_generate(run_id, asset_id, *, prompt_override=None):
            generated.append(asset_id)
            return {"id": asset_id, "status": "generated"}

        async def _run():
            with patch.object(pipeline, "generate_asset", side_effect=mock_generate):
                await pipeline.generate_all_assets(run_with_mixed_assets)

        asyncio.get_event_loop().run_until_complete(_run())
        assert "a2" not in generated
        assert "a3" not in generated
        assert "a5" not in generated
        assert "a6" not in generated


# ── upload_asset lock coverage ────────────────────────────────────────────

class TestUploadAssetLock:

    @pytest.fixture
    def run_with_asset(self, tmp_output_root):
        import pipeline
        run_id = "test_upload_lock"
        run_dir = tmp_output_root / run_id
        run_dir.mkdir()
        state = {
            "run_id": run_id,
            "status": "new",
            "story": {"title": "test"},
            "assets": [
                {"id": "a1", "name": "Logo", "status": "pending",
                 "path": None, "filename": None, "updated_at": None},
            ],
            "params": {},
        }
        (run_dir / "state.json").write_text(json.dumps(state))
        return run_id

    def test_upload_writes_state_under_lock(self, run_with_asset):
        import pipeline
        lock_was_held = []
        original_get_lock = pipeline._get_lock

        class TrackingLock:
            def __init__(self, lock):
                self._lock = lock

            async def __aenter__(self):
                await self._lock.__aenter__()
                lock_was_held.append(True)
                return self

            async def __aexit__(self, *args):
                return await self._lock.__aexit__(*args)

        def patched_get_lock(run_id):
            return TrackingLock(original_get_lock(run_id))

        async def _run():
            with patch.object(pipeline, "_get_lock", side_effect=patched_get_lock):
                return await pipeline.upload_asset(
                    run_with_asset, "a1",
                    filename="logo.png", data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
                )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result["status"] == "uploaded"
        assert len(lock_was_held) == 1

        state = pipeline.get_state(run_with_asset)
        slot = pipeline._asset_slot(state, "a1")
        assert slot["status"] == "uploaded"
        assert slot["path"].startswith("assets/")

    def test_upload_file_written_to_disk(self, run_with_asset, tmp_output_root):
        import pipeline
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

        async def _run():
            return await pipeline.upload_asset(
                run_with_asset, "a1", filename="logo.png", data=data,
            )

        asyncio.get_event_loop().run_until_complete(_run())
        dst = tmp_output_root / run_with_asset / "assets" / "a1.png"
        assert dst.exists()
        assert dst.read_bytes() == data


# ── attach_music lock coverage ────────────────────────────────────────────

class TestAttachMusicLock:

    @pytest.fixture
    def run_for_music(self, tmp_output_root):
        import pipeline
        run_id = "test_music_lock"
        run_dir = tmp_output_root / run_id
        run_dir.mkdir()
        state = {
            "run_id": run_id,
            "status": "new",
            "story": {"title": "test"},
            "params": {},
        }
        (run_dir / "state.json").write_text(json.dumps(state))
        return run_id

    def test_success_path_saves_under_lock(self, run_for_music):
        import pipeline
        lock_entries = []
        original_get_lock = pipeline._get_lock

        class TrackingLock:
            def __init__(self, lock):
                self._lock = lock

            async def __aenter__(self):
                await self._lock.__aenter__()
                lock_entries.append("entered")
                return self

            async def __aexit__(self, *args):
                return await self._lock.__aexit__(*args)

        def patched_get_lock(run_id):
            return TrackingLock(original_get_lock(run_id))

        fake_analysis = {
            "bpm": 120, "beats": [0.5, 1.0], "energy_spikes": [2.0],
            "dynamic_range": 8.5,
        }

        async def _run():
            with patch.object(pipeline, "_get_lock", side_effect=patched_get_lock), \
                 patch("pipeline.music_mod") as mock_music:
                mock_music.analyze.return_value = fake_analysis
                return await pipeline.attach_music(
                    run_for_music, filename="track.mp3", data=b"\xff" * 100,
                )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result["analysis"]["bpm"] == 120
        assert len(lock_entries) == 1

        state = pipeline.get_state(run_for_music)
        assert state["music"]["analysis"]["bpm"] == 120

    def test_failure_path_saves_under_lock(self, run_for_music):
        import pipeline
        lock_entries = []
        original_get_lock = pipeline._get_lock

        class TrackingLock:
            def __init__(self, lock):
                self._lock = lock

            async def __aenter__(self):
                await self._lock.__aenter__()
                lock_entries.append("entered")
                return self

            async def __aexit__(self, *args):
                return await self._lock.__aexit__(*args)

        def patched_get_lock(run_id):
            return TrackingLock(original_get_lock(run_id))

        async def _run():
            with patch.object(pipeline, "_get_lock", side_effect=patched_get_lock), \
                 patch("pipeline.music_mod") as mock_music:
                mock_music.analyze.side_effect = RuntimeError("analysis boom")
                return await pipeline.attach_music(
                    run_for_music, filename="track.mp3", data=b"\xff" * 100,
                )

        with pytest.raises(RuntimeError, match="analysis boom"):
            asyncio.get_event_loop().run_until_complete(_run())

        assert len(lock_entries) == 1
        state = pipeline.get_state(run_for_music)
        assert state["music"]["error"] == "analysis failed: analysis boom"
        assert state["music"]["analysis"] is None
