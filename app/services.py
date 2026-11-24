"""Core service logic: mask submission / correction and gap queries.

Design guarantees (see README for the full rationale):

* A submission runs inside one transaction. On any failure nothing is
  committed, so operators can still query the previous committed result.
* Re-submitting a band only reads *that scene's* source masks and only
  rewrites coverage rows inside *that scene's* grid extent.
* Queries never read per-scene masks: they read the committed ``coverage``
  table only, at most one row per rectangle cell.
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
    GapRun,
    MaskSubmission,
)


class GeometryConflictError(Exception):
    """A re-submitted scene uses a different origin or size."""


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
                touched += 1
                continue

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
