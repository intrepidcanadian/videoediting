"""Tests for featured_* empty-array logic, name auto-correction, pre-render
validation, and library tag search."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch


# ── featured_* empty-array vs None semantics ─────────────────────────────

def test_featured_chars_none_includes_all():
    """When featured_characters is absent (None), all cast refs should be included."""
    import pipeline
    shot = {"keyframe_prompt": "test", "motion_prompt": "test"}
    assert shot.get("featured_characters") is None
    _raw_fc = shot.get("featured_characters")
    featured = {n.strip().lower() for n in _raw_fc} if _raw_fc is not None else None
    assert featured is None


def test_featured_chars_empty_list_includes_none():
    """When featured_characters is [], no cast refs should be included."""
    shot = {"featured_characters": []}
    _raw_fc = shot.get("featured_characters")
    featured = {n.strip().lower() for n in _raw_fc} if _raw_fc is not None else None
    assert featured is not None
    assert len(featured) == 0


def test_featured_chars_with_names():
    """When featured_characters has names, only those should be included."""
    shot = {"featured_characters": ["Elena", "John"]}
    _raw_fc = shot.get("featured_characters")
    featured = {n.strip().lower() for n in _raw_fc} if _raw_fc is not None else None
    assert featured == {"elena", "john"}


def test_none_matches_all_refs():
    """featured_chars is None → condition `featured_chars is None` is True → include ref."""
    featured_chars = None
    cname = "elena"
    assert featured_chars is None or cname in featured_chars


def test_empty_set_excludes_all_refs():
    """featured_chars is empty set → condition `featured_chars is None` is False,
    and cname not in empty set → exclude ref."""
    featured_chars = set()
    cname = "elena"
    assert not (featured_chars is None or cname in featured_chars)


def test_named_set_includes_match():
    """featured_chars has 'elena' → include elena, exclude john."""
    featured_chars = {"elena"}
    assert featured_chars is None or "elena" in featured_chars
    assert not (featured_chars is None or "john" in featured_chars)


# ── _validate_featured_names auto-correction ─────────────────────────────

def test_validate_exact_match_no_correction(tmp_output_root):
    import pipeline
    story = {"shots": [{"featured_characters": ["Elena"], "featured_locations": [], "featured_props": []}]}
    state = {"cast": [{"name": "Elena"}], "locations": [], "props": []}
    run_id = "test_run"
    d = tmp_output_root / run_id
    d.mkdir()
    pipeline._save_state(run_id, state)
    pipeline._validate_featured_names(run_id, story, state)
    assert story["shots"][0]["featured_characters"] == ["Elena"]


def test_validate_substring_auto_correction(tmp_output_root):
    import pipeline
    story = {"shots": [{"featured_characters": ["Elena"], "featured_locations": [], "featured_props": []}]}
    state = {"cast": [{"name": "Elena Martinez"}], "locations": [], "props": []}
    run_id = "test_run2"
    d = tmp_output_root / run_id
    d.mkdir()
    pipeline._save_state(run_id, state)
    pipeline._validate_featured_names(run_id, story, state)
    assert story["shots"][0]["featured_characters"] == ["Elena Martinez"]


def test_validate_no_match_leaves_unchanged(tmp_output_root):
    import pipeline
    story = {"shots": [{"featured_characters": ["Bob"], "featured_locations": [], "featured_props": []}]}
    state = {"cast": [{"name": "Elena Martinez"}], "locations": [], "props": []}
    run_id = "test_run3"
    d = tmp_output_root / run_id
    d.mkdir()
    pipeline._save_state(run_id, state)
    pipeline._validate_featured_names(run_id, story, state)
    assert story["shots"][0]["featured_characters"] == ["Bob"]


def test_validate_location_auto_correction(tmp_output_root):
    import pipeline
    story = {"shots": [{"featured_characters": [], "featured_locations": ["stadium"], "featured_props": []}]}
    state = {"cast": [], "locations": [{"name": "City Stadium"}], "props": []}
    run_id = "test_run4"
    d = tmp_output_root / run_id
    d.mkdir()
    pipeline._save_state(run_id, state)
    pipeline._validate_featured_names(run_id, story, state)
    assert story["shots"][0]["featured_locations"] == ["City Stadium"]


def test_validate_prop_auto_correction(tmp_output_root):
    import pipeline
    story = {"shots": [{"featured_characters": [], "featured_locations": [], "featured_props": ["gun"]}]}
    state = {"cast": [], "locations": [], "props": [{"name": "Antique Gun"}]}
    run_id = "test_run5"
    d = tmp_output_root / run_id
    d.mkdir()
    pipeline._save_state(run_id, state)
    pipeline._validate_featured_names(run_id, story, state)
    assert story["shots"][0]["featured_props"] == ["Antique Gun"]


# ── _best_match helper ──────────────────────────────────────────────────

def test_best_match_exact():
    from pipeline import _best_match
    assert _best_match("elena", {"elena", "john"}) == "elena"


def test_best_match_substring():
    from pipeline import _best_match
    assert _best_match("elena", {"elena martinez", "john"}) == "elena martinez"


def test_best_match_reverse_substring():
    from pipeline import _best_match
    assert _best_match("elena martinez", {"elena", "john"}) == "elena"


def test_best_match_no_match():
    from pipeline import _best_match
    assert _best_match("bob", {"elena", "john"}) is None


def test_best_match_empty_candidates():
    from pipeline import _best_match
    assert _best_match("elena", set()) is None


# ── _pre_render_ref_check ────────────────────────────────────────────────

def test_pre_render_ref_check_none_featured(tmp_output_root):
    """None featured set → no warnings (backward compat)."""
    import pipeline
    run_id = "test_prerender"
    d = tmp_output_root / run_id
    d.mkdir()
    pipeline._save_state(run_id, {})
    warnings = []
    with patch.object(pipeline.logger, "warn", lambda rid, phase, msg: warnings.append(msg)):
        pipeline._pre_render_ref_check(run_id, 0, None, [], {}, "character", d)
    assert len(warnings) == 0


def test_pre_render_ref_check_empty_featured(tmp_output_root):
    """Empty featured set → no warnings (no characters expected)."""
    import pipeline
    run_id = "test_prerender2"
    d = tmp_output_root / run_id
    d.mkdir()
    pipeline._save_state(run_id, {})
    warnings = []
    with patch.object(pipeline.logger, "warn", lambda rid, phase, msg: warnings.append(msg)):
        pipeline._pre_render_ref_check(run_id, 0, set(), [], {}, "character", d)
    assert len(warnings) == 0


def test_pre_render_ref_check_missing_refs(tmp_output_root):
    """Featured character with no ref_paths → warns."""
    import pipeline
    run_id = "test_prerender3"
    d = tmp_output_root / run_id
    d.mkdir()
    pipeline._save_state(run_id, {})
    warnings = []
    with patch.object(pipeline.logger, "warn", lambda rid, phase, msg: warnings.append(msg)):
        pipeline._pre_render_ref_check(run_id, 0, {"elena"}, [], {}, "character", d)
    assert len(warnings) == 1
    assert "no ref files" in warnings[0]


def test_pre_render_ref_check_files_missing_on_disk(tmp_output_root):
    """Featured character has ref_paths but files don't exist on disk → warns."""
    import pipeline
    run_id = "test_prerender4"
    d = tmp_output_root / run_id
    d.mkdir()
    pipeline._save_state(run_id, {})
    warnings = []
    name_to_refs = {"elena": ["references/missing.jpg"]}
    with patch.object(pipeline.logger, "warn", lambda rid, phase, msg: warnings.append(msg)):
        pipeline._pre_render_ref_check(run_id, 0, {"elena"}, [], name_to_refs, "character", d)
    assert len(warnings) == 1
    assert "missing from disk" in warnings[0]


