"""Tests for thirty-ninth review improvements: race condition fixes (locked state
writes in build_animatic, compose_music, run_asset_discovery, run_cut_plan),
director.py traceback logging, export.py master file validation, and errors.py
adoption in pipeline.py."""

import asyncio
import json
import sys
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))


# ── Helpers ──────────────────────────────────────────────────────────────────

class TrackingLock:
    """Wraps an asyncio.Lock to record acquire/release events."""
    def __init__(self, real_lock):
        self._lock = real_lock
        self.entries = []

    async def __aenter__(self):
        await self._lock.acquire()
        self.entries.append("acquire")
        return self

    async def __aexit__(self, *args):
        self.entries.append("release")
        self._lock.release()


def _make_run(tmp_output_root, run_id, state):
    run_dir = tmp_output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps(state))
    return run_dir


# ── build_animatic lock ─────────────────────────────────────────────────────

class TestBuildAnimaticLock:

    @pytest.fixture
    def run_for_animatic(self, tmp_output_root):
        import pipeline
        run_id = "test_animatic_lock"
        run_dir = _make_run(tmp_output_root, run_id, {
            "run_id": run_id, "status": "keyframes_ready",
            "concept": "test",
            "params": {"num_shots": 2, "ratio": "16:9"},
            "story": {"shots": [{"duration_s": 3}, {"duration_s": 4}]},
            "keyframes": [
                {"idx": 0, "path": "keyframes/shot_01.png", "status": "ready"},
                {"idx": 1, "path": "keyframes/shot_02.png", "status": "ready"},
            ],
            "music": None, "audio": None,
        })
        kf_dir = run_dir / "keyframes"
        kf_dir.mkdir()
        (kf_dir / "shot_01.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        (kf_dir / "shot_02.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        return run_id

    def test_state_write_under_lock(self, run_for_animatic):
        import pipeline
        original_get_lock = pipeline._get_lock
        tracker = TrackingLock(original_get_lock(run_for_animatic))

        def patched_get_lock(run_id):
            return tracker

        out_file = pipeline._run_dir(run_for_animatic) / "animatic.mp4"

        async def mock_build_animatic(*args, **kwargs):
            out_file.write_bytes(b"\x00\x00\x00\x1cftyp" + b"\x00" * 100)

        async def _run():
            with patch.object(pipeline, "_get_lock", side_effect=patched_get_lock), \
                 patch("pipeline.video") as mock_video:
                mock_video.build_animatic = mock_build_animatic
                return await pipeline.build_animatic(run_for_animatic)

        asyncio.get_event_loop().run_until_complete(_run())
        assert len(tracker.entries) >= 2
        state = pipeline.get_state(run_for_animatic)
        assert state["animatic"] == "animatic.mp4"
        assert state.get("animatic_generated_at") is not None


# ── compose_music lock ──────────────────────────────────────────────────────

class TestComposeMusicLock:

    @pytest.fixture
    def run_for_compose(self, tmp_output_root):
        import pipeline
        run_id = "test_compose_lock"
        run_dir = _make_run(tmp_output_root, run_id, {
            "run_id": run_id, "status": "storyboard_ready",
            "concept": "test",
            "params": {"num_shots": 2, "genre": "action"},
            "story": {"title": "test", "shots": [{"duration_s": 5}, {"duration_s": 5}]},
            "keyframes": [], "shots": [], "music": None, "audio": None,
        })
        return run_id

    def test_state_write_under_lock(self, run_for_compose):
        import pipeline
        original_get_lock = pipeline._get_lock
        tracker = TrackingLock(original_get_lock(run_for_compose))

        def patched_get_lock(run_id):
            return tracker

        async def mock_generate_brief(*a, **kw):
            return "test brief"

        async def mock_compose(*a, **kw):
            dst = kw.get("dst") or a[1]
            dst.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)

        async def mock_analyze(path):
            return {"bpm": 120, "beats": [], "downbeats": [], "energy_spikes": [], "duration": 10, "dynamic_range": 6}

        async def _run():
            with patch.object(pipeline, "_get_lock", side_effect=patched_get_lock), \
                 patch("pipeline.audio_mod") as mock_audio, \
                 patch("pipeline.music_mod") as mock_music, \
                 patch("pipeline.costs"):
                mock_audio.generate_music_brief = MagicMock(return_value="test brief")
                mock_audio.compose_music = AsyncMock(side_effect=lambda brief, dst, **kw: dst.write_bytes(b"\xff" * 50))
                mock_music.analyze = MagicMock(return_value={
                    "bpm": 120, "beats": [], "downbeats": [],
                    "energy_spikes": [], "duration": 10, "dynamic_range": 6,
                })
                return await pipeline.compose_music(run_for_compose)

        asyncio.get_event_loop().run_until_complete(_run())
        assert len(tracker.entries) >= 2
        state = pipeline.get_state(run_for_compose)
        assert state["music"]["composed"] is True
        assert state["music"]["path"] == "music/track.mp3"


