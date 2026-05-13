from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from vastu.core.block import (
    Block,
    HardBlock,
    HierarchicalBlock,
    Orient,
    SoftBlock,
    StructuredBlock,
)
from vastu.sp.sequence_pair import SequencePair


@dataclass
class FloorplanState:
    """All mutable state the SA operates on for a single SP problem.

    Fixed hard blocks (HardBlock with fixed_x/y set) are NOT in `sp`; instead
    they appear in `fixed_placements` and act as obstacles that the SP-packed
    blocks must avoid (penalty term in cost).
    """

    blocks: dict[str, Block]
    sp: SequencePair
    shapes: dict[str, tuple[float, float]] = field(default_factory=dict)
    orients: dict[str, Orient] = field(default_factory=dict)
    fixed_placements: dict[str, "Placement"] = field(default_factory=dict)
    hier_shape_idx: dict[str, int] = field(default_factory=dict)

    @classmethod
    def initial(
        cls,
        blocks: dict[str, Block],
        rng: random.Random | None = None,
    ) -> "FloorplanState":
        from vastu.core.block import Placement
        rng = rng or random.Random()
        free_names: list[str] = []
        fixed_placements: dict[str, Placement] = {}
        for name, blk in blocks.items():
            if isinstance(blk, HardBlock) and blk.is_fixed:
                orient = blk.fixed_orient or blk.allowed_orients[0]
                w, h = (blk.h, blk.w) if orient.swaps_wh() else (blk.w, blk.h)
                fixed_placements[name] = Placement(
                    x=blk.fixed_x, y=blk.fixed_y, w=w, h=h, orient=orient
                )
            else:
                free_names.append(name)
        sp = SequencePair.random(free_names, rng) if free_names else SequencePair([], [])
        state = cls(blocks=blocks, sp=sp, fixed_placements=fixed_placements)
        for name in free_names:
            blk = blocks[name]
            if isinstance(blk, HardBlock):
                orient = blk.allowed_orients[0]
                state.orients[name] = orient
                state.shapes[name] = (blk.h, blk.w) if orient.swaps_wh() else (blk.w, blk.h)
            elif isinstance(blk, SoftBlock):
                state.shapes[name] = blk.shape_for_ar(1.0)
            elif isinstance(blk, StructuredBlock):
                state.shapes[name] = (blk.w, blk.h)
            elif isinstance(blk, HierarchicalBlock):
                state.hier_shape_idx[name] = blk.chosen_shape_idx
                state.shapes[name] = blk.current_shape()
            else:
                shapes = blk.shape_func()
                state.shapes[name] = shapes[0] if shapes else (0.0, 0.0)
        return state

    def copy(self) -> "FloorplanState":
        return FloorplanState(
            blocks=self.blocks,
            sp=self.sp.copy(),
            shapes=dict(self.shapes),
            orients=dict(self.orients),
            fixed_placements=dict(self.fixed_placements),
            hier_shape_idx=dict(self.hier_shape_idx),
        )


class MoveKind(Enum):
    SWAP_PLUS = "swap_plus"
    SWAP_MINUS = "swap_minus"
    SWAP_BOTH = "swap_both"
    ROTATE_HARD = "rotate_hard"
    PERTURB_SOFT_AR = "perturb_soft_ar"
    PICK_HIER_SHAPE = "pick_hier_shape"


def _swap(seq: list[str], i: int, j: int) -> None:
    seq[i], seq[j] = seq[j], seq[i]


def _two_distinct(rng: random.Random, n: int) -> tuple[int, int]:
    if n < 2:
        return (0, 0)
    i = rng.randrange(n)
    j = rng.randrange(n - 1)
    if j >= i:
        j += 1
    return (i, j)


