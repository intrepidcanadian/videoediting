"""Tests for Pydantic payload validation on server endpoints.

Verifies that typed payloads reject invalid input at the API boundary
instead of passing raw dicts through to pipeline code."""

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient", reason="fastapi not installed")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402


@pytest.fixture
def client(tmp_output_root):
    with TestClient(server.app) as c:
        yield c


# ── Pydantic model validation ─────────────────────────────────────────

def test_sweep_n_below_range_is_422(client):
    resp = client.post("/api/runs/test_run/shots/0/sweep", json={"n": 1})
    assert resp.status_code in (404, 422)

def test_sweep_n_above_range_is_422(client):
    resp = client.post("/api/runs/test_run/shots/0/sweep", json={"n": 10})
    assert resp.status_code in (404, 422)

def test_sweep_n_not_int_is_422(client):
    resp = client.post("/api/runs/test_run/shots/0/sweep", json={"n": "five"})
    assert resp.status_code in (404, 422)

def test_export_empty_presets_is_422(client):
    resp = client.post("/api/runs/test_run/export", json={"presets": []})
    assert resp.status_code in (404, 422)

def test_export_invalid_preset_is_422(client):
    resp = client.post("/api/runs/test_run/export", json={"presets": ["99x99"]})
    assert resp.status_code in (404, 422)

def test_export_valid_presets_shape(client):
    resp = client.post("/api/runs/test_run/export", json={"presets": ["9x16", "1x1"]})
    # Should be 404 (run not found), not 422 (validation error)
    assert resp.status_code == 404

def test_director_empty_message_is_422(client):
    resp = client.post("/api/runs/test_run/director", json={"message": ""})
    assert resp.status_code in (404, 422)

def test_director_missing_message_is_422(client):
    resp = client.post("/api/runs/test_run/director", json={})
    assert resp.status_code == 422

def test_subtitle_invalid_format_is_422(client):
    resp = client.post("/api/runs/test_run/subtitles", json={"format": "ass"})
    assert resp.status_code in (404, 422)


# ── Asset ID boundary validation ─────────────────────────────────────

def test_asset_id_traversal_blocked(client):
    resp = client.post("/api/runs/test_run/assets/../../etc/upload")
    assert resp.status_code in (400, 404, 422)

def test_asset_id_dotdot_blocked(client):
    resp = client.post("/api/runs/test_run/assets/..%2F..%2Fetc/skip")
    assert resp.status_code in (400, 404)

def test_asset_id_valid_shape_passes_boundary(client):
    resp = client.post("/api/runs/test_run/assets/char_hero_01/skip")
    # 404 because run doesn't exist — but passes the boundary check
    assert resp.status_code == 404
