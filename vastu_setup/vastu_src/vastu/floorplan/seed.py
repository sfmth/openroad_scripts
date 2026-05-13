"""Cell-grid seeding for soft blocks.

Once a soft block's footprint (w, h) is realized, distribute its constituent
standard cells on a row grid inside that footprint. The placement is a *seed*
for downstream global placers (RePlAce, Capo, etc.) — it doesn't have to be
optimal, just spread out enough that the placer doesn't start from a degenerate
single point.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from vastu.core.block import Placement


@dataclass(frozen=True)
class CellSeed:
    """One cell's seeded position inside a soft block (absolute coords)."""

    name: str
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class CellSpec:
    """A cell to be seeded — name and width (height = row_height)."""

    name: str
    w: float


def seed_cells(
    cells: list[CellSpec],
    block_pl: Placement,
    *,
    row_height: float,
) -> list[CellSeed]:
    """Lay out cells in rows of `row_height` inside `block_pl`'s footprint.

    Cells are placed left-to-right, wrapping when a row fills. Cells exceeding
    the block height are clipped (extra cells overflow at the top, which is
    flagged via the return list — nothing crashes). The caller can detect
    overflow by checking that the last seed's y + h is within the block.

    The seeds are emitted in absolute coordinates — block_pl's (x, y) is
    added to the within-block offset.
    """
    if row_height <= 0 or not cells:
        return []
    n_rows = max(1, int(block_pl.h // row_height))
    seeds: list[CellSeed] = []
    row = 0
    cur_x = 0.0
    for cell in cells:
        if cur_x + cell.w > block_pl.w:
            row += 1
            cur_x = 0.0
        if row >= n_rows:
            # Block too small. Stack extra cells past the top (caller will
            # observe the overflow); some planners are tolerant.
            row = n_rows
        x = block_pl.x + cur_x
        y = block_pl.y + row * row_height
        seeds.append(CellSeed(name=cell.name, x=x, y=y, w=cell.w, h=row_height))
        cur_x += cell.w
    return seeds


def fits_in_footprint(seeds: list[CellSeed], block_pl: Placement) -> bool:
    """All seeds lie within block_pl's bounding box."""
    for s in seeds:
        if s.x < block_pl.x - 1e-6 or s.y < block_pl.y - 1e-6:
            return False
        if s.x + s.w > block_pl.x + block_pl.w + 1e-6:
            return False
        if s.y + s.h > block_pl.y + block_pl.h + 1e-6:
            return False
    return True
