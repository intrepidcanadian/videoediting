"""Anchor parser / validator smoke tests. Anchors drive reference labeling into
Seedance and Nano Banana — a parser regression silently degrades every shot."""

import anchors


def test_parse_empty_returns_no_indices():
    r = anchors.parse("")
    assert r["image_indices"] == []
    assert r["video_indices"] == []
    assert r["audio_indices"] == []


def test_parse_simple_image():
    r = anchors.parse("The detective in @image1 walks forward.")
    assert r["image_indices"] == [1]
    assert r["video_indices"] == []


def test_parse_mixed_kinds_and_dedupes():
    r = anchors.parse("@image1 meets @image2 in @video1, again @image1.")
    assert r["image_indices"] == [1, 2]  # dedupes, preserves first-seen order
    assert r["video_indices"] == [1]


def test_parse_case_insensitive():
    r = anchors.parse("@IMAGE1 and @Video2")
    assert r["image_indices"] == [1]
    assert r["video_indices"] == [2]


def test_validate_detects_missing_refs():
    warnings = anchors.validate(
        "@image3 pushes through @video2's alley",
        image_count=2, video_count=1, audio_count=0,
    )
    # image3 doesn't exist (only 2), video2 doesn't exist (only 1)
    assert len(warnings) == 2
    assert any("@image3" in w for w in warnings)
    assert any("@video2" in w for w in warnings)


def test_validate_clean_prompt_has_no_warnings():
    warnings = anchors.validate(
        "@image1 opens @video1", image_count=1, video_count=1
    )
    assert warnings == []


def test_has_any():
    assert anchors.has_any("before @image1 after")
    assert not anchors.has_any("no refs here")
    assert not anchors.has_any("")


def test_auto_prepend_default_adds_when_missing_and_refs_exist():
    result = anchors.auto_prepend_default("a cinematic shot", image_count=1)
    assert "@image1" in result
    assert "cinematic shot" in result


def test_auto_prepend_default_leaves_alone_when_already_anchored():
    original = "The subject in @image2 turns"
    assert anchors.auto_prepend_default(original, image_count=2) == original


def test_auto_prepend_default_leaves_alone_when_no_refs():
    original = "a cinematic shot"
    assert anchors.auto_prepend_default(original, image_count=0) == original


def test_annotate_adds_labels_once():
    out = anchors.annotate_with_labels(
        "The woman in @image1 looks back.",
        {"image": {1: "primary character"}},
    )
    assert "(primary character)" in out
    # Idempotent — running twice doesn't double-annotate
    out2 = anchors.annotate_with_labels(out, {"image": {1: "primary character"}})
    assert out2.count("(primary character)") == 1
