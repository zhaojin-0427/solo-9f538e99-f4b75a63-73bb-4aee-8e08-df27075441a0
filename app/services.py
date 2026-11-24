"""Core service logic: mask submission / correction, gap queries and
two-period coverage comparison.

Design guarantees (see README for the full rationale):

* A submission runs inside one transaction. On any failure nothing is
  committed, so operators can still query the previous committed result.
* Re-submitting a band only reads *that scene's* source masks and only
  rewrites coverage rows inside *that scene's* grid extent. Cross-period
  statistics are updated only for cells where this scene flips the cell
  between computable and not computable on the submitted date.
* Gap queries never read per-scene masks: they read the committed ``coverage``
  table only, at most one row per rectangle cell.
* Comparison queries read only the committed per-cell cumulative checkpoints,
  never masks or per-day coverage, and their row reads are bounded by the
  rectangle's cell count -- not by the date span or ingested days.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date as date_type

from .models import (
    GAP_BOTH_MISSING,
    GAP_CATEGORY_LABELS,
    GAP_DIFFERENT_SCENES,
    GAP_NIR_ONLY,
    GAP_RED_ONLY,
    DecreasedCell,
    DecreasedRun,
    GapRun,
    MaskSubmission,
)


class GeometryConflictError(Exception):
    """A re-submitted scene uses a different origin or size."""


def _prefix_total(
    conn: sqlite3.Connection, x: int, y: int, before: str
) -> int:
    """Computable days for one cell from the beginning of record up to ``before``.

    ``cell_cumulative`` is sparse (one row per computable day), so this is a
    single descending index probe -- it never scans the date range.
    """
    row = conn.execute(
        "SELECT total FROM cell_cumulative "
        "WHERE x = ? AND y = ? AND date < ? ORDER BY date DESC LIMIT 1",
        (x, y, before),
    ).fetchone()
    return int(row["total"]) if row is not None else 0


def _flip_cumulative(
    conn: sqlite3.Connection, x: int, y: int, date_str: str, is_computable: bool
) -> None:
    """Add/remove one computable day for a cell and shift later checkpoints.

    Called only when a submission changes this cell on this date between
    computable and not computable. Only statistics rows for the same cell are
    read or written; coverage of other dates is never read. The cumulative
    totals after this date change by exactly +/-1, so later checkpoints are
    shifted rather than recomputed.
    """
    if is_computable:
        total = _prefix_total(conn, x, y, date_str) + 1
        conn.execute(
            "INSERT OR IGNORE INTO cell_registry (x, y) VALUES (?, ?)", (x, y)
        )
        conn.execute(
            "INSERT INTO cell_cumulative (x, y, date, total) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (x, y, date) DO UPDATE SET total = excluded.total",
            (x, y, date_str, total),
        )
        conn.execute(
            "UPDATE cell_cumulative SET total = total + 1 "
            "WHERE x = ? AND y = ? AND date > ?",
            (x, y, date_str),
        )
    else:
        cur = conn.execute(
            "DELETE FROM cell_cumulative WHERE x = ? AND y = ? AND date = ?",
            (x, y, date_str),
        )
        if cur.rowcount:
            conn.execute(
                "UPDATE cell_cumulative SET total = total - 1 "
                "WHERE x = ? AND y = ? AND date > ?",
                (x, y, date_str),
            )
        left = conn.execute(
            "SELECT 1 FROM cell_cumulative WHERE x = ? AND y = ? LIMIT 1",
            (x, y),
        ).fetchone()
        if left is None:
            conn.execute(
                "DELETE FROM cell_registry WHERE x = ? AND y = ?", (x, y)
            )


def _flatten(mask: list[list[int]]) -> bytes:
    return bytes(v for row in mask for v in row)


def submit_mask(conn: sqlite3.Connection, sub: MaskSubmission) -> dict:
    """Insert/correct one band mask and recompute this scene's coverage.

    Only coverage cells inside the submitted scene's extent are touched, and
    only the submitted scene's contribution to those cells changes.
    """
    date_str = sub.date.isoformat()
    flat = _flatten(sub.mask)
    valid_cells = sum(flat)

    # 1. Register the scene, or verify that the fixed geometry still matches.
    row = conn.execute(
        "SELECT origin_x, origin_y, width, height FROM scenes "
        "WHERE date = ? AND scene_id = ?",
        (date_str, sub.scene_id),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO scenes (date, scene_id, origin_x, origin_y, width, height) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                date_str,
                sub.scene_id,
                sub.origin_x,
                sub.origin_y,
                sub.width,
                sub.height,
            ),
        )
    elif (row["origin_x"], row["origin_y"], row["width"], row["height"]) != (
        sub.origin_x,
        sub.origin_y,
        sub.width,
        sub.height,
    ):
        raise GeometryConflictError(
            f"scene {sub.scene_id} on {date_str} is already registered with origin "
            f"({row['origin_x']}, {row['origin_y']}) and size "
            f"{row['width']}x{row['height']}; a scene's grid position and size are fixed"
        )

    # 2. Read this scene's own current mask for the *other* band. No masks of
    #    any other scene are read on the correction path.
    other_band = "nir" if sub.band == "red" else "red"
    other = conn.execute(
        "SELECT mask FROM band_masks WHERE date = ? AND scene_id = ? AND band = ?",
        (date_str, sub.scene_id, other_band),
    ).fetchone()
    other_by_index = (
        {i for i, v in enumerate(other["mask"]) if v} if other is not None else set()
    )
    new_by_index = {i for i, v in enumerate(flat) if v}

    # 3. Upsert the source mask for the submitted band.
    conn.execute(
        "INSERT INTO band_masks (date, scene_id, band, mask) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (date, scene_id, band) DO UPDATE SET "
        "mask = excluded.mask, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
        (date_str, sub.scene_id, sub.band, flat),
    )

    # 4. Recompute coverage for every cell in this scene's extent. Existing
    #    coverage rows are loaded in one indexed range scan; other scenes'
    #    contributions are preserved in their scene-id lists.
    ox, oy, w, h = sub.origin_x, sub.origin_y, sub.width, sub.height
    existing: dict[tuple[int, int], sqlite3.Row] = {
        (r["x"], r["y"]): r
        for r in conn.execute(
            "SELECT x, y, red_scenes, nir_scenes FROM coverage "
            "WHERE date = ? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
            (date_str, ox, ox + w - 1, oy, oy + h - 1),
        )
    }

    touched = 0
    flips = 0
    for iy in range(h):
        for ix in range(w):
            idx = iy * w + ix
            x, y = ox + ix, oy + iy
            row = existing.get((x, y))
            red_scenes = (
                set(json.loads(row["red_scenes"])) if row is not None else set()
            )
            nir_scenes = (
                set(json.loads(row["nir_scenes"])) if row is not None else set()
            )
            was_computable = bool(red_scenes & nir_scenes)

            # Drop this scene's previous contribution, then apply the new one.
            red_scenes.discard(sub.scene_id)
            nir_scenes.discard(sub.scene_id)
            if sub.band == "red":
                if idx in new_by_index:
                    red_scenes.add(sub.scene_id)
                if idx in other_by_index:
                    nir_scenes.add(sub.scene_id)
            else:
                if idx in new_by_index:
                    nir_scenes.add(sub.scene_id)
                if idx in other_by_index:
                    red_scenes.add(sub.scene_id)

            if not red_scenes and not nir_scenes:
                # No scene contributes anything here anymore.
                conn.execute(
                    "DELETE FROM coverage WHERE date = ? AND x = ? AND y = ?",
                    (date_str, x, y),
                )
            else:
                conn.execute(
                    "INSERT INTO coverage (date, x, y, red_scenes, nir_scenes) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (date, x, y) DO UPDATE SET "
                    "red_scenes = excluded.red_scenes, "
                    "nir_scenes = excluded.nir_scenes, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                    (
                        date_str,
                        x,
                        y,
                        json.dumps(sorted(red_scenes), separators=(",", ":")),
                        json.dumps(sorted(nir_scenes), separators=(",", ":")),
                    ),
                )
            touched += 1

            # Cross-period statistics are touched only where this scene flips
            # the cell between computable and not computable. No coverage of
            # any other date is read (or written) here.
            is_computable = bool(red_scenes & nir_scenes)
            if is_computable != was_computable:
                _flip_cumulative(conn, x, y, date_str, is_computable)
                flips += 1

    return {
        "date": sub.date,
        "scene_id": sub.scene_id,
        "band": sub.band,
        "origin_x": ox,
        "origin_y": oy,
        "width": w,
        "height": h,
        "valid_cells": valid_cells,
        "coverage_cells_updated": touched,
        "cumulative_cells_updated": flips,
    }


def _classify(red: set[str], nir: set[str]) -> str | None:
    """Return the gap category, or None when the cell is complete."""
    if red & nir:
        return None
    if red and nir:
        return GAP_DIFFERENT_SCENES
    if red:
        return GAP_RED_ONLY
    if nir:
        return GAP_NIR_ONLY
    return GAP_BOTH_MISSING


def query_gaps(
    conn: sqlite3.Connection,
    query_date: date_type,
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
) -> dict:
    """Find gap cells in a rectangle and merge same-category runs per row.

    A single indexed range read returns at most ``width * height`` coverage
    rows; masks are never read here.
    """
    date_str = query_date.isoformat()
    rows = conn.execute(
        "SELECT x, y, red_scenes, nir_scenes FROM coverage "
        "WHERE date = ? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
        (date_str, origin_x, origin_x + width - 1, origin_y, origin_y + height - 1),
    ).fetchall()
    by_cell: dict[tuple[int, int], tuple[set[str], set[str]]] = {
        (r["x"], r["y"]): (
            set(json.loads(r["red_scenes"])),
            set(json.loads(r["nir_scenes"])),
        )
        for r in rows
    }

    gaps: list[GapRun] = []
    category_counts = {
        GAP_RED_ONLY: 0,
        GAP_NIR_ONLY: 0,
        GAP_DIFFERENT_SCENES: 0,
        GAP_BOTH_MISSING: 0,
    }
    complete = 0
    gap_total = 0

    for iy in range(height):
        y = origin_y + iy
        run_category: str | None = None
        run_start = origin_x

        def _flush(end_x: int) -> None:
            nonlocal run_category, run_start
            if run_category is not None:
                gaps.append(
                    GapRun(
                        y=y,
                        x_start=run_start,
                        x_end=end_x,
                        category=run_category,  # type: ignore[arg-type]
                        category_label=GAP_CATEGORY_LABELS[run_category],
                        length=end_x - run_start + 1,
                    )
                )
            run_category = None

        for ix in range(width):
            x = origin_x + ix
            red, nir = by_cell.get((x, y), (set(), set()))
            category = _classify(red, nir)
            if category is None:
                _flush(x - 1)
                complete += 1
                continue
            gap_total += 1
            category_counts[category] += 1
            if run_category is None:
                run_category = category
                run_start = x
            elif category != run_category:
                _flush(x - 1)
                run_category = category
                run_start = x
        _flush(origin_x + width - 1)

    return {
        "date": query_date,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "width": width,
        "height": height,
        "total_cells": width * height,
        "complete_cells": complete,
        "gap_cells": gap_total,
        "category_counts": category_counts,
        "gaps": gaps,
    }


def compare_coverage(
    conn: sqlite3.Connection,
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
    period_a_start: date_type,
    period_a_end: date_type,
    period_b_start: date_type,
    period_b_end: date_type,
) -> dict:
    """Compare per-cell computable-day coverage between two inclusive periods.

    Reads only ``cell_registry`` and ``cell_cumulative``: at most one registry
    row per rectangle cell, and four index probes per cell (the last checkpoint
    at/before each period end, and the last one strictly before each period
    start). The work is bounded by ``width * height`` and does not grow with
    the date span or with the number of ingested days. Masks and per-day
    ``coverage`` rows are never read here.

    The periods may overlap or have different lengths; the ratio of
    computable days to inclusive calendar days is what is compared.
    """
    a_start_s = period_a_start.isoformat()
    a_end_s = period_a_end.isoformat()
    b_start_s = period_b_start.isoformat()
    b_end_s = period_b_end.isoformat()
    a_days = (period_a_end - period_a_start).days + 1
    b_days = (period_b_end - period_b_start).days + 1

    def _counts_range(start: str, end: str) -> dict[tuple[int, int], int]:
        """Computable-day counts per cell in ``[start, end]``.

        One statement per grid row (at most ``height`` of them), each seeking
        ``cell_registry`` by ``(y, x)`` so exactly the rectangle's cells are
        visited; every cell then costs two descending probes on
        ``cell_cumulative``. The date span is never scanned.
        """
        out: dict[tuple[int, int], int] = {}
        sql = (
            "SELECT r.x AS x, "
            "  (SELECT c.total FROM cell_cumulative c "
            "   WHERE c.x = r.x AND c.y = r.y AND c.date <= ? "
            "   ORDER BY c.date DESC LIMIT 1) AS upto_end, "
            "  (SELECT c.total FROM cell_cumulative c "
            "   WHERE c.x = r.x AND c.y = r.y AND c.date < ? "
            "   ORDER BY c.date DESC LIMIT 1) AS upto_before "
            "FROM cell_registry r "
            "WHERE r.y = ? AND r.x BETWEEN ? AND ?"
        )
        for y in range(origin_y, origin_y + height):
            rows = conn.execute(
                sql, (end, start, y, origin_x, origin_x + width - 1)
            ).fetchall()
            for r in rows:
                count = (r["upto_end"] or 0) - (r["upto_before"] or 0)
                if count:
                    out[(r["x"], y)] = count
        return out

    a_counts = _counts_range(a_start_s, a_end_s)
    b_counts = _counts_range(b_start_s, b_end_s)

    days_a_rows: list[list[int]] = []
    days_b_rows: list[list[int]] = []
    decreased: list[DecreasedCell] = []
    decreased_runs: list[DecreasedRun] = []

    for iy in range(height):
        y = origin_y + iy
        row_a: list[int] = []
        row_b: list[int] = []
        run_start: int | None = None
        for ix in range(width):
            x = origin_x + ix
            days_a = a_counts.get((x, y), 0)
            days_b = b_counts.get((x, y), 0)
            row_a.append(days_a)
            row_b.append(days_b)
            # Integers compare exactly: days_a/a_days > days_b/b_days.
            if days_a * b_days > days_b * a_days:
                decreased.append(
                    DecreasedCell(
                        x=x,
                        y=y,
                        computable_days_a=days_a,
                        computable_days_b=days_b,
                        ratio_a=round(days_a / a_days, 6),
                        ratio_b=round(days_b / b_days, 6),
                    )
                )
                if run_start is None:
                    run_start = x
            elif run_start is not None:
                decreased_runs.append(
                    DecreasedRun(
                        y=y, x_start=run_start, x_end=x - 1, length=x - run_start
                    )
                )
                run_start = None
        if run_start is not None:
            decreased_runs.append(
                DecreasedRun(
                    y=y,
                    x_start=run_start,
                    x_end=origin_x + width - 1,
                    length=origin_x + width - run_start,
                )
            )
        days_a_rows.append(row_a)
        days_b_rows.append(row_b)

    return {
        "origin_x": origin_x,
        "origin_y": origin_y,
        "width": width,
        "height": height,
        "period_a": {
            "start": period_a_start,
            "end": period_a_end,
            "calendar_days": a_days,
        },
        "period_b": {
            "start": period_b_start,
            "end": period_b_end,
            "calendar_days": b_days,
        },
        "total_cells": width * height,
        "computable_days_a": days_a_rows,
        "computable_days_b": days_b_rows,
        "decreased_count": len(decreased),
        "decreased_cells": decreased,
        "decreased_runs": decreased_runs,
    }


def get_scene(
    conn: sqlite3.Connection, query_date: date_type, scene_id: str
) -> dict | None:
    date_str = query_date.isoformat()
    scene = conn.execute(
        "SELECT origin_x, origin_y, width, height FROM scenes "
        "WHERE date = ? AND scene_id = ?",
        (date_str, scene_id),
    ).fetchone()
    if scene is None:
        return None
    bands = [
        {
            "band": r["band"],
            "valid_cells": sum(r["mask"]),
            "updated_at": r["updated_at"],
        }
        for r in conn.execute(
            "SELECT band, mask, updated_at FROM band_masks "
            "WHERE date = ? AND scene_id = ? ORDER BY band",
            (date_str, scene_id),
        )
    ]
    return {
        "date": query_date,
        "scene_id": scene_id,
        "origin_x": scene["origin_x"],
        "origin_y": scene["origin_y"],
        "width": scene["width"],
        "height": scene["height"],
        "bands": bands,
    }
