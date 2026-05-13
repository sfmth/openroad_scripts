from __future__ import annotations

from dataclasses import dataclass

from vastu.core.block import Placement, Orient
from vastu.sp.sequence_pair import SequencePair


@dataclass
class PackResult:
    placements: dict[str, Placement]
    total_w: float
    total_h: float


def _lcs_positions(
    plus: list[str],
    minus: list[str],
    weight: dict[str, float],
) -> tuple[dict[str, float], float]:
    """Compute bottom-left coordinate for each block on one axis via LCS DP.

    For x-coords: pass plus, minus, widths. For y-coords: pass plus, reversed
    minus, heights. Returns (origin, total_extent) where origin[b] is the
    position of b's bottom-left corner on this axis.

    Algorithm (Tang/Otten O(n^2)):
        idx_in_minus[b] = position of b in `minus`
        L[k] = current max accumulated length over the first k slots in minus
        For each b in plus order:
            j = idx_in_minus[b]
            best = max(L[0..j-1])  with L[-1] = 0
            origin[b] = best
            L[j] = best + weight[b]
            (no need to propagate further; later max queries take care of it)
    """
    idx_in_minus = {name: i for i, name in enumerate(minus)}
    n = len(plus)
    L = [0.0] * n
    origin: dict[str, float] = {}
    for b in plus:
        j = idx_in_minus[b]
        best = 0.0
        for k in range(j):
            if L[k] > best:
                best = L[k]
        origin[b] = best
        new_val = best + weight[b]
        if new_val > L[j]:
            L[j] = new_val
    total = max(L) if L else 0.0
    return origin, total


def pack(
    sp: SequencePair,
    widths: dict[str, float],
    heights: dict[str, float],
    orients: dict[str, Orient] | None = None,
) -> PackResult:
    """Compute placements for an SP given per-block width/height/orient.

    Width/height are the *outer* dimensions of each block already accounting
    for orientation (caller swaps wh for rotated hard blocks).
    """
    orients = orients or {}
    x_origin, total_w = _lcs_positions(sp.plus, sp.minus, widths)
    # Y axis uses (plus, reverse(minus)) with heights
    y_origin, total_h = _lcs_positions(sp.plus, list(reversed(sp.minus)), heights)
    placements: dict[str, Placement] = {}
    for b in sp.plus:
        placements[b] = Placement(
            x=x_origin[b],
            y=y_origin[b],
            w=widths[b],
            h=heights[b],
            orient=orients.get(b, Orient.R0),
        )
    return PackResult(placements=placements, total_w=total_w, total_h=total_h)
