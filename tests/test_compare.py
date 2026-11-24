"""End-to-end tests for the two-period coverage comparison endpoint."""

from __future__ import annotations

import sqlite3
from datetime import date

from fastapi.testclient import TestClient


def _post(client, date, scene_id="S1", band="red", mask=None, ox=0, oy=0):
    payload = {
        "date": date,
        "scene_id": scene_id,
        "origin_x": ox,
        "origin_y": oy,
        "band": band,
        "mask": [[1, 1]] if mask is None else mask,
    }
    return client.post("/masks", json=payload)


def _both(client, date, scene_id="S1", mask=None, ox=0, oy=0):
    """Submit matching red and nir masks for one scene / date."""
    assert _post(client, date, scene_id, "red", mask, ox, oy).status_code == 200
    assert _post(client, date, scene_id, "nir", mask, ox, oy).status_code == 200


def _db_path(client):
    from app import config

    return config.DEFAULT_DB_PATH


def _compare(client, a, b, ox=0, oy=0, w=2, h=1):
    r = client.get(
        "/coverage/compare",
        params={
            "origin_x": ox,
            "origin_y": oy,
            "width": w,
            "height": h,
            "a_start": a[0],
            "a_end": a[1],
            "b_start": b[0],
            "b_end": b[1],
        },
    )
    return r


def _seed_mixed(client):
    """Two cells over several days with differing computability.

    day        cell (0,0)             cell (1,0)
    2026-11-01 computable             red only
    2026-11-02 computable             computable
    2026-11-03 red only               computable
    2026-11-05 computable             computable
    """
    _both(client, "2026-11-01", mask=[[1, 1]])  # then withdraw nir at cell 1
    assert _post(client, "2026-11-01", band="nir", mask=[[1, 0]]).status_code == 200
    _both(client, "2026-11-02")
    assert _post(client, "2026-11-03", band="red", mask=[[1, 1]]).status_code == 200
    assert _post(client, "2026-11-03", band="nir", mask=[[0, 1]]).status_code == 200
    _both(client, "2026-11-05")


def test_two_periods_may_overlap_and_have_different_lengths(client):
    _seed_mixed(client)

    # A = 3 days, B = 2 days, overlapping on 11-02 and 11-03.
    data = _compare(client, ("2026-11-01", "2026-11-03"),
                    ("2026-11-02", "2026-11-03")).json()
    assert data["period_a"]["calendar_days"] == 3
    assert data["period_b"]["calendar_days"] == 2
    assert data["computable_days_a"] == [[2, 2]]
    assert data["computable_days_b"] == [[1, 2]]
    # Overlapping days are counted independently in both periods.
    assert data["computable_days_a"][0][0] == 2
    assert data["computable_days_b"][0][0] == 1

    # cell (0,0): 2/3 > 1/2 -> decreased; cell (1,0): 2/3 < 2/2 -> increased.
    assert data["decreased_count"] == 1
    assert [(c["x"], c["y"]) for c in data["decreased_cells"]] == [(0, 0)]
    cell = data["decreased_cells"][0]
    assert cell["computable_days_a"] == 2
    assert cell["computable_days_b"] == 1
    assert cell["ratio_a"] > cell["ratio_b"]
    assert data["decreased_runs"] == [
        {"y": 0, "x_start": 0, "x_end": 0, "length": 1}
    ]


def test_equal_ratio_is_not_flagged_and_longer_period_is_handled(client):
    _seed_mixed(client)
    # Same 4-day period twice -> nothing decreased.
    data = _compare(client, ("2026-11-01", "2026-11-04"),
                    ("2026-11-01", "2026-11-04")).json()
    assert data["computable_days_a"] == data["computable_days_b"] == [[2, 2]]
    assert data["decreased_count"] == 0
    assert data["decreased_runs"] == []

    # Period B strictly later and shorter: cell (0,0) 2/3 -> 0/1 decreases,
    # cell (1,0) 2/3 -> 1/1 increases; only cell (0,0) is flagged.
    data = _compare(client, ("2026-11-01", "2026-11-03"),
                    ("2026-11-04", "2026-11-04")).json()
    assert data["computable_days_b"] == [[0, 0]]
    assert data["decreased_count"] == 2  # 2/3 > 0/1 and 2/3 > 0/1
    # And a day missing from B is simply not computable there.
    data = _compare(client, ("2026-11-01", "2026-11-03"),
                    ("2026-11-06", "2026-11-07")).json()
    assert data["computable_days_b"] == [[0, 0]]
    assert data["decreased_count"] == 2


