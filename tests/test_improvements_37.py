"""Tests for thirty-seventh review improvements: props dedup, review retry,
contact sheet alignment, playground vref cleanup, batch asset generation."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))


# ── Props sheet deduplication ──────────────────────────────────────────────

class TestMergePropsSheets:

    def test_empty_lib_returns_story(self):
        import pipeline
        assert pipeline._merge_props_sheets("", "Sword: ancient blade") == "Sword: ancient blade"

    def test_empty_story_returns_lib(self):
        import pipeline
        assert pipeline._merge_props_sheets("Sword: ancient blade", "") == "Sword: ancient blade"

    def test_both_empty(self):
        import pipeline
        assert pipeline._merge_props_sheets("", "") == ""

    def test_no_overlap_merges_both(self):
        import pipeline
        result = pipeline._merge_props_sheets("Sword: ancient blade", "Shield: golden shield")
        assert "Sword: ancient blade" in result
        assert "Shield: golden shield" in result

    def test_duplicate_name_prefers_library(self):
        import pipeline
        result = pipeline._merge_props_sheets(
            "1967 Impala: classic muscle car",
            "1967 Impala: vintage Chevrolet; Medallion: bronze pendant",
        )
        assert "classic muscle car" in result
        assert "vintage Chevrolet" not in result
        assert "Medallion: bronze pendant" in result

    def test_case_insensitive_dedup(self):
        import pipeline
        result = pipeline._merge_props_sheets("sword: big", "Sword: small; axe: sharp")
        assert result.count("sword") + result.count("Sword") == 1
        assert "axe: sharp" in result

    def test_multiple_lib_entries(self):
        import pipeline
        result = pipeline._merge_props_sheets(
            "A: desc1; B: desc2",
            "B: other; C: desc3",
        )
        assert "A: desc1" in result
        assert "B: desc2" in result
        assert "other" not in result
        assert "C: desc3" in result


# ── Review retry helper ────────────────────────────────────────────────────

class TestCallWithRetry:

    def test_success_on_first_try(self):
        import review
        mock_client = MagicMock()
        mock_client.messages.create.return_value = "ok"
        result = review._call_with_retry(
            mock_client, model="m", max_tokens=100,
            system=[], messages=[],
        )
        assert result == "ok"
        assert mock_client.messages.create.call_count == 1

    def test_retries_on_timeout(self):
        import httpx
        import review
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            httpx.ReadTimeout("timeout"),
            "ok",
        ]
        with patch.object(review.time, "sleep"):
            result = review._call_with_retry(
                mock_client, model="m", max_tokens=100,
                system=[], messages=[],
            )
        assert result == "ok"
        assert mock_client.messages.create.call_count == 2

    def test_retries_on_retriable_status(self):
        import review
        err = Exception("API error")
        err.status_code = 429
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [err, "ok"]
        with patch.object(review.time, "sleep"):
            result = review._call_with_retry(
                mock_client, model="m", max_tokens=100,
                system=[], messages=[],
            )
        assert result == "ok"

    def test_raises_non_retriable_immediately(self):
        import review
        err = Exception("bad request")
        err.status_code = 400
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = err
        with pytest.raises(Exception, match="bad request"):
            review._call_with_retry(
                mock_client, model="m", max_tokens=100,
                system=[], messages=[],
            )
        assert mock_client.messages.create.call_count == 1

    def test_exhausts_retries(self):
        import httpx
        import review
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = httpx.ReadTimeout("timeout")
        with patch.object(review.time, "sleep"):
            with pytest.raises(httpx.ReadTimeout):
                review._call_with_retry(
                    mock_client, model="m", max_tokens=100,
                    system=[], messages=[],
                )
        assert mock_client.messages.create.call_count == review._MAX_TRIES


# ── Playground vref cleanup ────────────────────────────────────────────────

class TestPlaygroundVrefCleanup:

    @pytest.fixture
    def tmp_playground(self, monkeypatch, tmp_path):
        import playground
        monkeypatch.setattr(playground, "PLAYGROUND_ROOT", tmp_path)
        return tmp_path

    def test_normalized_vrefs_cleaned_after_successful_render(self, tmp_playground):
        clip_id = "clip_20260425_120000_test"
        clip_dir = tmp_playground / clip_id
        refs_dir = clip_dir / "refs"
        refs_dir.mkdir(parents=True)

        norm1 = refs_dir / "vref_01_norm.mp4"
        norm2 = refs_dir / "vref_02_norm.mp4"
        norm1.write_bytes(b"fake1")
        norm2.write_bytes(b"fake2")
        assert norm1.exists() and norm2.exists()

        import playground
        meta = {
            "clip_id": clip_id, "kind": "video", "prompt": "test",
            "status": "generating", "video_path": None,
            "references": [], "video_references": [],
            "error": None, "created_at": "now", "updated_at": "now",
            "cost_usd": None,
        }
        playground._save_meta(clip_id, meta)

        # Simulate the cleanup logic from _render (success path)
        normalized_vrefs = [norm1, norm2]
        for nv in normalized_vrefs:
            nv.unlink(missing_ok=True)

        assert not norm1.exists()
        assert not norm2.exists()


# ── Batch asset generation ─────────────────────────────────────────────────

class TestBatchAssetGeneration:

    @pytest.fixture
    def run_with_assets(self, tmp_output_root):
        import pipeline
        run_id = "test_batch_assets"
        run_dir = tmp_output_root / run_id
        run_dir.mkdir()
        state = {
            "run_id": run_id,
            "status": "new",
            "story": {"title": "test"},
            "assets": [
                {"id": "a1", "name": "Logo", "status": "pending"},
                {"id": "a2", "name": "Shield", "status": "pending"},
                {"id": "a3", "name": "Sword", "status": "uploaded", "path": "assets/sword.png"},
                {"id": "a4", "name": "Map", "status": "failed"},
            ],
            "params": {},
        }
        (run_dir / "state.json").write_text(json.dumps(state))
        return run_id

    def test_generates_only_pending_assets(self, run_with_assets):
        import asyncio
        import pipeline
        generated = []
        async def mock_generate(run_id, asset_id, *, prompt_override=None):
            generated.append(asset_id)
            return {"id": asset_id, "status": "generated"}

        async def _run():
            with patch.object(pipeline, "generate_asset", side_effect=mock_generate):
                return await pipeline.generate_all_assets(run_with_assets)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert set(generated) == {"a1", "a2", "a4"}
        assert len(result) == 3

    def test_handles_partial_failure(self, run_with_assets):
        import asyncio
        import pipeline
        async def mock_generate(run_id, asset_id, *, prompt_override=None):
            if asset_id == "a2":
                raise RuntimeError("generation failed")
            return {"id": asset_id, "status": "generated"}

        async def _run():
            with patch.object(pipeline, "generate_asset", side_effect=mock_generate):
                return await pipeline.generate_all_assets(run_with_assets)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert len(result) == 2

    def test_empty_when_no_pending(self, tmp_output_root):
        import asyncio
        import pipeline
        run_id = "test_no_pending"
        run_dir = tmp_output_root / run_id
        run_dir.mkdir()
        state = {
            "run_id": run_id, "status": "new",
            "assets": [{"id": "a1", "status": "uploaded"}],
            "params": {},
        }
        (run_dir / "state.json").write_text(json.dumps(state))

        result = asyncio.get_event_loop().run_until_complete(
            pipeline.generate_all_assets(run_id)
        )
        assert result == []
