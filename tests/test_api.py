"""End-to-end tests for the mask submission and gap query API."""

from __future__ import annotations


def _post(client, **overrides):
    payload = {
        "date": "2026-11-23",
        "scene_id": "S1",
        "origin_x": 0,
        "origin_y": 0,
        "band": "red",
        "mask": [
            [1, 1, 0, 0],
            [1, 0, 0, 0],
        ],
    }
    payload.update(overrides)
    return client.post("/masks", json=payload)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_submit_red_then_query_classifies_red_only_and_both_missing(client):
    r = _post(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid_cells"] == 3
    assert body["coverage_cells_updated"] == 8

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 4, "height": 2},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["complete_cells"] == 0
    assert data["gap_cells"] == 8
    assert data["category_counts"]["red_only"] == 3
    assert data["category_counts"]["both_missing"] == 5

    # Row 0: red-only run x=0..1, then both-missing run x=2..3
    gaps = data["gaps"]
    assert gaps[0]["y"] == 0
    assert gaps[0]["category"] == "red_only"
    assert (gaps[0]["x_start"], gaps[0]["x_end"], gaps[0]["length"]) == (0, 1, 2)
    assert gaps[1]["category"] == "both_missing"
    assert (gaps[1]["x_start"], gaps[1]["x_end"]) == (2, 3)
    # Row 1: red-only single cell at x=0, then both-missing x=1..3
    assert gaps[2]["y"] == 1 and gaps[2]["category"] == "red_only"
    assert gaps[3]["category"] == "both_missing"
    assert (gaps[3]["x_start"], gaps[3]["x_end"], gaps[3]["length"]) == (1, 3, 3)


def test_same_scene_red_and_nir_marks_complete_cells(client):
    assert _post(client, band="red", mask=[[1, 1], [0, 1]]).status_code == 200
    assert _post(client, band="nir", mask=[[1, 0], [0, 1]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 2},
    )
    data = r.json()
    # Complete where both bands valid in S1: (0,0) and (1,1).
    assert data["complete_cells"] == 2
    assert data["gap_cells"] == 2
    counts = data["category_counts"]
    assert counts["red_only"] == 1   # cell (1,0): red only
    assert counts["nir_only"] == 0
    assert counts["both_missing"] == 1  # cell (0,1)
    assert counts["different_scenes"] == 0


def test_different_scenes_supply_bands_at_same_cell(client):
    # S1 covers origin (0,0); S2 is shifted and overlaps at cell (1,1).
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1], [1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", origin_x=1, origin_y=1,
                 band="nir", mask=[[1]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 2},
    )
    data = r.json()
    assert data["complete_cells"] == 0
    assert data["category_counts"]["different_scenes"] == 1
    assert data["category_counts"]["red_only"] == 3

    diff = [g for g in data["gaps"] if g["category"] == "different_scenes"]
    assert len(diff) == 1
    assert (diff[0]["y"], diff[0]["x_start"], diff[0]["x_end"]) == (1, 1, 1)
    assert "不同景" in diff[0]["category_label"]


def test_complete_when_any_scene_supplies_both_bands(client):
    # S1: red only everywhere; S2 supplies both bands at (0,0) -> complete.
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", band="red",
                 mask=[[1, 0]]).status_code == 200
    assert _post(client, scene_id="S2", band="nir",
                 mask=[[1, 0]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    )
    data = r.json()
    assert data["complete_cells"] == 1
    assert data["category_counts"]["red_only"] == 1


def test_correction_recomputes_only_this_scene(client):
    # Two overlapping scenes, then correct S1's red mask.
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", origin_x=1, origin_y=0,
                 band="nir", mask=[[1]]).status_code == 200
    # Cell (1,0): S1 red + S2 nir -> different_scenes before correction.

    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 0]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    )
    data = r.json()
    # (0,0): red only; (1,0): S1 red withdrawn, S2 nir remains -> nir_only.
    assert data["category_counts"]["red_only"] == 1
    assert data["category_counts"]["nir_only"] == 1
    assert data["category_counts"]["different_scenes"] == 0

    # The source mask read-back reflects the correction.
    info = client.get("/scenes/2026-11-23/S1").json()
    assert info["width"] == 2 and info["height"] == 1
    red_band = next(b for b in info["bands"] if b["band"] == "red")
    assert red_band["valid_cells"] == 1


def test_geometry_conflict_is_rejected(client):
    assert _post(client).status_code == 200
    r = _post(client, origin_x=5, origin_y=5)
    assert r.status_code == 409
    assert "fixed" in r.json()["detail"]

    # Size mismatch is also rejected.
    r = _post(client, mask=[[1, 1, 1]])
    assert r.status_code == 409


def test_failed_submission_keeps_previous_result(client):
    assert _post(client, band="red", mask=[[1, 1]]).status_code == 200

    # Oversized mask (>256) fails validation and must not change anything.
    big = [[1] * 257]
    r = _post(client, band="nir", mask=big)
    assert r.status_code == 422

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    )
    data = r.json()
    assert data["complete_cells"] == 0
    assert data["category_counts"]["red_only"] == 2


def test_invalid_mask_shapes_rejected(client):
    # Empty mask.
    r = _post(client, mask=[])
    assert r.status_code == 422
    # Ragged rows.
    r = _post(client, mask=[[1, 0], [1]])
    assert r.status_code == 422


def test_query_rectangle_limits_enforced(client):
    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 257, "height": 1},
    )
    assert r.status_code == 422


def test_rectangle_partially_outside_coverage_treated_as_both_missing(client):
    assert _post(client, mask=[[1]]).status_code == 200
    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 3, "height": 1},
    )
    data = r.json()
    assert data["total_cells"] == 3
    assert data["category_counts"]["red_only"] == 1
    assert data["category_counts"]["both_missing"] == 2


def test_dates_are_isolated(client):
    assert _post(client, date="2026-11-23", mask=[[1]]).status_code == 200
    r = client.get(
        "/gaps",
        params={"date": "2026-11-24", "origin_x": 0, "origin_y": 0,
                "width": 1, "height": 1},
    )
    data = r.json()
    assert data["gap_cells"] == 1
    assert data["category_counts"]["both_missing"] == 1


def test_scene_endpoint_404(client):
    r = client.get("/scenes/2026-11-23/nope")
    assert r.status_code == 404


def test_max_256_mask_accepted(client):
    mask = [[0] * 256 for _ in range(256)]
    mask[0][0] = 1
    r = _post(client, mask=mask)
    assert r.status_code == 200, r.text
    assert r.json()["valid_cells"] == 1
    assert r.json()["coverage_cells_updated"] == 256 * 256


def test_coverage_rows_never_exceed_rectangle_cells(client, monkeypatch):
    """Storage invariant backing the query read bound: one coverage row per
    cell at most, so a rectangle scan returns at most width*height rows."""
    from app import config
    import sqlite3

    # Two overlapping scenes contribute to the same cells; they must share
    # rows rather than create one row per (cell, scene).
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1], [1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", origin_x=1, origin_y=1,
                 band="nir", mask=[[1]]).status_code == 200

    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM coverage WHERE date = '2026-11-23'"
        ).fetchone()[0]
        in_rect = conn.execute(
            "SELECT COUNT(*) FROM coverage "
            "WHERE date = '2026-11-23' AND x BETWEEN 0 AND 1 AND y BETWEEN 0 AND 1"
        ).fetchone()[0]
    finally:
        conn.close()

    rectangle_cells = 2 * 2
    assert total <= rectangle_cells
    assert in_rect <= rectangle_cells
