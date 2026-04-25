"""Regression tests for pipeline._migrate_shot. This function runs on every
state read and on every shot mutation — a bug here corrupts runs silently."""

import pipeline


def test_migrate_legacy_flat_shot_with_path():
    shot = {"idx": 0, "path": "shots/01.mp4", "status": "ready", "error": None}
    pipeline._migrate_shot(shot, num_variants=1)
    assert "variants" in shot
    assert len(shot["variants"]) == 1
    assert shot["variants"][0]["path"] == "shots/01.mp4"
    assert shot["variants"][0]["status"] == "ready"
    assert shot["primary_variant"] == 0


def test_migrate_legacy_flat_shot_without_path():
    shot = {"idx": 0, "status": "pending"}
    pipeline._migrate_shot(shot, num_variants=3)
    # 3 pending variant slots materialized
    assert len(shot["variants"]) == 3
    assert all(v["status"] == "pending" for v in shot["variants"])


def test_migrate_pads_variants_to_num_variants():
    shot = {"variants": [{"idx": 0, "path": "a.mp4", "status": "ready"}]}
    pipeline._migrate_shot(shot, num_variants=3)
    assert len(shot["variants"]) == 3
    assert shot["variants"][1]["status"] == "pending"


def test_migrate_repairs_missing_fields_on_variants():
    # A variant missing half its fields — should be repaired, not rejected.
    shot = {"variants": [{"path": "x.mp4"}]}
    pipeline._migrate_shot(shot, num_variants=1)
    v = shot["variants"][0]
    assert v["path"] == "x.mp4"
    assert v["status"] == "ready"  # inferred from presence of path
    assert v["seed"] is None
    assert v["error"] is None
    assert v["updated_at"] is None
    assert v["idx"] == 0


def test_migrate_drops_non_dict_variant_entries():
    shot = {"variants": [None, "bad", {"path": "ok.mp4"}]}
    pipeline._migrate_shot(shot, num_variants=1)
    assert len(shot["variants"]) == 1
    assert shot["variants"][0]["path"] == "ok.mp4"


def test_migrate_clamps_out_of_range_primary_variant():
    shot = {"variants": [{"path": "a.mp4"}], "primary_variant": 99}
    pipeline._migrate_shot(shot, num_variants=1)
    assert shot["primary_variant"] == 0


def test_migrate_clamps_negative_primary_variant():
    shot = {"variants": [{"path": "a.mp4"}, {"path": "b.mp4"}], "primary_variant": -1}
    pipeline._migrate_shot(shot, num_variants=2)
    assert shot["primary_variant"] == 0


def test_migrate_is_idempotent():
    shot = {"idx": 0, "path": "shots/01.mp4", "status": "ready"}
    pipeline._migrate_shot(shot, num_variants=2)
    snapshot = {"variants": [dict(v) for v in shot["variants"]],
                "primary_variant": shot["primary_variant"],
                "path": shot.get("path"), "status": shot.get("status")}
    # Re-run migration — should be a no-op.
    pipeline._migrate_shot(shot, num_variants=2)
    assert [dict(v) for v in shot["variants"]] == snapshot["variants"]
    assert shot["primary_variant"] == snapshot["primary_variant"]
    assert shot.get("path") == snapshot["path"]
    assert shot.get("status") == snapshot["status"]


def test_migrate_keeps_flat_fields_in_sync_with_primary():
    shot = {"variants": [
        {"path": "a.mp4", "status": "ready"},
        {"path": "b.mp4", "status": "ready"},
    ], "primary_variant": 1}
    pipeline._migrate_shot(shot, num_variants=2)
    # Flat fields mirror the primary variant.
    assert shot["path"] == "b.mp4"
    assert shot["status"] == "ready"


def test_migrate_handles_non_list_variants_gracefully():
    shot = {"variants": "not a list", "primary_variant": 0}
    pipeline._migrate_shot(shot, num_variants=2)
    assert isinstance(shot["variants"], list)
    assert len(shot["variants"]) == 2