def propose_move(
    state: FloorplanState,
    rng: random.Random,
    weights: Optional[dict[MoveKind, float]] = None,
) -> Callable[[FloorplanState], None]:
    """Return a function that mutates a given state in place to apply the move.

    The state passed to the returned function should be a copy of the source
    (the SA driver makes the copy so it can revert on rejection).
    """
    n = state.sp.n
    hard_rotatable = [
        name
        for name, blk in state.blocks.items()
        if isinstance(blk, HardBlock)
        and not blk.is_fixed
        and len(blk.allowed_orients) > 1
    ]
    soft_names = [
        name for name, blk in state.blocks.items() if isinstance(blk, SoftBlock)
    ]
    hier_names = [
        name
        for name, blk in state.blocks.items()
        if isinstance(blk, HierarchicalBlock) and len(blk.cached_shapes) > 1
    ]

    base_weights = {
        MoveKind.SWAP_PLUS: 1.0,
        MoveKind.SWAP_MINUS: 1.0,
        MoveKind.SWAP_BOTH: 0.5,
        MoveKind.ROTATE_HARD: 0.5 if hard_rotatable else 0.0,
        MoveKind.PERTURB_SOFT_AR: 1.0 if soft_names else 0.0,
        MoveKind.PICK_HIER_SHAPE: 0.7 if hier_names else 0.0,
    }
    if weights:
        base_weights.update(weights)

    kinds = list(base_weights.keys())
    w = [base_weights[k] for k in kinds]
    total = sum(w)
    if total <= 0:
        # Fall back to swap_plus
        kinds, w, total = [MoveKind.SWAP_PLUS], [1.0], 1.0

    pick = rng.random() * total
    acc = 0.0
    chosen = kinds[0]
    for k, wi in zip(kinds, w):
        acc += wi
        if pick <= acc:
            chosen = k
            break

    if chosen is MoveKind.SWAP_PLUS:
        i, j = _two_distinct(rng, n)
        return lambda s: _swap(s.sp.plus, i, j)
    if chosen is MoveKind.SWAP_MINUS:
        i, j = _two_distinct(rng, n)
        return lambda s: _swap(s.sp.minus, i, j)
    if chosen is MoveKind.SWAP_BOTH:
        # Swap a *block pair* in both sequences (not the same indices).
        names = state.sp.plus
        a_i, b_i = _two_distinct(rng, n)
        a, b = names[a_i], names[b_i]

        def apply_both(s: FloorplanState) -> None:
            ip_a, ip_b = s.sp.plus.index(a), s.sp.plus.index(b)
            im_a, im_b = s.sp.minus.index(a), s.sp.minus.index(b)
            _swap(s.sp.plus, ip_a, ip_b)
            _swap(s.sp.minus, im_a, im_b)

        return apply_both
    if chosen is MoveKind.ROTATE_HARD:
        target = rng.choice(hard_rotatable)
        blk = state.blocks[target]
        assert isinstance(blk, HardBlock)
        orients = list(blk.allowed_orients)
        cur = state.orients.get(target, orients[0])
        nxt = orients[(orients.index(cur) + 1) % len(orients)]

        def apply_rot(s: FloorplanState) -> None:
            s.orients[target] = nxt
            if nxt.swaps_wh() != cur.swaps_wh():
                w0, h0 = s.shapes[target]
                s.shapes[target] = (h0, w0)

        return apply_rot
    if chosen is MoveKind.PERTURB_SOFT_AR:
        target = rng.choice(soft_names)
        blk = state.blocks[target]
        assert isinstance(blk, SoftBlock)
        # Choose a fresh AR in log-space within bounds, biased toward current.
        w0, h0 = state.shapes[target]
        cur_ar = w0 / h0 if h0 > 0 else 1.0
        # Geometric jitter: log-AR += N(0, 0.3).
        new_log = math.log(cur_ar) + rng.gauss(0.0, 0.3)
        new_ar = math.exp(new_log)
        new_ar = max(blk.ar_lo, min(blk.ar_hi, new_ar))

        def apply_ar(s: FloorplanState) -> None:
            s.shapes[target] = blk.shape_for_ar(new_ar)

        return apply_ar
    if chosen is MoveKind.PICK_HIER_SHAPE:
        target = rng.choice(hier_names)
        blk = state.blocks[target]
        assert isinstance(blk, HierarchicalBlock)
        n_shapes = len(blk.cached_shapes)
        cur_idx = blk.chosen_shape_idx
        new_idx = rng.randrange(n_shapes - 1)
        if new_idx >= cur_idx:
            new_idx += 1

        def apply_pick(s: FloorplanState) -> None:
            s.hier_shape_idx[target] = new_idx
            s.shapes[target] = blk.cached_shapes[new_idx]

        return apply_pick
    raise RuntimeError(f"unhandled move kind {chosen}")
