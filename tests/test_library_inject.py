"""Library inject path traversal defense."""

from pathlib import Path

import pytest

import library


def test_inject_rejects_traversal_target(tmp_path):
    (tmp_path / "run_a").mkdir()
    with pytest.raises(ValueError, match="target_dir"):
        library.inject_into_run(
            run_root=tmp_path / "run_a",
            kind="characters",
            slug="nonexistent",
            target_dir="../escape",
        )


def test_inject_rejects_absolute_target(tmp_path):
    (tmp_path / "run_a").mkdir()
    with pytest.raises(ValueError):
        library.inject_into_run(
            run_root=tmp_path / "run_a",
            kind="characters",
            slug="x",
            target_dir="/etc",
        )


def test_inject_rejects_non_allowlist_target(tmp_path):
    (tmp_path / "run_a").mkdir()
    with pytest.raises(ValueError, match="must be one of"):
        library.inject_into_run(
            run_root=tmp_path / "run_a",
            kind="characters",
            slug="x",
            target_dir="secrets",
        )


def test_inject_rejects_empty_target(tmp_path):
    (tmp_path / "run_a").mkdir()
    with pytest.raises(ValueError):
        library.inject_into_run(
            run_root=tmp_path / "run_a",
            kind="characters",
            slug="x",
            target_dir="",
        )


def test_inject_rejects_nested_path(tmp_path):
    (tmp_path / "run_a").mkdir()
    with pytest.raises(ValueError):
        library.inject_into_run(
            run_root=tmp_path / "run_a",
            kind="characters",
            slug="x",
            target_dir="references/../etc",
        )


def test_inject_allowlist_enumerated():
    # Every member of the allowlist must be a simple, single-level directory.
    for target in library._ALLOWED_INJECT_TARGETS:
        assert "/" not in target
        assert "\\" not in target
        assert target not in (".", "..")
