"""Bit-slice arrangements: `n` identical slices laid out left-to-right.

A bitslice with rows=1 is a horizontal strip — typical datapath layout.
With rows>1 it folds into a rectangular grid (rows × ceil(n/rows)) — useful
when a 1×N strip would have an extreme aspect ratio.
"""
from __future__ import annotations

from vastu.core.block import Orient
from vastu.structured.grid import TilePlacement, array_layout


def bitslice_layout(
    instance_names: list[str],
    n: int,
    tile_w: float,
    tile_h: float,
    *,
    rows: int = 1,
    pitch_x: float = 0.0,
    pitch_y: float = 0.0,
    orient: Orient = Orient.R0,
) -> tuple[list[TilePlacement], float, float]:
    if n <= 0:
        raise ValueError(f"n={n} must be positive")
    if rows <= 0:
        raise ValueError(f"rows={rows} must be positive")
    cols = (n + rows - 1) // rows
    if len(instance_names) != n:
        raise ValueError(
            f"bitslice_layout expects {n} instances, got {len(instance_names)}"
        )
    return array_layout(
        instance_names,
        rows=rows,
        cols=cols,
        tile_w=tile_w,
        tile_h=tile_h,
        pitch_x=pitch_x,
        pitch_y=pitch_y,
        orient=orient,
    )
