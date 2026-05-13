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
    best_state = state.copy()
    best_cb = cb

    # --- Warmup: compute T0 from observed |Δcost| at fully-random walk. ---
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
            _, cand_cb = _eval(cand, nets_list, weights)
            d = cand_cb.total - cur_cb.total
            deltas.append(d)
            cur_state = cand
            cur_cb = cand_cb
            if cand_cb.total < best_cb.total:
                best_state = cand.copy()
                best_cb = cand_cb
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
            _, cand_cb = _eval(cand, nets_list, weights)
            delta = cand_cb.total - cb.total
            if delta <= 0 or rng.random() < math.exp(-delta / T):
                state = cand
                cb = cand_cb
                accepted += 1
                if cb.total < best_cb.total:
                    best_state = state.copy()
                    best_cb = cb
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
