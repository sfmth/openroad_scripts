"""vastu CLI: floorplan a Verilog design with hints, or from RTLMP cluster JSON.

Usage:
    vastu floorplan --verilog top.v [--verilog more.v] \
                    --hints hints.yaml \
                    [--lef tech.lef] \
                    [--top top_module] \
                    --out floorplan.def \
                    [--plot floorplan.png] \
                    [--seed N]

    vastu floorplan-clusters \
                    --clusters cluster_tree.json \
                    --out placement.tcl \
                    [--plot floorplan.png] \
                    [--seed N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vastu.core import HardBlock
from vastu.floorplan import absolute_placements, solve_hierarchical
from vastu.hints import build_floorplan, flatten_to_leaves, load_hints
from vastu.io.def_writer import write_def
from vastu.io.lef import parse_lef
from vastu.io.verilog import parse_verilog
from vastu.sa import AnnealConfig, CostWeights


def _resolve_macro(path: str, problem) -> str:
    """Walk path components into the problem's nested structure and return
    the original Verilog module name of the leaf at `path`.
    Falls back to the leaf instance name if the module is unknown."""
    parts = path.split("/")
    cur = problem
    cur_block = None
    for i, part in enumerate(parts):
        cur_block = cur.blocks.get(part) if hasattr(cur, "blocks") else None
        if cur_block is None:
            break
        if hasattr(cur_block, "inner_problem") and cur_block.inner_problem is not None:
            cur = cur_block.inner_problem
            continue
        if hasattr(cur_block, "child_layout") and cur_block.child_layout:
            remaining = parts[i + 1 :]
            if not remaining:
                return cur_block.module_name or cur_block.name
            sub = cur_block
            for r in remaining:
                next_sub = None
                for cp in sub.child_layout:
                    if cp.name == r:
                        next_sub = cp.block
                        break
                if next_sub is None:
                    return r
                sub = next_sub
            return getattr(sub, "module_name", "") or sub.name
        return cur_block.module_name or cur_block.name
    return cur_block.module_name if cur_block else parts[-1]


def _flatten_for_def(problem, result):
    """Yield (instance_name, macro_name, Placement) tuples for DEF COMPONENTS."""
    abs_pl = absolute_placements(problem, result)
    for path, pl in abs_pl.items():
        macro = _resolve_macro(path, problem)
        # DEF instance names cannot contain '/' in some tools; replace with '_'.
        inst_name = path.replace("/", "_")
        yield inst_name, macro, pl


def _floorplan(args: argparse.Namespace) -> int:
    sources = [Path(v) for v in args.verilog]
    nl = parse_verilog(sources, top=args.top)
    hints = load_hints(args.hints) if args.hints else None
    if hints is None:
        print("warning: no hints file provided; using defaults", file=sys.stderr)
        from vastu.hints.schema import Hints
        hints = Hints()
    if args.lef:
        macros = parse_lef(args.lef)
        # If a macro in LEF is not yet in hints, auto-register it as hard_macro.
        from vastu.hints.schema import HardMacroHint
        for mname, mac in macros.items():
            if mname not in hints.hard_macro:
                hints.hard_macro[mname] = HardMacroHint(w=mac.w, h=mac.h)
    if args.mode == "flat":
        fp = flatten_to_leaves(nl, hints, top=args.top)
    else:
        fp = build_floorplan(nl, hints)
    weights = CostWeights(
        area_weight=1.0,
        wirelength=args.wirelength_weight,
    )

    # Fixed-outline support
    target_w = getattr(args, "die_width", None)
    target_h = getattr(args, "die_height", None)
    if target_w is not None and target_h is not None:
        weights = CostWeights(
            area_weight=0.0,
            wirelength=args.wirelength_weight,
            outline_penalty=getattr(args, "outline_penalty", 50.0),
            target_w=target_w,
            target_h=target_h,
            fixed_outline=True,
        )

    cfg = AnnealConfig(
        seed=args.seed,
        max_temp_steps=args.max_temp_steps,
        moves_per_temp=args.moves_per_temp,
    )
    result = solve_hierarchical(fp, weights, cfg,
                                target_w=target_w, target_h=target_h)

    components = list(_flatten_for_def(fp, result))
    abs_pl = absolute_placements(fp, result)
    die_w = max(p.x + p.w for p in abs_pl.values()) if abs_pl else 0.0
    die_h = max(p.y + p.h for p in abs_pl.values()) if abs_pl else 0.0

    write_def(
        args.out,
        design_name=fp.top_module,
        components=components,
        die_w=die_w,
        die_h=die_h,
    )
    print(
        f"wrote {args.out} ({len(components)} components, die {die_w:.1f}×{die_h:.1f})"
    )

    if args.plot:
        from vastu.viz.plot import plot_floorplan
        flat_blocks = {n: HardBlock(name=n, w=p.w, h=p.h) for n, p in abs_pl.items()}
        # Top-level placements (in absolute coords; same coord system at top).
        outer_pl = dict(result.top_result.placements)
        plot_floorplan(
            flat_blocks, abs_pl,
            nets=fp.nets,
            outer_blocks=fp.blocks,
            outer_placements=outer_pl,
            out_path=args.plot,
            title=f"vastu: {fp.top_module}",
            show_nets=args.show_nets,
            show_pins=args.show_pins,
            figsize=(args.plot_size, args.plot_size),
        )
        print(f"wrote {args.plot}")
    return 0


def _floorplan_clusters(args: argparse.Namespace) -> int:
    """Floorplan from an RTLMP cluster tree JSON."""
    from vastu.io.cluster_import import load_cluster_tree
    from vastu.io.tcl_writer import write_placement_tcl

    problem, die_w, die_h, raw_data = load_cluster_tree(
        args.clusters,
        target_utilization=args.target_utilization,
    )

    # Override die dimensions if provided
    if args.die_width is not None:
        die_w = args.die_width
    if args.die_height is not None:
        die_h = args.die_height

    weights = CostWeights(
        area_weight=0.0,
        wirelength=args.wirelength_weight,
        outline_penalty=args.outline_penalty,
        overlap_penalty=10.0,
        target_w=die_w,
        target_h=die_h,
        fixed_outline=True,
    )

    cfg = AnnealConfig(
        seed=args.seed,
        max_temp_steps=args.max_temp_steps,
        moves_per_temp=args.moves_per_temp,
    )

    result = solve_hierarchical(
        problem, weights, cfg,
        target_w=die_w, target_h=die_h,
    )

    abs_pl = absolute_placements(problem, result)
    actual_w = max((p.x + p.w for p in abs_pl.values()), default=0.0)
    actual_h = max((p.y + p.h for p in abs_pl.values()), default=0.0)
    print(f"vastu: solved {len(abs_pl)} blocks, "
          f"outline {actual_w:.1f}×{actual_h:.1f} "
          f"(target {die_w:.1f}×{die_h:.1f})")

    dbu = raw_data.get("dbu_per_micron", 1000)
    write_placement_tcl(problem, result, raw_data, args.out, dbu_per_micron=dbu)
    print(f"wrote {args.out}")

    if args.plot:
        from vastu.viz.plot import plot_floorplan
        flat_blocks = {n: HardBlock(name=n, w=p.w, h=p.h) for n, p in abs_pl.items()}
        outer_pl = dict(result.top_result.placements)
        plot_floorplan(
            flat_blocks, abs_pl,
            nets=problem.nets,
            outer_blocks=problem.blocks,
            outer_placements=outer_pl,
            out_path=args.plot,
            title=f"vastu: {problem.top_module}",
            show_nets=True,
            show_pins=False,
            figsize=(args.plot_size, args.plot_size),
        )
        print(f"wrote {args.plot}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vastu")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # --- floorplan subcommand (existing, extended with --die-width/--die-height) ---
    fp = sub.add_parser("floorplan", help="produce a DEF from Verilog + hints")
    fp.add_argument("--verilog", action="append", required=True, help="input .v file (repeatable)")
    fp.add_argument("--hints", help="YAML hint file")
    fp.add_argument("--lef", help="LEF file (for hard-macro dimensions)")
    fp.add_argument("--top", help="top module name (auto-detected if unique)")
    fp.add_argument("--out", required=True, help="output DEF path")
    fp.add_argument("--plot", help="optional PNG plot path")
    fp.add_argument("--seed", type=int, default=0)
    fp.add_argument("--wirelength-weight", type=float, default=2.0)
    fp.add_argument("--mode", choices=("hierarchical", "flat"),
                    default="hierarchical",
                    help="hierarchical (default): respect Verilog hierarchy + recurse; "
                         "flat: collapse all to leaves and run a single SP+SA")
    fp.add_argument("--max-temp-steps", type=int, default=80)
    fp.add_argument("--moves-per-temp", type=int, default=400)
    fp.add_argument("--show-nets", action="store_true",
                    help="overlay net star-topology lines on the plot")
    fp.add_argument("--show-pins", action="store_true",
                    help="draw pin markers on each block")
    fp.add_argument("--plot-size", type=float, default=10.0,
                    help="plot figure size (inches per side)")
    fp.add_argument("--die-width", type=float, default=None,
                    help="fixed die width (microns); enables fixed-outline mode")
    fp.add_argument("--die-height", type=float, default=None,
                    help="fixed die height (microns); enables fixed-outline mode")
    fp.add_argument("--outline-penalty", type=float, default=50.0,
                    help="outline penalty weight for fixed-outline mode")
    fp.set_defaults(func=_floorplan)

    # --- floorplan-clusters subcommand (new) ---
    fc = sub.add_parser("floorplan-clusters",
                        help="floorplan from RTLMP cluster tree JSON")
    fc.add_argument("--clusters", required=True,
                    help="cluster_tree.json from RTLMP dump_cluster_tree")
    fc.add_argument("--out", required=True,
                    help="output placement.tcl for OpenROAD")
    fc.add_argument("--plot", help="optional PNG plot path")
    fc.add_argument("--seed", type=int, default=0)
    fc.add_argument("--die-width", type=float, default=None,
                    help="override die width from JSON (microns)")
    fc.add_argument("--die-height", type=float, default=None,
                    help="override die height from JSON (microns)")
    fc.add_argument("--wirelength-weight", type=float, default=5.0)
    fc.add_argument("--outline-penalty", type=float, default=50.0)
    fc.add_argument("--target-utilization", type=float, default=0.7,
                    help="utilization for inflating std-cell cluster area")
    fc.add_argument("--max-temp-steps", type=int, default=100)
    fc.add_argument("--moves-per-temp", type=int, default=600)
    fc.add_argument("--plot-size", type=float, default=10.0,
                    help="plot figure size (inches per side)")
    fc.set_defaults(func=_floorplan_clusters)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
