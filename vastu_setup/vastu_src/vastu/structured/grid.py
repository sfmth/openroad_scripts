"""Deterministic R×C grid placement for `structured(kind=array)` blocks.

Children are laid out in row-major order: instance i goes to (row=i//cols, col=i%cols)
with origin at (col*pitch_x, row*pitch_y) measured from the structured block's
bottom-left corner. Pitch defaults to tile dimensions (abutted tiles); pass
non-zero values to leave channel space.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from vastu.core.block import Block, ChildPlacement, Orient, StructuredBlock


@dataclass(frozen=True)
class TilePlacement:
    """One child instance's position within a structured block (raw — no Block ref)."""

    instance: str
    row: int
    col: int
    x: float
    y: float
    orient: Orient = Orient.R0


def array_layout(
    instance_names: list[str],
    rows: int,
    cols: int,
    tile_w: float,
    tile_h: float,
    *,
    pitch_x: float = 0.0,
    pitch_y: float = 0.0,
    orient: Orient = Orient.R0,
) -> tuple[list[TilePlacement], float, float]:
    """Place `len(instance_names)` instances on a rows×cols grid.

    Returns (placements, total_w, total_h). `total_w/h` is the bounding-box of
    the grid (it equals (cols-1)*pitch_x + tile_w on the x-axis).

    Raises ValueError if the instance count exceeds rows*cols.
    """
    if rows <= 0 or cols <= 0:
        raise ValueError(f"rows={rows}, cols={cols} must both be positive")
    if len(instance_names) > rows * cols:
        raise ValueError(
            f"{len(instance_names)} instances do not fit in {rows}×{cols} grid"
        )
    px = pitch_x if pitch_x > 0 else tile_w
    py = pitch_y if pitch_y > 0 else tile_h
    placements: list[TilePlacement] = []
    for idx, inst in enumerate(instance_names):
        r = idx // cols
        c = idx % cols
        placements.append(
            TilePlacement(instance=inst, row=r, col=c, x=c * px, y=r * py, orient=orient)
        )
    total_w = (cols - 1) * px + tile_w if cols > 0 else 0.0
    total_h = (rows - 1) * py + tile_h if rows > 0 else 0.0
    return placements, total_w, total_h


def expand_leaves(
    block: Block, x_off: float = 0.0, y_off: float = 0.0, path: str = ""
) -> Iterable[tuple[str, float, float, float, float, Orient, Block]]:
    """Yield (full_path, x, y, w, h, orient, leaf_block) for every leaf inside `block`.

    A "leaf" is anything that isn't a StructuredBlock — i.e. HardBlock, SoftBlock,
    or HierarchicalBlock. Coordinates are absolute (relative to the outermost
    StructuredBlock's origin).
    """
    if not isinstance(block, StructuredBlock):
        # Non-structured top — treat block itself as the only leaf at (x_off, y_off).
        w, h = block.shape_func()[0] if block.shape_func() else (0.0, 0.0)
        yield (path or block.name, x_off, y_off, w, h, Orient.R0, block)
        return
    for cp in block.child_layout:
        full = f"{path}/{cp.name}" if path else cp.name
        if isinstance(cp.block, StructuredBlock):
            yield from expand_leaves(cp.block, x_off + cp.x, y_off + cp.y, full)
        else:
            w, h = cp.block.shape_func()[0] if cp.block.shape_func() else (0.0, 0.0)
            yield (full, x_off + cp.x, y_off + cp.y, w, h, cp.orient, cp.block)
