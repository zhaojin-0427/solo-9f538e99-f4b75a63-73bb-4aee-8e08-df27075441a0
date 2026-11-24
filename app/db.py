"""SQLite persistence for band masks and committed coverage.

Three tables are used:

* ``scenes``      - one row per (UTC date, scene id); grid origin and size of a
                    scene are immutable once first registered.
* ``band_masks``  - the source 0/1 validity masks submitted per scene / band.
                    Re-submitting a band only overwrites that band's mask.
* ``coverage``    - one committed row per (date, grid cell), holding the sorted
                    scene-id lists that supply a valid red / NIR sample at that
                    cell. Queries read only this table; masks are never touched
                    on the query path.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import DEFAULT_DB_PATH

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
    """Create the schema if it does not exist yet."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
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
