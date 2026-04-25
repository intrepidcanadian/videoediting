"""log.jsonl rotation — prevents unbounded growth on long-running runs."""

import importlib

import pytest


@pytest.fixture
def small_threshold(monkeypatch):
    """Force tiny rotation thresholds so we don't have to write megabytes."""
    import logger
    monkeypatch.setattr(logger, "LOG_MAX_BYTES", 1024)          # 1 KB trigger
    monkeypatch.setattr(logger, "LOG_ROTATE_KEEP_TAIL", 256)    # keep 256 B
    yield logger


def test_rotation_fires_when_threshold_exceeded(tmp_output_root, small_threshold):
    import logger
    monkeypatch_root = tmp_output_root
    # logger.OUTPUT_ROOT points at real outputs/; redirect it
    logger.OUTPUT_ROOT = monkeypatch_root  # type: ignore[assignment]
    rid = "run_a"
    (monkeypatch_root / rid).mkdir()
    # Write enough entries to push past 1 KB.
    for i in range(100):
        logger.info(rid, "phase", f"entry {i} with some padding text " * 4)
    p = monkeypatch_root / rid / "log.jsonl"
    rotated = monkeypatch_root / rid / "log.jsonl.1"
    assert rotated.exists(), "rotation didn't produce a .1 file"
    # Rotated file keeps at most ~256 bytes (our test tail budget).
    assert rotated.stat().st_size <= 256 + 200, (
        f"rotated tail larger than budget: {rotated.stat().st_size} bytes"
    )


def test_rotation_preserves_valid_jsonl(tmp_output_root, small_threshold):
    import json
    import logger
    logger.OUTPUT_ROOT = tmp_output_root
    rid = "run_b"
    (tmp_output_root / rid).mkdir()
    for i in range(100):
        logger.info(rid, "phase", f"entry {i:04d} " + "x" * 40)
    rotated = tmp_output_root / rid / "log.jsonl.1"
    # Every non-empty line in the rotated file must be valid JSON.
    for line in rotated.read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)  # must not raise
        assert obj["phase"] == "phase"


def test_no_rotation_under_threshold(tmp_output_root):
    import logger
    logger.OUTPUT_ROOT = tmp_output_root
    rid = "run_c"
    (tmp_output_root / rid).mkdir()
    logger.info(rid, "phase", "just one line")
    assert not (tmp_output_root / rid / "log.jsonl.1").exists()