def test_last_same_scene_pair_invalidation_updates_cumulative(client):
    """Correcting the only remaining same-scene pair must flip a day to
    not-computable and shift later checkpoints, without any other day's
    coverage being reread."""
    # Day 1 and day 3: S1 supplies red+nir at cell (0,0).
    _both(client, "2026-11-01", "S1", mask=[[1]])
    _both(client, "2026-11-03", "S1", mask=[[1]])
    # S2 supplies only red at the same cell on day 1. On its own that is not a
    # same-scene pair, so it must not keep the cell computable.
    assert _post(client, "2026-11-01", "S2", "red", [[1]]).status_code == 200

    data = _compare(client, ("2026-11-01", "2026-11-03"),
                    ("2026-11-01", "2026-11-03"), w=1).json()
    assert data["computable_days_a"] == [[2]]  # 11-01 and 11-03

    # Withdraw S1's nir: S1 red + S2 red remain, but no same-scene pair at all.
    r = _post(client, "2026-11-01", "S1", "nir", [[0]])
    assert r.status_code == 200
    assert r.json()["cumulative_cells_updated"] == 1

    data = _compare(client, ("2026-11-01", "2026-11-03"),
                    ("2026-11-01", "2026-11-03"), w=1).json()
    assert data["computable_days_a"] == [[1]]  # only 11-03 remains
    assert data["computable_days_b"] == [[1]]
    # Both periods lost the same day, so their ratios still match.
    assert data["decreased_count"] == 0
    # Day 3's checkpoint was shifted down with no re-read of that day.
    conn = sqlite3.connect(str(_db_path(client)))
    try:
        totals = [
            r[0]
            for r in conn.execute(
                "SELECT total FROM cell_cumulative WHERE x = 0 AND y = 0 "
                "ORDER BY date"
            )
        ]
    finally:
        conn.close()
    assert totals == [1]  # a single checkpoint, total 1 (day 3)

    # Restoring the pair brings the earlier day back (flip in the other
    # direction), and the later checkpoint returns to 2.
    r = _post(client, "2026-11-01", "S1", "nir", [[1]])
    assert r.status_code == 200
    assert r.json()["cumulative_cells_updated"] == 1
    data = _compare(client, ("2026-11-01", "2026-11-03"),
                    ("2026-11-01", "2026-11-03"), w=1).json()
    assert data["computable_days_a"] == [[2]]


def test_same_scene_requirement_across_two_scenes(client):
    # Two different scenes each providing one band do not make a cell
    # computable, so they must never enter the cumulative stats.
    assert _post(client, "2026-11-01", "R", "red", [[1]], 0, 0).status_code == 200
    assert _post(client, "2026-11-01", "N", "nir", [[1]], 0, 0).status_code == 200
    data = _compare(client, ("2026-11-01", "2026-11-01"),
                    ("2026-11-01", "2026-11-01"), w=1).json()
    assert data["computable_days_a"] == [[0]]
    assert data["decreased_count"] == 0


def test_only_flipped_cells_update_cumulative_stats(client):
    # A 4x1 scene where only cells 0 and 2 become computable; the response
    # reports exactly those flips, and a no-op resubmission reports none.
    red = [[1, 1, 1, 1]]
    nir = [[1, 0, 1, 0]]
    r = _post(client, "2026-11-01", "S1", "red", red, 0, 0)
    assert r.status_code == 200
    r = _post(client, "2026-11-01", "S1", "nir", nir, 0, 0)
    assert r.status_code == 200
    assert r.json()["cumulative_cells_updated"] == 2

    # Same mask again: no flip, no statistic write.
    r = _post(client, "2026-11-01", "S1", "nir", nir, 0, 0)
    assert r.json()["cumulative_cells_updated"] == 0

    # Correction that makes cell 0 stop being computable is exactly one flip;
    # cells 1, 2 and 3 keep their previous computability.
    r = _post(client, "2026-11-01", "S1", "red", [[0, 1, 1, 1]], 0, 0)
    assert r.json()["cumulative_cells_updated"] == 1

    data = _compare(client, ("2026-11-01", "2026-11-01"),
                    ("2026-11-01", "2026-11-01"), w=4).json()
    assert data["computable_days_a"] == [[0, 0, 1, 0]]


