from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable, Optional

from vastu.core.block import Placement
from vastu.core.hypergraph import Net
from vastu.sa.cost import CostBreakdown, CostWeights, cost as eval_cost
from vastu.sa.moves import FloorplanState, propose_move
from vastu.sp.packing import PackResult, pack


@dataclass
class AnnealConfig:
    """Cooling schedule & loop control."""

    moves_per_temp: int = 0  # 0 -> 20 * n_blocks
    cooling: float = 0.92
    t_min: float = 1e-4
    t_initial: float = 0.0  # 0 -> auto-calibrate via warmup
    warmup_moves: int = 200
    target_initial_accept: float = 0.85
    seed: Optional[int] = None
    max_temp_steps: int = 200
    # Stop after this many temps without best-cost improvement.
    quiet_temps_to_stop: int = 8


@dataclass
class AnnealResult:
    state: FloorplanState
    placements: dict[str, Placement]
    total_w: float
    total_h: float
    cost: CostBreakdown
    cost_trace: list[float] = field(default_factory=list)
    best_trace: list[float] = field(default_factory=list)
    accept_trace: list[float] = field(default_factory=list)
    temperatures: list[float] = field(default_factory=list)


def _eval(state: FloorplanState, nets: list[Net], weights: CostWeights):
    widths = {n: wh[0] for n, wh in state.shapes.items()}
    heights = {n: wh[1] for n, wh in state.shapes.items()}
    pr: PackResult = pack(state.sp, widths, heights, state.orients)
    # Merge fixed placements into the placement dict for HPWL evaluation.
    all_pl = dict(pr.placements)
    all_pl.update(state.fixed_placements)
    # Bounding box must account for fixed blocks too.
    total_w = pr.total_w
    total_h = pr.total_h
    for fp in state.fixed_placements.values():
        total_w = max(total_w, fp.x2)
        total_h = max(total_h, fp.y2)
    cb: CostBreakdown = eval_cost(
        total_w, total_h, all_pl, nets, weights,
        fixed_placements=state.fixed_placements,
    )
    # Repackage so caller can read total_w/total_h consistent with the cost.
    pr.total_w = total_w
    pr.total_h = total_h
    pr.placements = all_pl
    return pr, cb


def _outline_violation(total_w: float, total_h: float, weights: CostWeights) -> float:
    """How much the packing exceeds the target outline.

    Returns 0.0 if feasible (inside target_w x target_h, or fixed_outline is
    off). When fixed_outline=True, this is treated as a HARD constraint by
    the SA accept logic: moves that grow the violation from zero are rejected.
    """
    if not weights.fixed_outline:
        return 0.0
    v = 0.0
    if weights.target_w is not None and total_w > weights.target_w:
        v += total_w - weights.target_w
    if weights.target_h is not None and total_h > weights.target_h:
        v += total_h - weights.target_h
    return v


def _better_than(
    cand_viol: float, cand_total: float, ref_viol: float, ref_total: float
) -> bool:
    """Best-tracking comparator: feasibility first, then cost."""
    if cand_viol == 0.0 and ref_viol > 0.0:
        return True
    if cand_viol > 0.0 and ref_viol == 0.0:
        return False
    if cand_viol != ref_viol:
        return cand_viol < ref_viol
    return cand_total < ref_total


def anneal(
    initial: FloorplanState,
    nets: Iterable[Net],
    weights: CostWeights,
    config: AnnealConfig | None = None,
) -> AnnealResult:
    config = config or AnnealConfig()
    rng = random.Random(config.seed)
    nets_list = list(nets)
    n_blocks = initial.sp.n
    moves_per_temp = config.moves_per_temp or max(20 * n_blocks, 50)

    state = initial.copy()
    pr, cb = _eval(state, nets_list, weights)
    viol = _outline_violation(pr.total_w, pr.total_h, weights)
    best_state = state.copy()
    best_cb = cb
    best_viol = viol

    # --- Warmup: compute T0 from observed |Δcost| at fully-random walk. ---
    # Warmup performs an unconstrained random walk so the temperature scale
    # reflects the natural cost noise. The hard-outline accept rule kicks in
    # only in the main loop below.
    if config.t_initial > 0:
        T = config.t_initial
    else:
        deltas: list[float] = []
        cur_state = state.copy()
        cur_cb = cb
        for _ in range(config.warmup_moves):
            cand = cur_state.copy()
            move = propose_move(cur_state, rng)
            move(cand)
            cand_pr, cand_cb = _eval(cand, nets_list, weights)
            cand_viol = _outline_violation(cand_pr.total_w, cand_pr.total_h, weights)
            d = cand_cb.total - cur_cb.total
            deltas.append(d)
            cur_state = cand
            cur_cb = cand_cb
            if _better_than(cand_viol, cand_cb.total, best_viol, best_cb.total):
                best_state = cand.copy()
                best_cb = cand_cb
                best_viol = cand_viol
        positives = [d for d in deltas if d > 0]
        if positives:
            mean_pos = sum(positives) / len(positives)
            T = -mean_pos / math.log(config.target_initial_accept)
        else:
            T = 1.0

    cost_trace: list[float] = []
    best_trace: list[float] = []
    accept_trace: list[float] = []
    temperatures: list[float] = []
    quiet_count = 0
    last_best = best_cb.total

    for step in range(config.max_temp_steps):
        accepted = 0
        for _ in range(moves_per_temp):
            cand = state.copy()
            move = propose_move(state, rng)
            move(cand)
            cand_pr, cand_cb = _eval(cand, nets_list, weights)
            cand_viol = _outline_violation(cand_pr.total_w, cand_pr.total_h, weights)
            delta = cand_cb.total - cb.total

            # Feasibility-first accept rule. In fixed_outline mode, target_w x
            # target_h is a HARD constraint: never leave feasibility, always
            # enter it, and when both infeasible favor smaller violation.
            if viol == 0.0 and cand_viol > 0.0:
                accept = False
            elif viol > 0.0 and cand_viol == 0.0:
                accept = True
            elif viol > 0.0 and cand_viol > 0.0:
                if cand_viol < viol:
                    accept = True
                elif cand_viol > viol:
                    accept = False
                else:
                    accept = delta <= 0 or rng.random() < math.exp(-delta / T)
            else:
                accept = delta <= 0 or rng.random() < math.exp(-delta / T)

            if accept:
                state = cand
                cb = cand_cb
                viol = cand_viol
                accepted += 1
                if _better_than(viol, cb.total, best_viol, best_cb.total):
                    best_state = state.copy()
                    best_cb = cb
                    best_viol = viol
        cost_trace.append(cb.total)
        best_trace.append(best_cb.total)
        accept_trace.append(accepted / moves_per_temp)
        temperatures.append(T)
        if best_cb.total < last_best * 0.995:
            quiet_count = 0
            last_best = best_cb.total
        else:
            quiet_count += 1
        if T < config.t_min or quiet_count >= config.quiet_temps_to_stop:
            break
        T *= config.cooling

    final_pr, final_cb = _eval(best_state, nets_list, weights)
    return AnnealResult(
        state=best_state,
        placements=final_pr.placements,
        total_w=final_pr.total_w,
        total_h=final_pr.total_h,
        cost=final_cb,
        cost_trace=cost_trace,
        best_trace=best_trace,
        accept_trace=accept_trace,
        temperatures=temperatures,
    )
