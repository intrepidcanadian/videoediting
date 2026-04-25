"""state.json shadow-backup + restore behavior.

Every _save_state moves the previous primary to state.json.bak before writing
the new primary. If a corrupt primary is ever read, get_state promotes the
backup. This prevents a single bad write (bug, disk hiccup) from wiping a run."""

import json

import pytest

from errors import TrailerError
import pipeline


def _seed(root, run_id="run_a", payload=None):
    (root / run_id).mkdir()
    pipeline._save_state(run_id, payload or {"concept": "x", "shots": [], "status": "pending"})


def test_save_creates_primary_and_no_backup_on_first_write(tmp_output_root):
    _seed(tmp_output_root)
    primary = tmp_output_root / "run_a" / "state.json"
    backup = tmp_output_root / "run_a" / "state.json.bak"
    assert primary.exists()
    assert not backup.exists()  # first save has nothing to snapshot


def test_second_save_shadows_previous_primary(tmp_output_root):
    _seed(tmp_output_root, payload={"concept": "v1", "shots": [], "status": "pending"})
    pipeline._save_state("run_a", {"concept": "v2", "shots": [], "status": "pending"})
    primary = tmp_output_root / "run_a" / "state.json"
    backup = tmp_output_root / "run_a" / "state.json.bak"
    assert json.loads(primary.read_text())["concept"] == "v2"
    assert json.loads(backup.read_text())["concept"] == "v1"


def test_get_state_recovers_corrupt_primary_from_backup(tmp_output_root):
    _seed(tmp_output_root, payload={"concept": "good", "shots": [], "status": "pending"})
    pipeline._save_state("run_a", {"concept": "also_good", "shots": [], "status": "pending"})
    # Simulate mid-write corruption by scrambling the primary.
    primary = tmp_output_root / "run_a" / "state.json"
    primary.write_text("{not: valid json")
    # get_state should recover from .bak instead of raising.
    state = pipeline.get_state("run_a")
    assert state["concept"] == "good"
    # And the recovered backup should have been promoted to the primary.
    assert json.loads(primary.read_text())["concept"] == "good"


def test_get_state_restores_missing_primary_from_backup(tmp_output_root):
    _seed(tmp_output_root, payload={"concept": "v1", "shots": [], "status": "pending"})
    pipeline._save_state("run_a", {"concept": "v2", "shots": [], "status": "pending"})
    # Simulate a crash between "move primary to .bak" and "rename .tmp to primary"
    primary = tmp_output_root / "run_a" / "state.json"
    primary.unlink()
    state = pipeline.get_state("run_a")
    assert state["concept"] == "v1"


def test_get_state_raises_when_both_primary_and_backup_corrupt(tmp_output_root):
    _seed(tmp_output_root, payload={"concept": "v1", "shots": [], "status": "pending"})
    pipeline._save_state("run_a", {"concept": "v2", "shots": [], "status": "pending"})
    primary = tmp_output_root / "run_a" / "state.json"
    backup = tmp_output_root / "run_a" / "state.json.bak"
    primary.write_text("garbage 1")
    backup.write_text("garbage 2")
    with pytest.raises(TrailerError, match="corrupted"):
        pipeline.get_state("run_a")


def test_get_state_raises_when_primary_missing_and_no_backup(tmp_output_root):
    (tmp_output_root / "run_a").mkdir()
    with pytest.raises(FileNotFoundError):
        pipeline.get_state("run_a")
