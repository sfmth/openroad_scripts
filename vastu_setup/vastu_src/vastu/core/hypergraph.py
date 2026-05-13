from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from vastu.core.block import Orient, Placement, transform_offset


@dataclass(frozen=True)
class Pin:
    """A pin belongs to a block.

    Two coordinate models, picked by `side`:
      - side is None: (dx, dy) is the literal offset in unrotated block coords.
        Use this for hard macros where pin coords come from LEF.
      - side in {"W","E","N","S","C"}: pin lives on that edge (or center) at
        a fractional position along it given by `frac` in [0,1]. Use this for
        soft blocks whose dimensions are determined by SA — the absolute
        offset is recomputed each time `pin_position` runs.
    """

    block: str
    name: str
    dx: float = 0.0
    dy: float = 0.0
    side: str | None = None
    frac: float = 0.5


@dataclass
class Net:
    """Hyperedge over pins. Weight scales HPWL contribution."""

    name: str
    pins: list[Pin] = field(default_factory=list)
    weight: float = 1.0
    fanout_cap: int = 0  # 0 = no cap; otherwise clip to bbox of `cap` random pins


@dataclass
class Hypergraph:
    nets: list[Net] = field(default_factory=list)

    def add(self, net: Net) -> None:
        self.nets.append(net)


def pin_position(
    pin: Pin, placement: Placement
) -> tuple[float, float]:
    if pin.side is None:
        dx, dy = pin.dx, pin.dy
    else:
        w, h = placement.w, placement.h
        f = max(0.0, min(1.0, pin.frac))
        if pin.side == "W":
            dx, dy = 0.0, f * h
        elif pin.side == "E":
            dx, dy = w, f * h
        elif pin.side == "S":
            dx, dy = f * w, 0.0
        elif pin.side == "N":
            dx, dy = f * w, h
        elif pin.side == "C":
            dx, dy = w / 2, h / 2
        else:
            raise ValueError(f"unknown pin side {pin.side!r}")
    dx, dy = transform_offset(dx, dy, placement.w, placement.h, placement.orient)
    return (placement.x + dx, placement.y + dy)


def hpwl(
    nets: Iterable[Net],
    placements: dict[str, Placement],
) -> float:
    """Half-perimeter wirelength over all nets.

    Pins on blocks not in `placements` are skipped (useful when a soft block has
    not yet been realized at the parent level).
    """
    total = 0.0
    for net in nets:
        xs: list[float] = []
        ys: list[float] = []
        for pin in net.pins:
            pl = placements.get(pin.block)
            if pl is None:
                continue
            x, y = pin_position(pin, pl)
            xs.append(x)
            ys.append(y)
        if len(xs) < 2:
            continue
        if net.fanout_cap > 0 and len(xs) > net.fanout_cap:
            # Use only the extreme pins on each axis — this caps the
            # HPWL of very high-fanout nets so they don't dominate.
            xs.sort()
            ys.sort()
            xs = xs[: net.fanout_cap // 2] + xs[-net.fanout_cap // 2 :]
            ys = ys[: net.fanout_cap // 2] + ys[-net.fanout_cap // 2 :]
        total += net.weight * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
    return total
