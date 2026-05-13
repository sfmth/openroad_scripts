from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Orient(Enum):
    R0 = "R0"
    R90 = "R90"
    R180 = "R180"
    R270 = "R270"
    MX = "MX"
    MY = "MY"
    MXR90 = "MXR90"
    MYR90 = "MYR90"

    def swaps_wh(self) -> bool:
        return self in (Orient.R90, Orient.R270, Orient.MXR90, Orient.MYR90)


def transform_offset(
    dx: float, dy: float, w: float, h: float, orient: Orient
) -> tuple[float, float]:
    if orient is Orient.R0:
        return (dx, dy)
    if orient is Orient.R90:
        return (h - dy, dx)
    if orient is Orient.R180:
        return (w - dx, h - dy)
    if orient is Orient.R270:
        return (dy, w - dx)
    if orient is Orient.MX:
        return (dx, h - dy)
    if orient is Orient.MY:
        return (w - dx, dy)
    if orient is Orient.MXR90:
        return (h - dy, w - dx)
    if orient is Orient.MYR90:
        return (dy, dx)
    raise ValueError(f"unknown orient {orient}")


@dataclass(frozen=True)
class Placement:
    """Result of packing/SA: where a block sits on its parent's canvas."""

    x: float
    y: float
    w: float
    h: float
    orient: Orient = Orient.R0

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h


@dataclass
class Block:
    """Base class for floorplan blocks. Subclasses define how shapes are derived."""

    name: str
    pins: list = field(default_factory=list)
    # Original Verilog module this block came from (empty for synthetic blocks).
    module_name: str = ""

    def shape_func(self) -> list[tuple[float, float]]:
        """Pareto curve of (w, h) shapes this block can take."""
        raise NotImplementedError


@dataclass
class SoftBlock(Block):
    """Variable-aspect-ratio block. Area is fixed; (w,h) chosen subject to AR range."""

    area: float = 0.0
    ar_lo: float = 0.5
    ar_hi: float = 1.5
    cells: list = field(default_factory=list)

    def shape_func(self, samples: int = 5) -> list[tuple[float, float]]:
        if self.area <= 0:
            return [(0.0, 0.0)]
        if samples < 2:
            ar = math.sqrt(self.ar_lo * self.ar_hi)
            w = math.sqrt(self.area * ar)
            h = self.area / w
            return [(w, h)]
        # Sample AR geometrically across [ar_lo, ar_hi].
        log_lo = math.log(self.ar_lo)
        log_hi = math.log(self.ar_hi)
        out: list[tuple[float, float]] = []
        for i in range(samples):
            t = i / (samples - 1)
            ar = math.exp(log_lo + (log_hi - log_lo) * t)
            w = math.sqrt(self.area * ar)
            h = self.area / w
            out.append((w, h))
        return out

    def shape_for_ar(self, ar: float) -> tuple[float, float]:
        ar = max(self.ar_lo, min(self.ar_hi, ar))
        w = math.sqrt(self.area * ar)
        h = self.area / w
        return (w, h)


@dataclass
class HardBlock(Block):
    """Fixed-dimension terminal. Optional fixed location/orientation."""

    w: float = 0.0
    h: float = 0.0
    fixed_x: Optional[float] = None
    fixed_y: Optional[float] = None
    fixed_orient: Optional[Orient] = None
    allowed_orients: tuple[Orient, ...] = (Orient.R0,)

    @property
    def is_fixed(self) -> bool:
        return self.fixed_x is not None and self.fixed_y is not None

    def shape_func(self) -> list[tuple[float, float]]:
        seen: set[tuple[float, float]] = set()
        out: list[tuple[float, float]] = []
        for o in self.allowed_orients:
            wh = (self.h, self.w) if o.swaps_wh() else (self.w, self.h)
            if wh not in seen:
                seen.add(wh)
                out.append(wh)
        return out


@dataclass
class ChildPlacement:
    """One child tile inside a StructuredBlock — position, orientation, and
    the tile block itself (which may be another StructuredBlock for nesting)."""

    name: str
    x: float
    y: float
    orient: "Orient"
    block: "Block"


@dataclass
class StructuredBlock(Block):
    """Deterministic grid of children. Used for arrays and bit-slices.

    `tile_w`/`tile_h` are the *outer* dimensions of each tile (after any inner
    expansion). `child_layout` is a list of ChildPlacement; nested grids are
    handled by setting the tile's `block` to another StructuredBlock.
    """

    rows: int = 1
    cols: int = 1
    tile_w: float = 0.0
    tile_h: float = 0.0
    pitch_x: float = 0.0  # 0 means abut tiles (use tile_w)
    pitch_y: float = 0.0
    child_layout: list = field(default_factory=list)

    @property
    def step_x(self) -> float:
        return self.pitch_x if self.pitch_x > 0 else self.tile_w

    @property
    def step_y(self) -> float:
        return self.pitch_y if self.pitch_y > 0 else self.tile_h

    @property
    def w(self) -> float:
        if self.cols == 0:
            return 0.0
        return self.step_x * (self.cols - 1) + self.tile_w

    @property
    def h(self) -> float:
        if self.rows == 0:
            return 0.0
        return self.step_y * (self.rows - 1) + self.tile_h

    def shape_func(self) -> list[tuple[float, float]]:
        return [(self.w, self.h)]


@dataclass
class HierarchicalBlock(Block):
    """A block whose footprint is derived from a recursive floorplan inside it.

    `inner_problem` is a FloorplanProblem (defined in vastu.hints.rewrite) — we
    use `object` here to avoid a circular import. The inner problem's blocks
    and nets get their own SP+SA solve; the resulting Pareto curve over
    aspect ratios is cached in `cached_shapes`.

    `chosen_shape_idx` records which shape from `cached_shapes` is currently
    selected by the parent SA; this is mutated by SA moves.
    """

    inner_problem: object = None
    cached_shapes: list[tuple[float, float]] = field(default_factory=list)
    chosen_shape_idx: int = 0
    ar_lo: float = 0.5
    ar_hi: float = 1.5

    def shape_func(self) -> list[tuple[float, float]]:
        if self.cached_shapes:
            return list(self.cached_shapes)
        return [(0.0, 0.0)]

    def current_shape(self) -> tuple[float, float]:
        if not self.cached_shapes:
            return (0.0, 0.0)
        idx = max(0, min(self.chosen_shape_idx, len(self.cached_shapes) - 1))
        return self.cached_shapes[idx]
