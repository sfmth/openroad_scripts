from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from vastu.core.block import Placement
from vastu.core.hypergraph import Net, hpwl


@dataclass
class CostWeights:
    """Tuning knobs for the SA cost function. All terms summed.

        area_weight     * total_w * total_h
        wirelength      * HPWL
        outline_penalty * (max(0, W-Wt)^2 + max(0, H-Ht)^2)
        overlap_penalty * sum of overlap areas with fixed blocks

    When `fixed_outline` is True, the outline penalty uses a steeper
    cubic term and the area weight should be set to 0 by the caller
    (the die size is fixed, so minimizing packing area is not meaningful).
    """

    area_weight: float = 1.0
    wirelength: float = 0.0
    outline_penalty: float = 0.0
    overlap_penalty: float = 0.0
    target_w: Optional[float] = None
    target_h: Optional[float] = None
    fixed_outline: bool = False


@dataclass
class CostBreakdown:
    area: float
    hpwl: float
    outline: float
    overlap: float
    total: float


def _overlap_area(p1: Placement, p2: Placement) -> float:
    dx = max(0.0, min(p1.x2, p2.x2) - max(p1.x, p2.x))
    dy = max(0.0, min(p1.y2, p2.y2) - max(p1.y, p2.y))
    return dx * dy


def cost(
    total_w: float,
    total_h: float,
    placements: dict[str, Placement],
    nets: Iterable[Net],
    weights: CostWeights,
    fixed_placements: dict[str, Placement] | None = None,
) -> CostBreakdown:
    area = total_w * total_h
    wl = hpwl(nets, placements) if weights.wirelength != 0.0 else 0.0
    outline = 0.0
    if weights.outline_penalty != 0.0:
        if weights.target_w is not None and total_w > weights.target_w:
            ow = total_w - weights.target_w
            outline += ow ** 3 if weights.fixed_outline else ow ** 2
        if weights.target_h is not None and total_h > weights.target_h:
            oh = total_h - weights.target_h
            outline += oh ** 3 if weights.fixed_outline else oh ** 2
    overlap = 0.0
    if fixed_placements and weights.overlap_penalty != 0.0:
        for fname, fp in fixed_placements.items():
            for name, p in placements.items():
                if name == fname:
                    continue
                overlap += _overlap_area(fp, p)
    total = (
        weights.area_weight * area
        + weights.wirelength * wl
        + weights.outline_penalty * outline
        + weights.overlap_penalty * overlap
    )
    return CostBreakdown(area=area, hpwl=wl, outline=outline, overlap=overlap, total=total)
