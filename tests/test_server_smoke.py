"""End-to-end smoke: boot the FastAPI app, hit endpoints that don't need
external APIs, confirm responses shape is sane and validation middleware fires."""

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient", reason="fastapi not installed")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402


@pytest.fixture
def client(tmp_output_root):
    # tmp_output_root monkeypatches pipeline.OUTPUT_ROOT so nothing writes to real outputs/.
    with TestClient(server.app) as c:
        yield c


def test_list_runs_returns_list_shape(client):
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert "runs" in body
    assert isinstance(body["runs"], list)


def test_rules_endpoint_returns_data(client):
    resp = client.get("/api/rules")
    assert resp.status_code == 200
    body = resp.json()
    assert "rules" in body
    assert isinstance(body["rules"], list)


def test_genres_endpoint(client):
    resp = client.get("/api/genres")
    assert resp.status_code == 200
    body = resp.json()
    assert "genres" in body
    assert any(g.get("id") == "neutral" for g in body["genres"])


def test_looks_endpoint(client):
    resp = client.get("/api/looks")
    assert resp.status_code == 200
    body = resp.json()
    assert "looks" in body


def test_invalid_run_id_traversal_blocked_by_middleware(client):
    # ../ segments should be normalized away by starlette before reaching here,
    # but a directly-encoded traversal-like string should be rejected.
    resp = client.get("/api/runs/..")
    # Either 400 (middleware catches) or 404 (path doesn't match a route). Never 200.
    assert resp.status_code in (400, 404)


def test_invalid_run_id_with_slash_blocked(client):
    resp = client.get("/api/runs/evil%2F..%2Fpasswd")
    # URL-encoded slash — if our middleware sees the decoded path, it rejects.
    assert resp.status_code in (400, 404)


def test_nonexistent_run_id_returns_404(client):
    # Valid shape but doesn't exist → 404, not 500.
    resp = client.get("/api/runs/nonexistent_run_id_xyz")
    assert resp.status_code == 404


def test_taste_endpoint_returns_profile(client):
    resp = client.get("/api/taste")
    assert resp.status_code == 200
    body = resp.json()
    # Shape may vary (empty profile at first) but it must be a dict.
    assert isinstance(body, dict)


def test_rules_test_endpoint_transforms_prompt(client):
    resp = client.post("/api/rules/test", json={
        "prompt": "  short   prompt  ",
        "target": "nano_banana",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "original" in body
    assert "transformed" in body


def test_invalid_run_id_post_body_blocked(client):
    resp = client.post("/api/runs/..%2F..%2Fetc/storyboard")
    assert resp.status_code in (400, 404, 405)


def test_missing_payload_on_rules_test_is_400(client):
    resp = client.post("/api/rules/test", json={})
    # prompt is required — should be a 4xx, not a 500 crash.
    assert 400 <= resp.status_code < 500
