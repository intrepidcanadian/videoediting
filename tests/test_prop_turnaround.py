"""Tests for prop turnaround generation and asset improvements."""

import pytest

# ── Turnaround prop angles ────────────────────────────────────────────

def test_turnaround_accepts_props_kind():
    import turnaround as t
    assert "props" in ("characters", "locations", "props")
    angles = t._PROP_ANGLES
    assert len(angles) == 5
    slugs = [a[0] for a in angles]
    assert "hero" in slugs
    assert "in_context" in slugs


def test_turnaround_build_prompt_props():
    import turnaround as t
    seed = t._build_prompt("A brass compass", "Hero shot", is_seed=True, kind="props")
    assert "A brass compass" in seed
    assert t._PROP_STYLE_BLOCK in seed

    ref = t._build_prompt("A brass compass", "Top-down view", is_seed=False, kind="props")
    assert "Same object as reference image 1" in ref


def test_turnaround_rejects_invalid_kind():
    import asyncio
    import turnaround as t
    with pytest.raises(ValueError, match="props"):
        asyncio.get_event_loop().run_until_complete(
            t.generate_turnaround(name="test", description="test", kind="weapons")
        )


def test_turnaround_bump_meta_props(tmp_library_root):
    import turnaround as t
    import library as lib
    lib.save_item("props", name="Compass", description="Brass compass", tags=[])
    slug = "compass"
    t._bump_turnaround_meta(
        slug, "Compass", "Brass compass",
        generated_count=3, failed_angles=["close_detail"],
        status="generating", kind="props",
    )
    meta = lib.get_item("props", slug)
    assert meta["turnaround"]["planned_count"] == 5
    assert meta["turnaround"]["generated_count"] == 3
    assert "close_detail" in meta["turnaround"]["failed_angles"]
    assert "hero" in meta["turnaround"]["angles"]


# ── Asset re-discovery guard (409) ────────────────────────────────────

fastapi_testclient = pytest.importorskip("fastapi.testclient", reason="fastapi not installed")


@pytest.fixture
def client(tmp_output_root):
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app) as c:
        yield c


def _create_run_with_assets(tmp_output_root, assets=None):
    """Helper: create a minimal run with an existing storyboard and optional assets."""
    import json
    run_id = "test_guard_run"
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


def test_discover_assets_409_when_generating(client, tmp_output_root):
    run_id = _create_run_with_assets(tmp_output_root, assets=[
        {"id": "asset_01", "name": "Logo", "type": "logo", "status": "generating", "path": None},
    ])
    resp = client.post(f"/api/runs/{run_id}/assets/discover")
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["detail"]


def test_discover_assets_409_when_uploaded(client, tmp_output_root):
    run_id = _create_run_with_assets(tmp_output_root, assets=[
        {"id": "asset_01", "name": "Logo", "type": "logo", "status": "uploaded", "path": "assets/logo.png"},
    ])
    resp = client.post(f"/api/runs/{run_id}/assets/discover")
    assert resp.status_code == 409


def test_discover_assets_force_bypasses_409(client, tmp_output_root):
    run_id = _create_run_with_assets(tmp_output_root, assets=[
        {"id": "asset_01", "name": "Logo", "type": "logo", "status": "uploaded", "path": "assets/logo.png"},
    ])
    resp = client.post(f"/api/runs/{run_id}/assets/discover?force=true")
    assert resp.status_code == 200


def test_discover_assets_ok_when_all_pending(client, tmp_output_root):
    run_id = _create_run_with_assets(tmp_output_root, assets=[
        {"id": "asset_01", "name": "Logo", "type": "logo", "status": "pending", "path": None},
    ])
    resp = client.post(f"/api/runs/{run_id}/assets/discover")
    assert resp.status_code == 200


def test_discover_assets_ok_when_no_assets(client, tmp_output_root):
    run_id = _create_run_with_assets(tmp_output_root, assets=[])
    resp = client.post(f"/api/runs/{run_id}/assets/discover")
    assert resp.status_code == 200


# ── Asset promote endpoint ────────────────────────────────────────────

def test_promote_asset_rejects_pending(client, tmp_output_root):
    run_id = _create_run_with_assets(tmp_output_root, assets=[
        {"id": "asset_01", "name": "Logo", "type": "logo", "status": "pending", "path": None},
    ])
    resp = client.post(
        f"/api/runs/{run_id}/assets/asset_01/promote",
        data={"name": "My Logo"},
    )
    assert resp.status_code == 400
    assert "uploaded or generated" in resp.json()["detail"]


def test_promote_asset_not_found(client, tmp_output_root):
    run_id = _create_run_with_assets(tmp_output_root, assets=[])
    resp = client.post(
        f"/api/runs/{run_id}/assets/asset_99/promote",
        data={"name": "Ghost"},
    )
    assert resp.status_code == 404


def test_promote_asset_maps_type_to_kind(client, tmp_output_root, tmp_library_root):
    import json
    run_id = _create_run_with_assets(tmp_output_root, assets=[
        {"id": "asset_01", "name": "Brass Compass", "type": "prop",
         "description": "A vintage brass compass",
         "status": "generated", "path": "assets/asset_01.png"},
    ])
    run_dir = tmp_output_root / run_id
    assets_dir = run_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    (assets_dir / "asset_01.png").write_bytes(b"\x89PNG\r\n\x1a\nfakedata")
    resp = client.post(
        f"/api/runs/{run_id}/assets/asset_01/promote",
        data={"name": "Brass Compass", "description": "A vintage compass"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "props"
    assert body["ok"] is True


def test_promote_asset_character_maps_to_characters(client, tmp_output_root, tmp_library_root):
    import json
    run_id = _create_run_with_assets(tmp_output_root, assets=[
        {"id": "asset_02", "name": "Elena", "type": "character",
         "description": "Main character",
         "status": "uploaded", "path": "assets/asset_02.png"},
    ])
    run_dir = tmp_output_root / run_id
    assets_dir = run_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    (assets_dir / "asset_02.png").write_bytes(b"\x89PNG\r\n\x1a\nfakedata")
    resp = client.post(
        f"/api/runs/{run_id}/assets/asset_02/promote",
        data={"name": "Elena"},
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "characters"
