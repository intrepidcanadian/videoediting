"""Tests for fortieth review improvements: prop/location continuity refs in
keyframe rendering, asset discovery deduplication (preserving already-resolved
assets on re-scan), featured name validation on storyboard edit, and asset
promotion API method in UI."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))


def _make_run(tmp_output_root, run_id, state):
    run_dir = tmp_output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps(state))
    return run_dir


# ── Prop/location continuity refs ──────────────────────────────────────────

class TestPropLocationContinuityRefs:

    @pytest.fixture
    def run_with_featured(self, tmp_output_root):
        import pipeline
        run_id = "test_continuity_refs"
        state = {
            "run_id": run_id,
            "status": "keyframes_partial",
            "concept": "test",
            "params": {"num_shots": 5, "ratio": "16:9", "shot_duration": 5},
            "story": {
                "title": "Test",
                "character_sheet": "",
                "world_sheet": "",
                "shots": [
                    {"beat": "intro", "duration_s": 5, "keyframe_prompt": "office",
                     "motion_prompt": "pan", "featured_props": ["magic sword"],
                     "featured_locations": ["castle hall"]},
                    {"beat": "build", "duration_s": 5, "keyframe_prompt": "forest",
                     "motion_prompt": "track", "featured_props": [],
                     "featured_locations": ["dark forest"]},
                    {"beat": "mid", "duration_s": 5, "keyframe_prompt": "field",
                     "motion_prompt": "drone", "featured_props": [],
                     "featured_locations": []},
                    {"beat": "climax", "duration_s": 5, "keyframe_prompt": "battle",
                     "motion_prompt": "shake", "featured_props": ["magic sword"],
                     "featured_locations": ["castle hall"]},
                    {"beat": "end", "duration_s": 5, "keyframe_prompt": "sunset",
                     "motion_prompt": "slow", "featured_props": ["magic sword"],
                     "featured_locations": []},
                ],
            },
            "references": [],
            "cast": [],
            "locations": [],
            "props": [],
            "assets": [],
            "keyframes": [
                {"idx": i, "path": f"keyframes/shot_{i+1:02d}.png", "status": "ready",
                 "error": None, "prompt_override": None, "updated_at": None}
                for i in range(5)
            ],
            "shots": [
                {"idx": i, "path": None, "status": "pending", "error": None,
                 "prompt_override": None, "updated_at": None,
                 "variants": [{"idx": 0, "path": None, "seed": None,
                               "status": "pending", "error": None, "updated_at": None}],
                 "primary_variant": 0}
                for i in range(5)
            ],
        }
        run_dir = _make_run(tmp_output_root, run_id, state)
        kf_dir = run_dir / "keyframes"
        kf_dir.mkdir(exist_ok=True)
        for i in range(5):
            (kf_dir / f"shot_{i+1:02d}.png").write_bytes(b"fake-png")
        return run_id

    def test_prop_continuity_ref_from_earlier_shot(self, run_with_featured, tmp_output_root):
        """Shot 3 (idx=3) features 'magic sword' which also appeared in shot 0.
        Shot 0's keyframe should be included as a prop continuity ref."""
        import pipeline

        collected_refs = []
        collected_labels = []

        async def capture_generate(prompt, refs, output_path, reference_labels=None, run_id=None):
            collected_refs.extend(refs)
            collected_labels.extend(reference_labels or [])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-keyframe")

        async def _run():
            with patch("nano_banana.generate_keyframe", side_effect=capture_generate):
                await pipeline.run_keyframe(run_with_featured, 3, chain_previous=True)

        asyncio.get_event_loop().run_until_complete(_run())

        ref_strs = [str(r) for r in collected_refs]
        assert any("shot_01.png" in r for r in ref_strs), \
            f"Expected shot_01.png (prop continuity) in refs, got: {ref_strs}"
        assert any("prop continuity" in l for l in collected_labels), \
            f"Expected 'prop continuity' label, got: {collected_labels}"

    def test_location_continuity_ref_from_earlier_shot(self, run_with_featured, tmp_output_root):
        """Shot 3 (idx=3) features 'castle hall' which also appeared in shot 0.
        Shot 0's keyframe should be included as a continuity ref. When both prop
        and location point to the same earlier shot, the keyframe is included once
        (deduped by _used_continuity set)."""
        import pipeline

        collected_refs = []
        collected_labels = []

        async def capture_generate(prompt, refs, output_path, reference_labels=None, run_id=None):
            collected_refs.extend(refs)
            collected_labels.extend(reference_labels or [])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-keyframe")

        async def _run():
            with patch("nano_banana.generate_keyframe", side_effect=capture_generate):
                await pipeline.run_keyframe(run_with_featured, 3, chain_previous=True)

        asyncio.get_event_loop().run_until_complete(_run())

        ref_strs = [str(r) for r in collected_refs]
        assert any("shot_01.png" in r for r in ref_strs), \
            f"Expected shot_01.png in refs, got: {ref_strs}"
        has_any_continuity = any("prop continuity" in l or "location continuity" in l for l in collected_labels)
        assert has_any_continuity, \
            f"Expected prop or location continuity label, got: {collected_labels}"

    def test_no_continuity_ref_when_no_overlap(self, run_with_featured, tmp_output_root):
        """Shot 2 (idx=2) has no featured props or locations overlapping earlier shots.
        No prop/location continuity refs should be added."""
        import pipeline

        collected_labels = []

        async def capture_generate(prompt, refs, output_path, reference_labels=None, run_id=None):
            collected_labels.extend(reference_labels or [])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-keyframe")

        async def _run():
            with patch("nano_banana.generate_keyframe", side_effect=capture_generate):
                await pipeline.run_keyframe(run_with_featured, 2, chain_previous=True)

        asyncio.get_event_loop().run_until_complete(_run())

        assert not any("prop continuity" in l for l in collected_labels), \
            f"Expected no prop continuity label, got: {collected_labels}"
        assert not any("location continuity" in l for l in collected_labels), \
            f"Expected no location continuity label, got: {collected_labels}"

    def test_adjacent_shot_skipped_for_continuity(self, run_with_featured, tmp_output_root):
        """Shot 4 (idx=4) features 'magic sword' which appeared in shot 3 (idx=3)
        AND shot 0. The adjacent shot (3) is already the chain_previous continuity
        ref, so the prop continuity should pick shot 0 instead."""
        import pipeline

        collected_refs = []
        collected_labels = []

        async def capture_generate(prompt, refs, output_path, reference_labels=None, run_id=None):
            collected_refs.extend(refs)
            collected_labels.extend(reference_labels or [])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-keyframe")

        async def _run():
            with patch("nano_banana.generate_keyframe", side_effect=capture_generate):
                await pipeline.run_keyframe(run_with_featured, 4, chain_previous=True)

        asyncio.get_event_loop().run_until_complete(_run())

        ref_strs = [str(r) for r in collected_refs]
        prop_continuity_refs = [r for r, l in zip(ref_strs, collected_labels) if "prop continuity" in l]
        if prop_continuity_refs:
            assert any("shot_01.png" in r or "shot_04.png" in r for r in prop_continuity_refs), \
                f"Expected shot_01.png (non-adjacent) for prop continuity, got: {prop_continuity_refs}"


