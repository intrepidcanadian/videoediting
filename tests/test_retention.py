"""Retention module — disk usage, candidate listing, cleanup."""

import json
import os
import time

import pytest

import pipeline
import retention


def _make_run(root, run_id, *, age_days: float = 0, status: str = "done", size_bytes: int = 1000):
    d = root / run_id
    d.mkdir()
    state = {"concept": "t", "shots": [], "status": status}
    state_path = d / "state.json"
    state_path.write_text(json.dumps(state))
    if size_bytes:
        (d / "trailer.mp4").write_bytes(b"x" * size_bytes)
    if age_days:
        ts = time.time() - (age_days * 86400)
        os.utime(state_path, (ts, ts))


def test_disk_usage_is_zero_for_empty_output_root(tmp_output_root):
    assert retention.disk_usage() == 0


def test_disk_usage_sums_files(tmp_output_root):
    _make_run(tmp_output_root, "a", size_bytes=1000)
    _make_run(tmp_output_root, "b", size_bytes=2500)
    total = retention.disk_usage()
    # state.json is a few dozen bytes per run; the mp4s dominate.
    assert total >= 3500


def test_list_candidates_respects_age_cutoff(tmp_output_root):
    _make_run(tmp_output_root, "fresh", age_days=0)
    _make_run(tmp_output_root, "old", age_days=45)
    _make_run(tmp_output_root, "ancient", age_days=90)
    # Clear list_runs cache that other tests may have set
    pipeline._list_runs_cache["data"] = None
    candidates = retention.list_candidates(older_than_days=30)
    ids = {c["run_id"] for c in candidates}
    assert ids == {"old", "ancient"}


def test_list_candidates_skips_active_runs(tmp_output_root):
    _make_run(tmp_output_root, "stale_but_busy", age_days=90, status="shots_generating")
    _make_run(tmp_output_root, "stale_and_done", age_days=90, status="done")
    pipeline._list_runs_cache["data"] = None
    ids = {c["run_id"] for c in retention.list_candidates(older_than_days=30)}
    assert "stale_and_done" in ids
    assert "stale_but_busy" not in ids


def test_list_candidates_includes_active_when_forced(tmp_output_root):
    _make_run(tmp_output_root, "stale_busy", age_days=90, status="stitching")
    pipeline._list_runs_cache["data"] = None
    ids = {c["run_id"] for c in retention.list_candidates(older_than_days=30, include_active=True)}
    assert "stale_busy" in ids


def test_delete_run_removes_directory_and_returns_freed_bytes(tmp_output_root):
    _make_run(tmp_output_root, "goner", size_bytes=5000)
    freed = retention.delete_run("goner")
    assert freed >= 5000
    assert not (tmp_output_root / "goner").exists()


def test_delete_run_rejects_invalid_id(tmp_output_root):
    with pytest.raises(ValueError):
        retention.delete_run("../../etc")


def test_cleanup_dry_run_deletes_nothing(tmp_output_root):
    _make_run(tmp_output_root, "old_a", age_days=60, size_bytes=1000)
    _make_run(tmp_output_root, "old_b", age_days=60, size_bytes=2000)
    pipeline._list_runs_cache["data"] = None
    result = retention.cleanup(older_than_days=30, dry_run=True)
    assert result["dry_run"] is True
    assert len(result["would_delete"]) == 2
    # Runs still on disk.
    assert (tmp_output_root / "old_a").exists()
    assert (tmp_output_root / "old_b").exists()


def test_cleanup_non_dry_run_deletes(tmp_output_root):
    _make_run(tmp_output_root, "old_a", age_days=60, size_bytes=1000)
    _make_run(tmp_output_root, "old_b", age_days=60, size_bytes=2000)
    _make_run(tmp_output_root, "fresh", age_days=0, size_bytes=500)
    pipeline._list_runs_cache["data"] = None
    result = retention.cleanup(older_than_days=30, dry_run=False)
    assert set(result["deleted"]) == {"old_a", "old_b"}
    assert not (tmp_output_root / "old_a").exists()
    assert not (tmp_output_root / "old_b").exists()
    assert (tmp_output_root / "fresh").exists()
