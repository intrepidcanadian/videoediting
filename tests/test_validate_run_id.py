"""Path-traversal defense: pipeline.validate_run_id must reject anything that
could escape OUTPUT_ROOT, while accepting the run_ids create_run actually generates."""

import pytest

import pipeline


def test_accepts_typical_generated_run_id():
    # Shape produced by create_run (timestamp + slug).
    pipeline.validate_run_id("20260424_143022_test_concept")


def test_accepts_single_char():
    pipeline.validate_run_id("a")


def test_rejects_traversal():
    with pytest.raises(ValueError):
        pipeline.validate_run_id("../../etc/passwd")


def test_rejects_dot():
    with pytest.raises(ValueError):
        pipeline.validate_run_id(".")


def test_rejects_double_dot():
    with pytest.raises(ValueError):
        pipeline.validate_run_id("..")


def test_rejects_empty():
    with pytest.raises(ValueError):
        pipeline.validate_run_id("")


def test_rejects_slash():
    with pytest.raises(ValueError):
        pipeline.validate_run_id("a/b")


def test_rejects_backslash():
    with pytest.raises(ValueError):
        pipeline.validate_run_id("a\\b")


def test_rejects_leading_hyphen():
    # Could look like a flag to some CLI tools; pattern enforces alnum first char.
    with pytest.raises(ValueError):
        pipeline.validate_run_id("-evil")


def test_rejects_leading_underscore():
    with pytest.raises(ValueError):
        pipeline.validate_run_id("_leading")


def test_rejects_null_byte():
    with pytest.raises(ValueError):
        pipeline.validate_run_id("a\x00b")


def test_rejects_non_string():
    with pytest.raises(ValueError):
        pipeline.validate_run_id(None)  # type: ignore[arg-type]


def test_rejects_too_long():
    with pytest.raises(ValueError):
        pipeline.validate_run_id("a" * 200)