def test_compare_reads_no_masks_or_daily_coverage_and_is_row_bounded(client):
    """The fixed-rectangle read is bounded by the cell count, not by the span
    or the number of ingested days."""
    # Ingest 59 days (Oct 1 .. Nov 28) for a 4x2 rectangle.
    from datetime import timedelta

    day = date(2026, 10, 1)
    while day <= date(2026, 11, 28):
        _both(client, day.isoformat(), "S1", mask=[[1] * 4, [1] * 4])
        day += timedelta(days=1)

    from app import config, db, services

    base = db.connect(config.DEFAULT_DB_PATH)
    forbidden: list[str] = []

    class ConnProxy:
        def __init__(self, conn):
            self._conn = conn
            self.fetched_rows = 0
            self.statements: list[str] = []

        def execute(self, sql, params=()):
            self.statements.append(sql)
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            self.fetched_rows += len(rows)
            return _Rows(rows)

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    def authorizer(action, arg1, arg2, dbname, trigger):  # noqa: ANN001
        if action == sqlite3.SQLITE_READ and arg1 in ("band_masks", "coverage"):
            forbidden.append(arg1)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    base.set_authorizer(authorizer)
    proxy = ConnProxy(base)
    w, h = 4, 2
    result = services.compare_coverage(
        proxy, 0, 0, w, h,
        date(2026, 10, 1), date(2026, 11, 28),
        date(2026, 10, 15), date(2026, 11, 20),
    )
    base.close()

    assert forbidden == []  # no source mask or per-day coverage read
    # One statement per grid row (height), each returning at most one row per
    # cell; the 59-day span and 59 ingested days do not increase rows read.
    assert len(proxy.statements) == 2 * h
    assert proxy.fetched_rows <= 2 * w * h
    assert result["computable_days_a"] == [[59] * 4] * 2
    # B covers Oct 15 .. Nov 20, all computable: 17 + 20 = 37 days.
    assert result["computable_days_b"] == [[37] * 4] * 2


def test_compare_validation(client):
    r = client.get(
        "/coverage/compare",
        params={"origin_x": 0, "origin_y": 0, "width": 257, "height": 1,
                "a_start": "2026-11-01", "a_end": "2026-11-02",
                "b_start": "2026-11-01", "b_end": "2026-11-02"},
    )
    assert r.status_code == 422

    r = client.get(
        "/coverage/compare",
        params={"origin_x": 0, "origin_y": 0, "width": 1, "height": 1,
                "a_start": "2026-11-03", "a_end": "2026-11-01",
                "b_start": "2026-11-01", "b_end": "2026-11-02"},
    )
    assert r.status_code == 422