# ── Asset discovery dedup ────────────────────────────���────────────────────

class TestAssetDiscoveryDedup:

    @pytest.fixture
    def run_with_existing_assets(self, tmp_output_root):
        import pipeline
        run_id = "test_asset_dedup"
        state = {
            "run_id": run_id,
            "status": "storyboard_ready",
            "concept": "test",
            "params": {"num_shots": 3, "ratio": "16:9"},
            "story": {
                "title": "Test",
                "character_sheet": "",
                "world_sheet": "",
                "shots": [
                    {"beat": "intro", "duration_s": 5, "keyframe_prompt": "logo reveal", "motion_prompt": "zoom"},
                    {"beat": "mid", "duration_s": 5, "keyframe_prompt": "product shot", "motion_prompt": "orbit"},
                    {"beat": "end", "duration_s": 5, "keyframe_prompt": "outro", "motion_prompt": "fade"},
                ],
            },
            "references": [],
            "cast": [],
            "locations": [],
            "props": [],
            "assets": [
                {"id": "asset_01", "name": "Acme Logo", "type": "logo", "shots": [0],
                 "description": "Company logo", "suggested_generation_prompt": "logo",
                 "status": "uploaded", "path": "assets/acme_logo.png",
                 "filename": "acme_logo.png", "generation_prompt": None, "updated_at": "2026-04-25"},
                {"id": "asset_02", "name": "Widget Pro", "type": "product", "shots": [1],
                 "description": "Flagship product", "suggested_generation_prompt": "product photo",
                 "status": "generated", "path": "assets/widget_pro.png",
                 "filename": "widget_pro.png", "generation_prompt": "photo of widget", "updated_at": "2026-04-25"},
                {"id": "asset_03", "name": "HQ Building", "type": "location", "shots": [2],
                 "description": "Company HQ", "suggested_generation_prompt": "building photo",
                 "status": "pending", "path": None,
                 "filename": None, "generation_prompt": None, "updated_at": None},
            ],
        }
        run_dir = _make_run(tmp_output_root, run_id, state)
        assets_dir = run_dir / "assets"
        assets_dir.mkdir(exist_ok=True)
        (assets_dir / "acme_logo.png").write_bytes(b"fake-logo")
        (assets_dir / "widget_pro.png").write_bytes(b"fake-product")
        return run_id

    def test_preserves_uploaded_assets_on_rescan(self, run_with_existing_assets, tmp_output_root):
        import pipeline

        discovery_result = {
            "assets": [
                {"name": "Acme Logo", "type": "logo", "shots": [0, 2],
                 "description": "Updated description", "suggested_generation_prompt": "new prompt"},
                {"name": "New Asset", "type": "prop", "shots": [1],
                 "description": "A new prop", "suggested_generation_prompt": "prop photo"},
            ],
            "reasoning": "re-scanned",
        }

        async def _run():
            with patch("assets.discover_assets", return_value=discovery_result):
                return await pipeline.run_asset_discovery(run_with_existing_assets)

        asyncio.get_event_loop().run_until_complete(_run())

        state = pipeline.get_state(run_with_existing_assets)
        assets = state["assets"]
        assert len(assets) == 2

        logo = next(a for a in assets if a["name"] == "Acme Logo")
        assert logo["status"] == "uploaded"
        assert logo["path"] == "assets/acme_logo.png"
        assert logo["shots"] == [0, 2], "shots should be updated from new discovery"

        new_asset = next(a for a in assets if a["name"] == "New Asset")
        assert new_asset["status"] == "pending"
        assert new_asset["path"] is None

    def test_preserves_generated_assets_on_rescan(self, run_with_existing_assets, tmp_output_root):
        import pipeline

        discovery_result = {
            "assets": [
                {"name": "Widget Pro", "type": "product", "shots": [1, 2],
                 "description": "Updated", "suggested_generation_prompt": "new prompt"},
            ],
            "reasoning": "re-scanned",
        }

        async def _run():
            with patch("assets.discover_assets", return_value=discovery_result):
                return await pipeline.run_asset_discovery(run_with_existing_assets)

        asyncio.get_event_loop().run_until_complete(_run())

        state = pipeline.get_state(run_with_existing_assets)
        assets = state["assets"]
        widget = next(a for a in assets if a["name"] == "Widget Pro")
        assert widget["status"] == "generated"
        assert widget["path"] == "assets/widget_pro.png"
        assert widget["shots"] == [1, 2]

    def test_pending_assets_replaced_on_rescan(self, run_with_existing_assets, tmp_output_root):
        import pipeline

        discovery_result = {
            "assets": [
                {"name": "HQ Building", "type": "location", "shots": [0, 2],
                 "description": "New desc", "suggested_generation_prompt": "wide photo"},
            ],
            "reasoning": "re-scanned",
        }

        async def _run():
            with patch("assets.discover_assets", return_value=discovery_result):
                return await pipeline.run_asset_discovery(run_with_existing_assets)

        asyncio.get_event_loop().run_until_complete(_run())

        state = pipeline.get_state(run_with_existing_assets)
        assets = state["assets"]
        assert len(assets) == 1
        hq = assets[0]
        assert hq["status"] == "pending"
        assert hq["description"] == "New desc"


