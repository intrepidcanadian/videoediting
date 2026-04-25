"""pytest config: make the repo root importable and isolate disk writes."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_output_root(monkeypatch):
    """Redirect pipeline.OUTPUT_ROOT into a temp dir so tests never touch real runs."""
    import pipeline
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        monkeypatch.setattr(pipeline, "OUTPUT_ROOT", tmp)
        # The list-runs cache keys on directory contents; force a miss so we don't
        # see real runs during tests.
        pipeline._list_runs_cache["data"] = None
        yield tmp


@pytest.fixture
def tmp_library_root(monkeypatch):
    import library as library_mod
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        monkeypatch.setattr(library_mod, "LIBRARY_ROOT", tmp)
        yield tmp
