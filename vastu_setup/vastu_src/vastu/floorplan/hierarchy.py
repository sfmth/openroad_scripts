"""Hierarchical SP+SA solver.

Algorithm (Stockmeyer-style shape functions, sampled):
  1. Walk the floorplan tree bottom-up. For each HierarchicalBlock, recursively
     solve its inner problem at K aspect-ratio targets, producing a Pareto
     curve of (w, h) shapes the parent can pick from.
  2. Solve the current level. The SA sees each HierarchicalBlock as a
     discrete-shape block whose (w, h) it can switch via PICK_HIER_SHAPE.
  3. Walk top-down: each HierarchicalBlock's chosen shape is committed; we
     re-solve its inner problem with that outline as the target so the
     realized inner layout matches the parent's expectation.
  4. Compose absolute placements by summing offsets from outer to inner.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional

from vastu.core.block import (
    Block,
    ChildPlacement,
    HardBlock,
    HierarchicalBlock,
    Orient,
    Placement,
    SoftBlock,
    StructuredBlock,
)
from vastu.sa.annealer import AnnealConfig, AnnealResult, anneal
from vastu.sa.cost import CostWeights
from vastu.sa.moves import FloorplanState


def _block_area(blk: Block) -> float:
    if isinstance(blk, SoftBlock):
        return blk.area
    if isinstance(blk, HardBlock):
        return blk.w * blk.h
    if isinstance(blk, StructuredBlock):
        return blk.w * blk.h
    if isinstance(blk, HierarchicalBlock) and blk.cached_shapes:
        w, h = blk.cached_shapes[0]
        return w * h
    return 0.0


@dataclass
class HierarchicalResult:
    """Top-level result. `inner_results[name]` holds the inner solve for any
    HierarchicalBlock at this level (recursive)."""

    top_result: AnnealResult
    inner_results: dict[str, "HierarchicalResult"] = field(default_factory=dict)


def sample_shapes(
    problem,
    weights: CostWeights,
    ar_targets: tuple[float, ...],
    sa_config: AnnealConfig,
    *,
    slack: float = 1.15,
    sample_seed_offset: int = 100,
) -> list[tuple[float, float]]:
    """Run an inner solve at each AR target; collect Pareto-distinct (w,h)."""
    if not problem.blocks:
        return [(0.0, 0.0)]
    sum_area = sum(_block_area(b) for b in problem.blocks.values())
    if sum_area <= 0:
        sum_area = 1.0
    seen: set[tuple[float, float]] = set()
    shapes: list[tuple[float, float]] = []
    for i, ar in enumerate(ar_targets):
        target_w = math.sqrt(sum_area * slack * ar)
        target_h = sum_area * slack / target_w
        local_weights = CostWeights(
            area_weight=weights.area_weight,
            wirelength=weights.wirelength,
            outline_penalty=10.0,
            overlap_penalty=weights.overlap_penalty,
            target_w=target_w,
            target_h=target_h,
        )
        cfg = AnnealConfig(
            seed=(sa_config.seed or 0) + sample_seed_offset + i,
            cooling=sa_config.cooling,
            t_min=sa_config.t_min,
            warmup_moves=sa_config.warmup_moves,
            target_initial_accept=sa_config.target_initial_accept,
            max_temp_steps=sa_config.max_temp_steps,
            quiet_temps_to_stop=sa_config.quiet_temps_to_stop,
            moves_per_temp=sa_config.moves_per_temp,
        )
        res = solve_hierarchical(problem, local_weights, cfg, ar_targets=ar_targets)
        key = (round(res.top_result.total_w, 4), round(res.top_result.total_h, 4))
        if key not in seen:
            seen.add(key)
            shapes.append((res.top_result.total_w, res.top_result.total_h))
    # Pareto-monotone: sort by w ascending; drop dominated points.
    shapes.sort()
    pareto: list[tuple[float, float]] = []
    best_h = float("inf")
    for w, h in shapes:
        if h < best_h:
            pareto.append((w, h))
            best_h = h
    return pareto or shapes


def solve_hierarchical(
    problem,
    weights: CostWeights,
    sa_config: AnnealConfig,
    *,
    ar_targets: tuple[float, ...] = (0.6, 0.85, 1.0, 1.2, 1.5),
    target_w: float | None = None,
    target_h: float | None = None,
) -> HierarchicalResult:
    """Solve a (possibly nested) FloorplanProblem and return the recursive result.

    When target_w/target_h are provided, the top-level solve uses fixed-outline
    mode: outline penalty is enforced and area_weight is zeroed.
    """
    # 1. Bottom-up: sample shapes for every HierarchicalBlock at this level.
    effective_ar_targets = ar_targets
    if target_w is not None and target_h is not None and target_h > 0:
        die_ar = target_w / target_h
        if die_ar not in ar_targets:
            effective_ar_targets = tuple(sorted(set(ar_targets) | {round(die_ar, 3)}))

    for name, blk in problem.blocks.items():
        if isinstance(blk, HierarchicalBlock):
            blk.cached_shapes = sample_shapes(
                blk.inner_problem,
                weights,
                _restrict_to_block(effective_ar_targets, blk),
                sa_config,
            )
            blk.chosen_shape_idx = 0

    # 2. Solve at this level.
    # If a fixed outline is requested, override weights for the top-level solve.
    if target_w is not None and target_h is not None:
        top_weights = CostWeights(
            area_weight=0.0,
            wirelength=weights.wirelength,
            outline_penalty=weights.outline_penalty if weights.outline_penalty > 0 else 50.0,
            overlap_penalty=weights.overlap_penalty,
            target_w=target_w,
            target_h=target_h,
            fixed_outline=True,
        )
    else:
        top_weights = weights

    state = FloorplanState.initial(
        problem.blocks, random.Random(sa_config.seed)
    )
    top_res = anneal(state, problem.nets, top_weights, sa_config)

    # 3. Top-down realization. Each HierarchicalBlock gets re-solved at the
    #    outline that matches its chosen shape.
    inner_results: dict[str, HierarchicalResult] = {}
    for name, blk in problem.blocks.items():
        if isinstance(blk, HierarchicalBlock):
            chosen_idx = top_res.state.hier_shape_idx.get(name, blk.chosen_shape_idx)
            blk.chosen_shape_idx = chosen_idx
            tgt_w, tgt_h = blk.cached_shapes[chosen_idx]
            inner_weights = CostWeights(
                area_weight=weights.area_weight,
                wirelength=weights.wirelength,
                outline_penalty=20.0,
                overlap_penalty=weights.overlap_penalty,
                target_w=tgt_w,
                target_h=tgt_h,
            )
            cfg = AnnealConfig(
                seed=(sa_config.seed or 0) + 7919,
                cooling=sa_config.cooling,
                t_min=sa_config.t_min,
                warmup_moves=sa_config.warmup_moves,
                target_initial_accept=sa_config.target_initial_accept,
                max_temp_steps=sa_config.max_temp_steps,
                quiet_temps_to_stop=sa_config.quiet_temps_to_stop,
                moves_per_temp=sa_config.moves_per_temp,
            )
            inner_results[name] = solve_hierarchical(
                blk.inner_problem, inner_weights, cfg, ar_targets=ar_targets
            )

    return HierarchicalResult(top_result=top_res, inner_results=inner_results)


def _restrict_to_block(
    ar_targets: tuple[float, ...], blk: HierarchicalBlock
) -> tuple[float, ...]:
    return tuple(ar for ar in ar_targets if blk.ar_lo <= ar <= blk.ar_hi) or (1.0,)


def absolute_placements(
    problem,
    result: HierarchicalResult,
    *,
    x_off: float = 0.0,
    y_off: float = 0.0,
    path: str = "",
) -> dict[str, Placement]:
    """Walk the result tree and produce absolute (x,y,w,h) placements for every
    leaf block (HardBlock, SoftBlock, or StructuredBlock).

    Hierarchical blocks themselves are *not* emitted as leaves; their inner
    blocks are recursed into. Structured blocks emit their flat leaves via
    `vastu.structured.expand_leaves`.
    """
    from vastu.structured import expand_leaves

    out: dict[str, Placement] = {}
    for name, p in result.top_result.placements.items():
        full = f"{path}/{name}" if path else name
        blk = problem.blocks.get(name)
        if isinstance(blk, HierarchicalBlock):
            inner = result.inner_results.get(name)
            if inner is not None:
                out.update(
                    absolute_placements(
                        blk.inner_problem, inner,
                        x_off=p.x + x_off, y_off=p.y + y_off, path=full,
                    )
                )
            continue
        if isinstance(blk, StructuredBlock):
            for full_path, x, y, w, h, orient, leaf in expand_leaves(blk):
                composite = f"{full}/{full_path}" if full_path else full
                out[composite] = Placement(
                    x=p.x + x_off + x, y=p.y + y_off + y, w=w, h=h, orient=orient,
                )
            continue
        # Plain leaf: emit directly.
        out[full] = Placement(
            x=p.x + x_off, y=p.y + y_off, w=p.w, h=p.h, orient=p.orient,
        )
    return out
