"""Tree-rewrite from Verilog netlist + hints into floorplan IR.

Milestone 2 scope: build a *single-level* set of blocks and nets for the
top module. Each direct child instance becomes a Block; a child whose module
appears in `hard_macro` becomes a HardBlock, in `opaque` becomes a SoftBlock.
A child whose module is recursive (has its own sub-instances) and is not
hinted is an error for now — hierarchical recursion lands in milestone 6.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import math

from vastu.core.block import (
    Block,
    ChildPlacement,
    HardBlock,
    HierarchicalBlock,
    Orient,
    SoftBlock,
    StructuredBlock,
)
from vastu.core.hypergraph import Net, Pin
from vastu.core.netlist import Module, Netlist, PortDir
from vastu.hints.schema import Hints, StructuredHint
from vastu.structured.grid import TilePlacement, array_layout


@dataclass
class FloorplanProblem:
    """Single-level problem ready for SP+SA: blocks, nets, and metadata."""

    blocks: dict[str, Block] = field(default_factory=dict)
    nets: list[Net] = field(default_factory=list)
    top_module: str = ""
    # Map from instance name back to its Verilog module name, for traceability.
    instance_modules: dict[str, str] = field(default_factory=dict)


_DIR_TO_SIDE = {
    PortDir.INPUT: "W",
    PortDir.OUTPUT: "E",
    PortDir.INOUT: "N",
}


def build_floorplan(netlist: Netlist, hints: Hints) -> FloorplanProblem:
    """Build the top-level floorplan problem from a Verilog netlist + hints.

    Recursive non-hinted modules become HierarchicalBlocks whose `inner_problem`
    is itself a FloorplanProblem; the hierarchical solver runs SA at each level.
    """
    top_name = hints.top or netlist.top
    if top_name not in netlist.modules:
        raise KeyError(f"top module {top_name!r} not in netlist")
    return _build_level(netlist, hints, top_name)


def _build_level(netlist: Netlist, hints: Hints, mod_name: str) -> FloorplanProblem:
    module = netlist.modules[mod_name]
    problem = FloorplanProblem(top_module=mod_name)
    nets_by_name: dict[str, Net] = {}

    for inst in module.instances:
        block = _instance_to_block(inst, netlist, hints)
        problem.blocks[inst.name] = block
        problem.instance_modules[inst.name] = inst.module
        child_module = netlist.modules.get(inst.module)
        port_dir = {p.name: p.direction for p in child_module.ports} if child_module else {}
        for port_name, net_name in inst.connections.items():
            if net_name.startswith("<const") or net_name.startswith("<expr"):
                continue
            d = port_dir.get(port_name, PortDir.INPUT)
            side = _DIR_TO_SIDE[d]
            frac = _stable_frac(port_name)
            pin = Pin(block=inst.name, name=port_name, side=side, frac=frac)
            block.pins.append(pin)
            net = nets_by_name.setdefault(net_name, Net(name=net_name))
            net.pins.append(pin)

    problem.nets = [
        n for n in nets_by_name.values() if len({p.block for p in n.pins}) >= 2
    ]
    return problem


def _stable_frac(name: str) -> float:
    # Deterministic spread of pins along a side. Use a simple hash modulo a
    # fixed grid (avoid Python's hash randomization).
    h = 2166136261
    for ch in name:
        h = (h ^ ord(ch)) * 16777619 & 0xFFFFFFFF
    return 0.1 + 0.8 * ((h % 9) / 8.0)


def _instance_to_block(inst, netlist: Netlist, hints: Hints) -> Block:
    mod_name = inst.module
    if mod_name in hints.structured:
        blk = _structured_block(inst.name, mod_name, netlist, hints)
        blk.module_name = mod_name
        return blk
    if mod_name in hints.hard_macro:
        spec = hints.hard_macro[mod_name]
        return HardBlock(
            name=inst.name,
            w=spec.w,
            h=spec.h,
            allowed_orients=spec.allowed_orients,
            module_name=mod_name,
        )
    if mod_name in hints.opaque:
        spec = hints.opaque[mod_name]
        ar = hints.aspect_ratio.get(mod_name)
        ar_lo = ar.lo if ar else spec.ar_lo
        ar_hi = ar.hi if ar else spec.ar_hi
        return SoftBlock(
            name=inst.name, area=spec.area, ar_lo=ar_lo, ar_hi=ar_hi,
            module_name=mod_name,
        )

    if netlist.is_leaf(mod_name):
        raise ValueError(
            f"module {mod_name!r} is a leaf with no `opaque` or `hard_macro` hint; "
            f"add one to specify area or LEF dimensions"
        )
    # Recursive non-hinted module: build a HierarchicalBlock with an inner
    # FloorplanProblem that the hierarchical solver will process.
    inner = _build_level(netlist, hints, mod_name)
    ar = hints.aspect_ratio.get(mod_name)
    ar_lo = ar.lo if ar else 0.5
    ar_hi = ar.hi if ar else 1.5
    return HierarchicalBlock(
        name=inst.name,
        inner_problem=inner,
        ar_lo=ar_lo,
        ar_hi=ar_hi,
        module_name=mod_name,
    )


def _tile_block_template(
    submodule: str, netlist: Netlist, hints: Hints, *, instance_name: str
) -> Block:
    """Build a Block representing the contents of one tile in a structured layout.

    For hard/opaque submodules the tile is a HardBlock or SoftBlock template.
    For nested structured submodules we recursively build a StructuredBlock —
    its outer dimensions then become the parent's tile_w/tile_h.
    """
    if submodule in hints.hard_macro:
        spec = hints.hard_macro[submodule]
        return HardBlock(
            name=instance_name, w=spec.w, h=spec.h,
            allowed_orients=spec.allowed_orients, module_name=submodule,
        )
    if submodule in hints.opaque:
        spec = hints.opaque[submodule]
        ar = hints.aspect_ratio.get(submodule)
        ar_lo = ar.lo if ar else spec.ar_lo
        ar_hi = ar.hi if ar else spec.ar_hi
        return SoftBlock(
            name=instance_name, area=spec.area, ar_lo=ar_lo, ar_hi=ar_hi,
            module_name=submodule,
        )
    if submodule in hints.structured:
        blk = _structured_block(instance_name, submodule, netlist, hints)
        blk.module_name = submodule
        return blk
    raise ValueError(
        f"submodule {submodule!r} of a structured block has no hint; add "
        f"`hard_macro`, `opaque`, or `structured` so its tile dimensions are known"
    )


def _tile_outer_dims(tile: Block) -> tuple[float, float]:
    if isinstance(tile, HardBlock):
        return tile.w, tile.h
    if isinstance(tile, SoftBlock):
        side = math.sqrt(tile.area) if tile.area > 0 else 0.0
        return side, side
    if isinstance(tile, StructuredBlock):
        return tile.w, tile.h
    raise TypeError(f"can't compute outer dims of {type(tile).__name__}")


def _structured_block(
    instance_name: str, mod_name: str, netlist: Netlist, hints: Hints
) -> StructuredBlock:
    spec = hints.structured[mod_name]
    module = netlist.modules.get(mod_name)
    if module is None or not module.instances:
        raise ValueError(
            f"module {mod_name!r} is structured-hinted but has no sub-instances"
        )
    submodules = {sub.module for sub in module.instances}
    if len(submodules) > 1:
        raise NotImplementedError(
            f"structured module {mod_name!r} has heterogeneous tiles {submodules}; "
            f"only homogeneous tiles supported"
        )
    (submodule_name,) = submodules

    # Build one tile to learn dimensions; then build per-instance tiles so
    # each child has a unique block (positions are derived from the layout).
    template = _tile_block_template(
        submodule_name, netlist, hints, instance_name="<template>"
    )
    tile_w, tile_h = _tile_outer_dims(template)
    rows, cols = _resolve_grid(spec, len(module.instances))
    inst_names = [sub.name for sub in module.instances]
    layout, _total_w, _total_h = array_layout(
        inst_names,
        rows=rows,
        cols=cols,
        tile_w=tile_w,
        tile_h=tile_h,
        pitch_x=spec.pitch_x,
        pitch_y=spec.pitch_y,
    )
    children: list[ChildPlacement] = []
    for tp in layout:
        tile = _tile_block_template(
            submodule_name, netlist, hints, instance_name=tp.instance
        )
        children.append(
            ChildPlacement(
                name=tp.instance, x=tp.x, y=tp.y, orient=tp.orient, block=tile,
            )
        )
    return StructuredBlock(
        name=instance_name,
        rows=rows,
        cols=cols,
        tile_w=tile_w,
        tile_h=tile_h,
        pitch_x=spec.pitch_x,
        pitch_y=spec.pitch_y,
        child_layout=children,
    )


def flatten_to_leaves(
    netlist: Netlist, hints: Hints, top: str | None = None
) -> FloorplanProblem:
    """Build a single-level FloorplanProblem with every leaf as a top-level block.

    Used by `--mode flat`: collapses the entire Verilog hierarchy and reconstructs
    nets at the leaf level by following port mappings through every intermediate
    module. Each leaf instance gets a hierarchical-path name like
    ``top/u_outer/b_0_0/t_0_0`` so collisions are impossible.

    Only ``hard_macro`` and ``opaque`` hints apply at the leaf level. ``structured``
    hints are an error in flatten mode (mixing forced grids with a flat solve
    is not meaningful — use hierarchical mode for that).
    """
    top_name = top or hints.top or netlist.top
    if top_name not in netlist.modules:
        raise KeyError(f"top module {top_name!r} not in netlist")
    problem = FloorplanProblem(top_module=top_name)
    nets_by_name: dict[str, Net] = {}
    structured_seen: set[str] = set()

    def visit(module_name: str, inst_path: str, wire_map: dict[str, str]) -> None:
        module = netlist.modules.get(module_name)
        if module is None:
            return
        # Local (non-port) wires in this scope get unique flat names so they
        # don't collide across instance copies.
        for wire in module.wires:
            if wire not in wire_map:
                wire_map[wire] = f"{inst_path}/{wire}" if inst_path else wire

        for inst in module.instances:
            if inst.module in hints.structured:
                structured_seen.add(inst.module)
            sub_path = f"{inst_path}/{inst.name}" if inst_path else inst.name
            sub_module = netlist.modules.get(inst.module)
            # Resolve this instance's port connections via the parent wire_map.
            resolved_conns: dict[str, str] = {}
            for port_name, parent_net in inst.connections.items():
                if parent_net.startswith("<const") or parent_net.startswith("<expr"):
                    continue
                resolved_conns[port_name] = wire_map.get(parent_net, parent_net)

            if sub_module is None or netlist.is_leaf(inst.module):
                block = _make_leaf_block(inst.module, sub_path, hints)
                if block is None:
                    raise ValueError(
                        f"flat: leaf {sub_path!r} (module {inst.module!r}) has no "
                        f"hard_macro or opaque hint"
                    )
                problem.blocks[sub_path] = block
                problem.instance_modules[sub_path] = inst.module
                port_dir = {p.name: p.direction for p in (sub_module.ports if sub_module else [])}
                for port_name, flat_net in resolved_conns.items():
                    d = port_dir.get(port_name, PortDir.INPUT)
                    side = _DIR_TO_SIDE[d]
                    frac = _stable_frac(port_name)
                    pin = Pin(block=sub_path, name=port_name, side=side, frac=frac)
                    block.pins.append(pin)
                    net = nets_by_name.setdefault(flat_net, Net(name=flat_net))
                    net.pins.append(pin)
                continue

            # Non-leaf: recurse, propagating port → net resolution downward.
            sub_wire_map: dict[str, str] = {}
            for sub_port in sub_module.ports:
                if sub_port.name in resolved_conns:
                    sub_wire_map[sub_port.name] = resolved_conns[sub_port.name]
                else:
                    sub_wire_map[sub_port.name] = f"{sub_path}/{sub_port.name}"
            visit(inst.module, sub_path, sub_wire_map)

    top_module = netlist.modules[top_name]
    initial_map: dict[str, str] = {p.name: p.name for p in top_module.ports}
    visit(top_name, "", initial_map)

    if structured_seen:
        raise ValueError(
            f"flat mode: structured hints on {sorted(structured_seen)!r} are not "
            f"compatible — drop them or use hierarchical mode"
        )

    problem.nets = [n for n in nets_by_name.values() if len({p.block for p in n.pins}) >= 2]
    return problem


def _make_leaf_block(module: str, instance_name: str, hints: Hints) -> Block | None:
    """Build a HardBlock or SoftBlock for a leaf module — used by flatten."""
    if module in hints.hard_macro:
        spec = hints.hard_macro[module]
        return HardBlock(
            name=instance_name, w=spec.w, h=spec.h,
            allowed_orients=spec.allowed_orients, module_name=module,
        )
    if module in hints.opaque:
        spec = hints.opaque[module]
        ar = hints.aspect_ratio.get(module)
        ar_lo = ar.lo if ar else spec.ar_lo
        ar_hi = ar.hi if ar else spec.ar_hi
        return SoftBlock(
            name=instance_name, area=spec.area, ar_lo=ar_lo, ar_hi=ar_hi,
            module_name=module,
        )
    return None


def _resolve_grid(spec: StructuredHint, n_instances: int) -> tuple[int, int]:
    """Pick (rows, cols) given a structured hint and the number of available children.

    For `kind=array`, both rows and cols come from the hint and rows*cols must
    accommodate the instances. For `kind=bitslice`, rows defaults to 1 and cols
    is derived from the instance count when not specified.
    """
    if spec.kind == "array":
        if spec.rows * spec.cols < n_instances:
            raise ValueError(
                f"array hint specifies {spec.rows}×{spec.cols}={spec.rows * spec.cols} "
                f"but module has {n_instances} instances"
            )
        return spec.rows, spec.cols
    if spec.kind == "bitslice":
        rows = max(1, spec.rows)
        cols = spec.cols if spec.cols > 0 else math.ceil(n_instances / rows)
        if rows * cols < n_instances:
            raise ValueError(
                f"bitslice hint with rows={rows}, cols={cols} cannot hold {n_instances} slices"
            )
        return rows, cols
    raise ValueError(f"unknown structured kind {spec.kind!r}")