def test_failed_submission_rolls_back_masks_coverage_and_stats(client, monkeypatch):
    from app import main, services

    r = _post(client, "2026-11-01", "S1", "red", [[1]], 0, 0)
    assert r.status_code == 200

    before = _compare(client, ("2026-11-01", "2026-11-01"),
                      ("2026-11-01", "2026-11-01"), w=1).json()

    # Fail after the mask/coverage work but while updating the cross-day stats.
    calls = {"n": 0}
    original = services._flip_cumulative

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 1:
            raise RuntimeError("simulated failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(services, "_flip_cumulative", boom)
    with TestClient(main.app, raise_server_exceptions=False) as failing:
        r = failing.post(
            "/masks",
            json={"date": "2026-11-01", "scene_id": "S1", "origin_x": 0,
                  "origin_y": 0, "band": "nir", "mask": [[1]]},
        )
    assert r.status_code == 500

    after = _compare(client, ("2026-11-01", "2026-11-01"),
                     ("2026-11-01", "2026-11-01"), w=1).json()
    assert after == before
    # The nir mask was not committed either.
    info = client.get("/scenes/2026-11-01/S1").json()
    assert [b["band"] for b in info["bands"]] == ["red"]


def test_old_volume_backfill_from_committed_coverage(tmp_path, monkeypatch):
    """A database created by the previous version (no stats tables) gets its
    cumulative index built once from committed coverage on startup."""
    db_path = tmp_path / "old.db"
    old = sqlite3.connect(str(db_path))
    old.executescript(
        """
        CREATE TABLE scenes (date TEXT NOT NULL, scene_id TEXT NOT NULL,
            origin_x INTEGER NOT NULL, origin_y INTEGER NOT NULL,
            width INTEGER NOT NULL, height INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT 'x', PRIMARY KEY (date, scene_id));
        CREATE TABLE band_masks (date TEXT NOT NULL, scene_id TEXT NOT NULL,
            band TEXT NOT NULL, mask BLOB NOT NULL, updated_at TEXT NOT NULL
            DEFAULT 'x', PRIMARY KEY (date, scene_id, band));
        CREATE TABLE coverage (date TEXT NOT NULL, x INTEGER NOT NULL,
            y INTEGER NOT NULL, red_scenes TEXT NOT NULL,
            nir_scenes TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT 'x',
            PRIMARY KEY (date, x, y)) WITHOUT ROWID;
        """
    )
    # Two committed days for cell (0,0): computable on day 1, not on day 2.
    old.execute(
        "INSERT INTO coverage VALUES ('2026-11-01', 0, 0, '[\"S1\"]', '[\"S1\"]', 'x')"
    )
    old.execute(
        "INSERT INTO coverage VALUES ('2026-11-02', 0, 0, '[\"S1\"]', '[]', 'x')"
    )
    old.execute(
        "INSERT INTO coverage VALUES ('2026-11-03', 0, 0, '[\"S1\"]', '[\"S1\"]', 'x')"
    )
    old.commit()
    old.close()

    from app import db, services

    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cell_cumulative").fetchone()[0] == 2
        result = services.compare_coverage(
            conn, 0, 0, 1, 1,
            date(2026, 11, 1), date(2026, 11, 3),
            date(2026, 11, 1), date(2026, 11, 3),
        )
        assert result["computable_days_a"] == [[2]]
    finally:
        conn.close()

    # Running startup again must not double-count.
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cell_cumulative").fetchone()[0] == 2
    finally:
        conn.close()


def test_old_volume_served_by_api_then_incremental_updates(monkeypatch, tmp_path):
    """An old data volume is usable through the API: startup backfills stats,
    the compare endpoint answers, and later submissions update incrementally.
    """
    db_path = tmp_path / "old-volume.db"
    old = sqlite3.connect(str(db_path))
    old.executescript(
        """
        CREATE TABLE scenes (date TEXT NOT NULL, scene_id TEXT NOT NULL,
            origin_x INTEGER NOT NULL, origin_y INTEGER NOT NULL,
            width INTEGER NOT NULL, height INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT 'x', PRIMARY KEY (date, scene_id));
        CREATE TABLE band_masks (date TEXT NOT NULL, scene_id TEXT NOT NULL,
            band TEXT NOT NULL, mask BLOB NOT NULL, updated_at TEXT NOT NULL
            DEFAULT 'x', PRIMARY KEY (date, scene_id, band));
        CREATE TABLE coverage (date TEXT NOT NULL, x INTEGER NOT NULL,
            y INTEGER NOT NULL, red_scenes TEXT NOT NULL,
            nir_scenes TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT 'x',
            PRIMARY KEY (date, x, y)) WITHOUT ROWID;
        INSERT INTO scenes VALUES ('2026-11-01', 'S1', 0, 0, 1, 1, 'x');
        INSERT INTO coverage VALUES ('2026-11-01', 0, 0, '["S1"]', '["S1"]', 'x');
        INSERT INTO coverage VALUES ('2026-11-02', 0, 0, '["S1"]', '["S1"]', 'x');
        """
    )
    old.commit()
    old.close()

    monkeypatch.setenv("GAPCHECK_DB_PATH", str(db_path))
    import importlib

    from app import config, db, main

    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(main)
    with TestClient(main.app) as client:
        # Backfilled stats answer immediately for the old committed days.
        r = _compare(client, ("2026-11-01", "2026-11-02"),
                     ("2026-11-01", "2026-11-02"), w=1)
        assert r.status_code == 200, r.text
        assert r.json()["computable_days_a"] == [[2]]

        # A fresh day submitted through the API joins the same cumulative line.
        assert _post(client, "2026-11-03", "S1", "red", [[1]]).status_code == 200
        r = _post(client, "2026-11-03", "S1", "nir", [[1]])
        assert r.status_code == 200
        assert r.json()["cumulative_cells_updated"] == 1

        data = _compare(client, ("2026-11-01", "2026-11-03"),
                        ("2026-11-01", "2026-11-03"), w=1).json()
        assert data["computable_days_a"] == [[3]]
        # Re-submitting identical masks does not inflate the count.
        assert _post(client, "2026-11-03", "S1", "nir", [[1]]).json()[
            "cumulative_cells_updated"
        ] == 0
        data = _compare(client, ("2026-11-01", "2026-11-03"),
                        ("2026-11-01", "2026-11-03"), w=1).json()
        assert data["computable_days_a"] == [[3]]


def test_compare_endpoint_is_in_openapi_docs(client):
    spec = client.get("/openapi.json").json()
    assert "/coverage/compare" in spec["paths"]