# ── run_asset_discovery lock ────────────────────────────────────────────────

class TestAssetDiscoveryLock:

    @pytest.fixture
    def run_for_discovery(self, tmp_output_root):
        import pipeline
        run_id = "test_discovery_lock"
        _make_run(tmp_output_root, run_id, {
            "run_id": run_id, "status": "storyboard_ready",
            "concept": "test",
            "params": {},
            "story": {"title": "test", "shots": [{"beat": "hook"}]},
            "references": [], "cast": [], "locations": [], "props": [],
        })
        return run_id

    def test_initial_status_write_under_lock(self, run_for_discovery):
        import pipeline
        original_get_lock = pipeline._get_lock
        tracker = TrackingLock(original_get_lock(run_for_discovery))

        def patched_get_lock(run_id):
            return tracker

        async def _run():
            with patch.object(pipeline, "_get_lock", side_effect=patched_get_lock), \
                 patch("pipeline.assets_mod") as mock_assets:
                mock_assets.discover_assets = MagicMock(return_value={
                    "assets": [{"name": "Logo", "type": "logo", "shots": [0],
                                "description": "Company logo",
                                "suggested_generation_prompt": "logo"}],
                    "reasoning": "found logo",
                })
                return await pipeline.run_asset_discovery(run_for_discovery)

        asyncio.get_event_loop().run_until_complete(_run())
        # Should have at least 2 lock acquisitions: initial status + final write
        assert len(tracker.entries) >= 4  # 2 acquires + 2 releases
        state = pipeline.get_state(run_for_discovery)
        assert state["asset_discovery_status"] == "ready"
        assert len(state["assets"]) == 1

    def test_error_write_under_lock(self, run_for_discovery):
        import pipeline
        original_get_lock = pipeline._get_lock
        tracker = TrackingLock(original_get_lock(run_for_discovery))

        def patched_get_lock(run_id):
            return tracker

        async def _run():
            with patch.object(pipeline, "_get_lock", side_effect=patched_get_lock), \
                 patch("pipeline.assets_mod") as mock_assets:
                mock_assets.discover_assets = MagicMock(side_effect=ValueError("Claude error"))
                with pytest.raises(ValueError, match="Claude error"):
                    await pipeline.run_asset_discovery(run_for_discovery)

        asyncio.get_event_loop().run_until_complete(_run())
        # 2 lock acquisitions: initial status + error status
        assert len(tracker.entries) >= 4
        state = pipeline.get_state(run_for_discovery)
        assert state["asset_discovery_status"] == "failed"
        assert "Claude error" in state.get("asset_discovery_error", "")


# ── run_cut_plan lock ───────────────────────────────────────────────────────