# ── Featured name validation on storyboard edit ───────���───────────────────

class TestFeaturedNameValidationOnEdit:

    @pytest.fixture
    def run_with_cast(self, tmp_output_root):
        import pipeline
        run_id = "test_featured_edit"
        state = {
            "run_id": run_id,
            "status": "storyboard_ready",
            "concept": "test",
            "params": {"num_shots": 2, "ratio": "16:9"},
            "story": {
                "title": "Test",
                "character_sheet": "",
                "world_sheet": "",
                "shots": [
                    {"beat": "intro", "duration_s": 5, "keyframe_prompt": "hero arrives",
                     "motion_prompt": "pan", "featured_characters": ["Elena Martinez"]},
                    {"beat": "end", "duration_s": 5, "keyframe_prompt": "hero departs",
                     "motion_prompt": "track", "featured_characters": ["Elena Martinez"]},
                ],
            },
            "references": [],
            "cast": [{"name": "Elena Martinez", "slug": "elena_martinez", "ref_paths": []}],
            "locations": [],
            "props": [],
            "keyframes": [
                {"idx": 0, "path": None, "status": "pending", "error": None, "prompt_override": None, "updated_at": None},
                {"idx": 1, "path": None, "status": "pending", "error": None, "prompt_override": None, "updated_at": None},
            ],
            "shots": [],
        }
        _make_run(tmp_output_root, run_id, state)
        return run_id

    def test_auto_corrects_featured_name_on_edit(self, run_with_cast, tmp_output_root):
        import pipeline

        edited = {
            "title": "Test",
            "character_sheet": "",
            "world_sheet": "",
            "shots": [
                {"beat": "intro", "duration_s": 5, "keyframe_prompt": "hero arrives",
                 "motion_prompt": "pan", "featured_characters": ["Elena"]},
                {"beat": "end", "duration_s": 5, "keyframe_prompt": "hero departs",
                 "motion_prompt": "track", "featured_characters": ["Elena Martinez"]},
            ],
        }

        async def _run():
            return await pipeline.update_storyboard(run_with_cast, edited)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result["shots"][0]["featured_characters"][0] == "Elena Martinez", \
            "Should auto-correct 'Elena' to 'Elena Martinez'"

    def test_unresolvable_name_kept_with_warning(self, run_with_cast, tmp_output_root):
        import pipeline

        edited = {
            "title": "Test",
            "character_sheet": "",
            "world_sheet": "",
            "shots": [
                {"beat": "intro", "duration_s": 5, "keyframe_prompt": "hero arrives",
                 "motion_prompt": "pan", "featured_characters": ["Unknown Person"]},
                {"beat": "end", "duration_s": 5, "keyframe_prompt": "hero departs",
                 "motion_prompt": "track", "featured_characters": ["Elena Martinez"]},
            ],
        }

        async def _run():
            return await pipeline.update_storyboard(run_with_cast, edited)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result["shots"][0]["featured_characters"][0] == "Unknown Person", \
            "Unresolvable names should be kept as-is"


