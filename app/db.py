"""SQLite persistence for band masks, committed coverage and daily totals.

Tables:

* ``scenes``      - one row per (UTC date, scene id); grid origin and size of a
                    scene are immutable once first registered.
* ``band_masks``  - the source 0/1 validity masks submitted per scene / band.
                    Re-submitting a band only overwrites that band's mask.
* ``coverage``    - one committed row per (date, grid cell), holding the sorted
                    scene-id lists that supply a valid red / NIR sample at that
                    cell. Single-day gap queries read only this table.
* ``cell_registry`` - one row per grid cell that has at least one computable
                    day ever. Cross-period queries walk only these rows in the
                    requested rectangle, so the row count read is bounded by
                    ``width * height`` and never by the date span.
* ``cell_cumulative`` - per-cell checkpoints ``(x, y, date, total)`` where
                    ``total`` is the number of "computable" days (at least one
                    scene supplies both bands) from the beginning of record
                    through ``date`` inclusive. A checkpoint exists only on a
                    computable day. Two indexed probes per period per cell
                    answer any date range, regardless of the span.
* ``stats_meta``  - one-off migration marker for rebuilding the checkpoints
                    from a pre-existing (old Docker volume) ``coverage`` table.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import DEFAULT_DB_PATH

# Bump this if the cumulative-stat layout ever changes.
STATS_BACKFILL_KEY = "cell_cumulative_v1"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS scenes (
    date       TEXT NOT NULL,          -- UTC date, ISO 8601 YYYY-MM-DD
    scene_id   TEXT NOT NULL,
    origin_x   INTEGER NOT NULL,       -- grid coordinate of the upper-left cell
    origin_y   INTEGER NOT NULL,
    width      INTEGER NOT NULL,       -- fixed grid width  of this scene
    height     INTEGER NOT NULL,       -- fixed grid height of this scene
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (date, scene_id),
    CHECK (width  > 0),
    CHECK (height > 0)
);

CREATE TABLE IF NOT EXISTS band_masks (
    date       TEXT NOT NULL,
    scene_id   TEXT NOT NULL,
    band       TEXT NOT NULL CHECK (band IN ('red', 'nir')),
    mask       BLOB NOT NULL,          -- width*height bytes, row-major, 0/1
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (date, scene_id, band),
    FOREIGN KEY (date, scene_id) REFERENCES scenes (date, scene_id)
);

-- WITHOUT ROWID keyed by cell: point / range lookups by (date, x, y) and
-- rectangle scans by (date, y, x) both use the primary-key index.
CREATE TABLE IF NOT EXISTS coverage (
    date        TEXT NOT NULL,
    x           INTEGER NOT NULL,
    y           INTEGER NOT NULL,
    red_scenes  TEXT NOT NULL,         -- JSON array of scene ids, sorted
    nir_scenes  TEXT NOT NULL,         -- JSON array of scene ids, sorted
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (date, x, y)
) WITHOUT ROWID;

-- Sparse per-cell checkpoints for cross-period counting. A row exists only on
-- a day on which the cell is computable (some scene supplies both bands), and
-- ``total`` is the number of computable days from the beginning of record
-- through ``date`` inclusive. Counting a date range therefore needs only the
-- last checkpoint at/before each endpoint -- two index probes per period,
-- independent of the span and of how many days have been ingested.
CREATE TABLE IF NOT EXISTS cell_cumulative (
    x     INTEGER NOT NULL,
    y     INTEGER NOT NULL,
    date  TEXT NOT NULL,               -- UTC date, ISO 8601 YYYY-MM-DD
    total INTEGER NOT NULL CHECK (total > 0),
    PRIMARY KEY (x, y, date)
) WITHOUT ROWID;

-- One row per cell ever computable, so a rectangle query reads at most one
-- row per requested cell (not one per ingested day). Kept in exact sync with
-- cell_cumulative: both appear when a cell first becomes computable and are
-- removed when it has no computable day left.
CREATE TABLE IF NOT EXISTS cell_registry (
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    PRIMARY KEY (x, y)
) WITHOUT ROWID;

-- Allow a rectangle query to seek each grid row independently, so the number
-- of registry rows read is bounded by the rectangle's cell count rather than
-- by a whole column band of the grid.
CREATE INDEX IF NOT EXISTS idx_cell_registry_y_x ON cell_registry (y, x);

-- One-off migration marker; older data volumes have ``coverage`` but no
-- ``cell_cumulative`` and are backfilled once from committed coverage.
CREATE TABLE IF NOT EXISTS stats_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the pragmas required by the service."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # Readers never block on the writer and always see a single committed
    # snapshot for the duration of their statement.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create the schema and, on an older volume, backfill cumulative stats.

    Databases created by earlier versions already hold committed ``coverage``
    rows but no ``cell_cumulative`` table; they are backfilled exactly once
    (guarded by ``stats_meta``) from those rows.
    """
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _backfill_cumulative(conn)
    finally:
        conn.close()


def _backfill_cumulative(conn: sqlite3.Connection) -> None:
    """Rebuild per-cell cumulative checkpoints from committed coverage once."""
    done = conn.execute(
        "SELECT 1 FROM stats_meta WHERE key = ?", (STATS_BACKFILL_KEY,)
    ).fetchone()
    if done is not None:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check under the write lock so two starting workers cannot both
        # backfill.
        done = conn.execute(
            "SELECT 1 FROM stats_meta WHERE key = ?", (STATS_BACKFILL_KEY,)
        ).fetchone()
        if done is not None:
            conn.rollback()
            return

        cursor = conn.execute(
            "SELECT x, y, date, red_scenes, nir_scenes FROM coverage "
            "ORDER BY x, y, date"
        )
        # The marker is authoritative; rebuild from committed coverage in case
        # a previous start was interrupted after creating the new tables.
        conn.execute("DELETE FROM cell_cumulative")
        conn.execute("DELETE FROM cell_registry")
        batch: list[tuple[int, int, str, int]] = []
        cell: tuple[int, int] | None = None
        total = 0
        for row in cursor:
            key = (row["x"], row["y"])
            if key != cell:
                cell = key
                total = 0
            red = set(json.loads(row["red_scenes"]))
            nir = set(json.loads(row["nir_scenes"]))
            if red & nir:
                if total == 0:
                    conn.execute(
                        "INSERT OR IGNORE INTO cell_registry (x, y) VALUES (?, ?)",
                        key,
                    )
                total += 1
                batch.append((row["x"], row["y"], row["date"], total))
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT INTO cell_cumulative (x, y, date, total) "
                        "VALUES (?, ?, ?, ?)",
                        batch,
                    )
                    batch.clear()
        if batch:
            conn.executemany(
                "INSERT INTO cell_cumulative (x, y, date, total) VALUES (?, ?, ?, ?)",
                batch,
            )
        conn.execute(
            "INSERT INTO stats_meta (key, value) VALUES (?, 'done')",
            (STATS_BACKFILL_KEY,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def snapshot(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Read-only transaction: all statements see one committed snapshot."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


@contextmanager
def session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Transaction boundary: commit on success, roll back on failure."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