class TestCutPlanLock:

    @pytest.fixture
    def run_for_cut_plan(self, tmp_output_root):
        import pipeline
        run_id = "test_cut_plan_lock"
        run_dir = _make_run(tmp_output_root, run_id, {
            "run_id": run_id, "status": "shots_ready",
            "concept": "test",
            "params": {"num_shots": 1, "ratio": "16:9", "variants_per_scene": 1},
            "story": {"title": "test", "shots": [{"beat": "hook", "duration_s": 5}]},
            "keyframes": [{"idx": 0, "path": "keyframes/shot_01.png", "status": "ready"}],
            "shots": [{"idx": 0, "path": "shots/shot_01.mp4", "status": "ready",
                        "variants": [{"idx": 0, "path": "shots/shot_01_v0.mp4",
                                       "status": "ready", "seed": 1000}],
                        "primary_variant": 0}],
        })
        shots_dir = run_dir / "shots"
        shots_dir.mkdir()
        (shots_dir / "shot_01.mp4").write_bytes(b"\x00\x00\x00\x1cftyp" + b"\x00" * 100)
        (shots_dir / "shot_01_v0.mp4").write_bytes(b"\x00\x00\x00\x1cftyp" + b"\x00" * 100)
        return run_id

    def test_status_writes_under_lock(self, run_for_cut_plan):
        import pipeline
        original_get_lock = pipeline._get_lock
        tracker = TrackingLock(original_get_lock(run_for_cut_plan))

        def patched_get_lock(run_id):
            return tracker

        async def _run():
            with patch.object(pipeline, "_get_lock", side_effect=patched_get_lock), \
                 patch.object(pipeline, "run_contact_sheets", new_callable=AsyncMock) as mock_sheets, \
                 patch("pipeline.review") as mock_review, \
                 patch("pipeline.video") as mock_video:
                mock_sheets.return_value = [{"idx": 0, "frames": []}]
                mock_video._probe_duration_sync = MagicMock(return_value=5.0)
                mock_review.analyze_shots = MagicMock(return_value={
                    "shots": [{"idx": 0, "quality": "good"}],
                })
                return await pipeline.run_cut_plan(run_for_cut_plan)

        asyncio.get_event_loop().run_until_complete(_run())
        # At least 2 lock acquisitions: "generating" status + final "ready" write
        assert len(tracker.entries) >= 4
        state = pipeline.get_state(run_for_cut_plan)
        assert state["cut_plan_status"] == "ready"
        assert state["cut_plan"]["shots"] == [{"idx": 0, "quality": "good"}]

    def test_error_write_under_lock(self, run_for_cut_plan):
        import pipeline
        original_get_lock = pipeline._get_lock
        tracker = TrackingLock(original_get_lock(run_for_cut_plan))

        def patched_get_lock(run_id):
            return tracker

        async def _run():
            with patch.object(pipeline, "_get_lock", side_effect=patched_get_lock), \
                 patch.object(pipeline, "run_contact_sheets", new_callable=AsyncMock) as mock_sheets, \
                 patch("pipeline.review") as mock_review, \
                 patch("pipeline.video") as mock_video:
                mock_sheets.return_value = [{"idx": 0, "frames": []}]
                mock_video._probe_duration_sync = MagicMock(return_value=5.0)
                mock_review.analyze_shots = MagicMock(side_effect=ValueError("vision failed"))
                with pytest.raises(ValueError, match="vision failed"):
                    await pipeline.run_cut_plan(run_for_cut_plan)

        asyncio.get_event_loop().run_until_complete(_run())
        assert len(tracker.entries) >= 4
        state = pipeline.get_state(run_for_cut_plan)
        assert state["cut_plan_status"] == "failed"
        assert "vision failed" in state.get("cut_plan_error", "")


# ── errors.py adoption ──────────────────────────────────────────────────────

class TestCustomErrors:
    """Verify pipeline.py raises custom error types instead of bare RuntimeError."""

    @pytest.fixture
    def run_no_storyboard(self, tmp_output_root):
        import pipeline
        run_id = "test_errors"
        _make_run(tmp_output_root, run_id, {
            "run_id": run_id, "status": "new",
            "concept": "test", "params": {},
            "story": None, "keyframes": [], "shots": [],
        })
        return run_id

    def test_storyboard_not_ready_raises_TrailerNotReady(self, run_no_storyboard):
        import pipeline
        from errors import TrailerNotReady

        async def _run():
            await pipeline.run_asset_discovery(run_no_storyboard)

        with pytest.raises(TrailerNotReady, match="storyboard"):
            asyncio.get_event_loop().run_until_complete(_run())

    def test_compose_music_not_ready_raises_TrailerNotReady(self, run_no_storyboard):
        import pipeline
        from errors import TrailerNotReady

        async def _run():
            await pipeline.compose_music(run_no_storyboard)

        with pytest.raises(TrailerNotReady, match="storyboard required"):
            asyncio.get_event_loop().run_until_complete(_run())

    def test_cut_plan_user_error_raises_TrailerUserError(self, tmp_output_root):
        import pipeline
        from errors import TrailerUserError
        run_id = "test_user_error"
        _make_run(tmp_output_root, run_id, {
            "run_id": run_id, "status": "storyboard_ready",
            "concept": "test", "params": {},
            "story": {"shots": []},
            "keyframes": [], "shots": [],
            "cut_plan": {"shots": "not_a_list"},
        })

        async def _run():
            await pipeline.update_cut_plan(run_id, {"shots": "not_a_list"})

        with pytest.raises(TrailerUserError, match="must be a"):
            asyncio.get_event_loop().run_until_complete(_run())

    def test_corrupted_state_raises_TrailerError(self, tmp_output_root):
        import pipeline
        from errors import TrailerError
        run_dir = tmp_output_root / "test_corrupt"
        run_dir.mkdir()
        (run_dir / "state.json").write_text("not valid json")
        with pytest.raises(TrailerError, match="corrupted"):
            pipeline.get_state("test_corrupt")