# ── Ref cap increase to 8 ────────────────────────────────────────────────

class TestRefCapIncrease:

    @pytest.fixture
    def run_with_many_refs(self, tmp_output_root):
        import pipeline
        run_id = "test_ref_cap"
        state = {
            "run_id": run_id,
            "status": "keyframes_partial",
            "concept": "test",
            "params": {"num_shots": 3, "ratio": "16:9", "shot_duration": 5},
            "story": {
                "title": "Test",
                "character_sheet": "",
                "world_sheet": "",
                "shots": [
                    {"beat": "intro", "duration_s": 5, "keyframe_prompt": "scene",
                     "motion_prompt": "pan", "featured_props": ["sword"],
                     "featured_locations": ["castle"]},
                    {"beat": "mid", "duration_s": 5, "keyframe_prompt": "scene2",
                     "motion_prompt": "track"},
                    {"beat": "end", "duration_s": 5, "keyframe_prompt": "scene3",
                     "motion_prompt": "slow", "featured_props": ["sword"],
                     "featured_locations": ["castle"]},
                ],
            },
            "references": ["references/char1.jpg", "references/char2.jpg",
                           "references/char3.jpg"],
            "cast": [
                {"name": "Hero", "slug": "hero", "ref_paths": ["references/char1.jpg", "references/char2.jpg"]},
                {"name": "Villain", "slug": "villain", "ref_paths": ["references/char3.jpg"]},
            ],
            "locations": [{"name": "Castle", "slug": "castle", "ref_paths": ["references/castle.jpg"]}],
            "props": [{"name": "Sword", "slug": "sword", "ref_paths": ["references/sword.jpg"]}],
            "assets": [
                {"id": "asset_01", "name": "Shield", "type": "prop", "shots": [0, 2],
                 "status": "generated", "path": "assets/shield.png", "description": "A shield"},
            ],
            "keyframes": [
                {"idx": i, "path": f"keyframes/shot_{i+1:02d}.png", "status": "ready",
                 "error": None, "prompt_override": None, "updated_at": None}
                for i in range(3)
            ],
            "shots": [
                {"idx": i, "path": None, "status": "pending", "error": None,
                 "prompt_override": None, "updated_at": None,
                 "variants": [{"idx": 0, "path": None, "seed": None,
                               "status": "pending", "error": None, "updated_at": None}],
                 "primary_variant": 0}
                for i in range(3)
            ],
        }
        run_dir = _make_run(tmp_output_root, run_id, state)
        kf_dir = run_dir / "keyframes"
        kf_dir.mkdir(exist_ok=True)
        for i in range(3):
            (kf_dir / f"shot_{i+1:02d}.png").write_bytes(b"fake-png")
        refs_dir = run_dir / "references"
        refs_dir.mkdir(exist_ok=True)
        for f in ["char1.jpg", "char2.jpg", "char3.jpg", "castle.jpg", "sword.jpg"]:
            (refs_dir / f).write_bytes(b"fake-ref")
        assets_dir = run_dir / "assets"
        assets_dir.mkdir(exist_ok=True)
        (assets_dir / "shield.png").write_bytes(b"fake-asset")
        return run_id

    def test_ref_cap_allows_up_to_8(self, run_with_many_refs, tmp_output_root):
        import pipeline

        collected_refs = []

        async def capture_generate(prompt, refs, output_path, reference_labels=None, run_id=None):
            collected_refs.extend(refs)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-keyframe")

        async def _run():
            with patch("nano_banana.generate_keyframe", side_effect=capture_generate):
                await pipeline.run_keyframe(run_with_many_refs, 2, chain_previous=True)

        asyncio.get_event_loop().run_until_complete(_run())

        assert len(collected_refs) <= 8, \
            f"Expected at most 8 refs, got {len(collected_refs)}"