def test_pre_render_ref_check_files_exist(tmp_output_root):
    """Featured character with valid ref files on disk → no warnings."""
    import pipeline
    run_id = "test_prerender5"
    d = tmp_output_root / run_id
    d.mkdir()
    refs_dir = d / "references"
    refs_dir.mkdir()
    (refs_dir / "elena.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    pipeline._save_state(run_id, {})
    warnings = []
    name_to_refs = {"elena": ["references/elena.jpg"]}
    with patch.object(pipeline.logger, "warn", lambda rid, phase, msg: warnings.append(msg)):
        pipeline._pre_render_ref_check(run_id, 0, {"elena"}, [refs_dir / "elena.jpg"], name_to_refs, "character", d)
    assert len(warnings) == 0


# ── Library tag search ───────────────────────────────────────────────────

def test_library_tag_filter(tmp_library_root):
    import library as lib
    lib.save_item("characters", name="Elena", description="Main character", tags=["noir", "female"])
    lib.save_item("characters", name="John", description="Side character", tags=["male"])

    from routers.library import api_library_list
    result = api_library_list(kind="characters", tag="noir")
    assert len(result["characters"]) == 1
    assert result["characters"][0]["name"] == "Elena"


def test_library_tag_filter_case_insensitive(tmp_library_root):
    import library as lib
    lib.save_item("characters", name="Elena", description="Main character", tags=["Noir"])

    from routers.library import api_library_list
    result = api_library_list(kind="characters", tag="noir")
    assert len(result["characters"]) == 1


def test_library_tag_filter_no_match(tmp_library_root):
    import library as lib
    lib.save_item("characters", name="Elena", description="Main character", tags=["noir"])

    from routers.library import api_library_list
    result = api_library_list(kind="characters", tag="sci-fi")
    assert len(result["characters"]) == 0


def test_library_no_tag_returns_all(tmp_library_root):
    import library as lib
    lib.save_item("characters", name="Elena", description="MC", tags=["noir"])
    lib.save_item("characters", name="John", description="SC", tags=["male"])

    from routers.library import api_library_list
    result = api_library_list(kind="characters", tag=None)
    assert len(result["characters"]) == 2