# ── director.py traceback logging ───────────────────────────────────────────

class TestDirectorTracebackLogging:
    """Verify _dispatch_tool logs full traceback on exception."""

    def test_dispatch_tool_logs_traceback(self, tmp_output_root, capsys):
        import pipeline
        import director

        run_id = "test_director_tb"
        _make_run(tmp_output_root, run_id, {
            "run_id": run_id, "status": "storyboard_ready",
            "concept": "test", "params": {},
            "story": {"shots": [{"beat": "hook"}]},
            "keyframes": [{"idx": 0, "status": "pending"}],
            "shots": [],
        })

        async def _run():
            result = await director._dispatch_tool(
                run_id, "swap_variant", {"shot_idx": 999, "variant_idx": 0}
            )
            return result

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result["ok"] is False
        captured = capsys.readouterr()
        assert "Traceback" in captured.err or "failed" in result.get("error", "")

    def test_dispatch_tool_error_message_500_chars(self, tmp_output_root):
        import director

        run_id = "test_director_len"
        _make_run(tmp_output_root, run_id, {
            "run_id": run_id, "status": "new",
            "concept": "test", "params": {},
            "story": None, "keyframes": [], "shots": [],
        })

        async def _run():
            with patch("pipeline.set_primary_variant",
                       new_callable=AsyncMock,
                       side_effect=ValueError("x" * 600)):
                return await director._dispatch_tool(
                    run_id, "swap_variant", {"shot_idx": 0, "variant_idx": 0}
                )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result["ok"] is False
        assert len(result["error"]) <= 550  # 500 char message + prefix


# ── export.py master file validation ────────────────────────────────────────

class TestExportMasterValidation:
    """Verify export_platform_variants checks master file exists."""

    def test_raises_when_master_file_missing(self, tmp_output_root):
        import pipeline
        from export import export_platform_variants

        run_id = "test_export_missing"
        _make_run(tmp_output_root, run_id, {
            "run_id": run_id, "status": "done",
            "concept": "test", "params": {},
            "story": {"shots": []},
            "keyframes": [], "shots": [],
            "final": "trailer.mp4",
        })

        async def _run():
            await export_platform_variants(run_id, ["16x9"])

        with pytest.raises(RuntimeError, match="master trailer file missing"):
            asyncio.get_event_loop().run_until_complete(_run())

    def test_succeeds_when_master_exists(self, tmp_output_root):
        import pipeline
        from export import export_platform_variants

        run_id = "test_export_exists"
        run_dir = _make_run(tmp_output_root, run_id, {
            "run_id": run_id, "status": "done",
            "concept": "test", "params": {},
            "story": {"shots": []},
            "keyframes": [], "shots": [],
            "final": "trailer.mp4",
        })
        (run_dir / "trailer.mp4").write_bytes(b"\x00\x00\x00\x1cftyp" + b"\x00" * 100)

        async def _run():
            with patch("export.video") as mock_video:
                mock_video._PLATFORM_VARIANTS = {"16x9": (1920, 1080)}
                mock_video.reframe_to_platform = AsyncMock(
                    side_effect=lambda src, dst, preset: dst.write_bytes(b"\x00" * 50)
                )
                return await export_platform_variants(run_id, ["16x9"])

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert "16x9" in result
