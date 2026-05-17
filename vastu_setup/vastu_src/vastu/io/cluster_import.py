"""Import an RTLMP cluster tree JSON into vastu's FloorplanProblem hierarchy."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from vastu.core.block import (
    Block,
    HardBlock,
    HierarchicalBlock,
    Orient,
    SoftBlock,
)
from vastu.core.hypergraph import Net, Pin
from vastu.hints.rewrite import FloorplanProblem


def load_cluster_tree(
    json_path: str,
    *,
    target_utilization: float = 0.7,
) -> tuple[FloorplanProblem, float, float, dict]:
    """Load an RTLMP cluster tree JSON.

    Returns:
        (problem, die_w, die_h, raw_data)
        - problem: top-level FloorplanProblem with root's children as blocks
        - die_w, die_h: core area dimensions in microns
        - raw_data: the parsed JSON dict (for downstream writers that need
          instance lists, dbu_per_micron, etc.)
    """
    with open(json_path) as f:
        data = json.load(f)

    floorplan = data["floorplan"]
    core = floorplan.get("core_area", floorplan.get("die_area"))
    die_w = core[2] - core[0] if len(core) == 4 else floorplan["width"]
    die_h = core[3] - core[1] if len(core) == 4 else floorplan["height"]

    clusters_by_id: dict[int, dict] = {}
    root_cluster: dict | None = None
    for c in data["clusters"]:
        clusters_by_id[c["id"]] = c
        if c.get("parent") is None:
            root_cluster = c

    if root_cluster is None:
        raise ValueError("No root cluster (parent=null) found in JSON")

    problem = _build_problem(
        root_cluster, clusters_by_id, target_utilization,
    )
    problem.top_module = root_cluster.get("name", "top")

    # IO pins as boundary blocks
    io_pins = data.get("io_pins", [])
    if io_pins:
        _add_io_boundary_blocks(problem, io_pins, die_w, die_h, clusters_by_id)

    # Fixed macros
    for fm in data.get("fixed_macros", []):
        blk = HardBlock(
            name=fm["inst_name"],
            w=fm["width"],
            h=fm["height"],
            fixed_x=fm["x"],
            fixed_y=fm["y"],
            fixed_orient=Orient[fm.get("orientation", "R0")],
            module_name=fm.get("name", fm["inst_name"]),
        )
        problem.blocks[fm["inst_name"]] = blk

    return problem, die_w, die_h, data


def _build_problem(
    parent: dict,
    all_clusters: dict[int, dict],
    util: float,
) -> FloorplanProblem:
    """Build a FloorplanProblem from one cluster's children."""
    problem = FloorplanProblem()
    child_ids = parent.get("children", [])
    sibling_names: dict[int, str] = {}

    for cid in child_ids:
        child = all_clusters[cid]
        block = _cluster_to_block(child, all_clusters, util)
        problem.blocks[child["name"]] = block
        sibling_names[cid] = child["name"]

    problem.nets = _build_nets(child_ids, all_clusters, sibling_names)
    return problem


def _cluster_to_block(
    cluster: dict,
    all_clusters: dict[int, dict],
    util: float,
) -> Block:
    """Convert one cluster dict into the appropriate vastu Block type."""
    children = cluster.get("children", [])
    has_macros = cluster.get("num_macros", 0) > 0
    has_stdcells = cluster.get("num_std_cells", 0) > 0
    ctype = cluster.get("type", "mixed")

    # Non-leaf: recurse into children
    if children:
        inner = _build_problem(cluster, all_clusters, util)
        return HierarchicalBlock(
            name=cluster["name"],
            inner_problem=inner,
            ar_lo=0.33,
            ar_hi=3.0,
        )

    # Leaf macro-only cluster
    if ctype == "macro" or (has_macros and not has_stdcells):
        macros = cluster.get("macros", [])
        if not macros:
            return SoftBlock(
                name=cluster["name"],
                area=max(1.0, cluster.get("macro_area", 1.0)),
            )
        inner = FloorplanProblem()
        for m in macros:
            # halo_x/halo_y in JSON are TOTAL (left+right, top+bottom), in microns.
            hx_total = m.get("halo_x", 0)
            hy_total = m.get("halo_y", 0)
            blk = HardBlock(
                name=m["inst_name"],
                w=m["width"] + hx_total,
                h=m["height"] + hy_total,
                allowed_orients=(Orient.R0, Orient.R180, Orient.MX, Orient.MY),
                module_name=m.get("name", m["inst_name"]),
            )
            if m.get("fixed", False):
                blk.fixed_x = m.get("fixed_x")
                blk.fixed_y = m.get("fixed_y")
            inner.blocks[m["inst_name"]] = blk
        return HierarchicalBlock(
            name=cluster["name"],
            inner_problem=inner,
            ar_lo=0.33,
            ar_hi=3.0,
        )

    # Leaf std-cell-only cluster
    if ctype == "stdcell" or (has_stdcells and not has_macros):
        raw_area = cluster.get("std_cell_area", 1.0)
        inflated = raw_area / util if util > 0 else raw_area
        return SoftBlock(
            name=cluster["name"],
            area=max(1.0, inflated),
            ar_lo=0.33,
            ar_hi=3.0,
        )

    # Leaf mixed cluster: macros + std-cells
    inner = FloorplanProblem()
    for m in cluster.get("macros", []):
        # halo_x/halo_y in JSON are TOTAL (left+right, top+bottom), in microns.
        hx_total = m.get("halo_x", 0)
        hy_total = m.get("halo_y", 0)
        blk = HardBlock(
            name=m["inst_name"],
            w=m["width"] + hx_total,
            h=m["height"] + hy_total,
            allowed_orients=(Orient.R0, Orient.R180, Orient.MX, Orient.MY),
            module_name=m.get("name", m["inst_name"]),
        )
        inner.blocks[m["inst_name"]] = blk
    sc_area = cluster.get("std_cell_area", 0)
    if sc_area > 0:
        inflated = sc_area / util if util > 0 else sc_area
        inner.blocks[f"{cluster['name']}_stdcells"] = SoftBlock(
            name=f"{cluster['name']}_stdcells",
            area=max(1.0, inflated),
            ar_lo=0.33,
            ar_hi=3.0,
        )
    return HierarchicalBlock(
        name=cluster["name"],
        inner_problem=inner,
        ar_lo=0.33,
        ar_hi=3.0,
    )


def _build_nets(
    child_ids: list[int],
    all_clusters: dict[int, dict],
    sibling_names: dict[int, str],
) -> list[Net]:
    """Build vastu Nets from cluster connection maps.

    Only sibling-to-sibling connections are included at this level.
    """
    nets: list[Net] = []
    seen: set[tuple[int, int]] = set()
    sibling_set = set(child_ids)

    for cid in child_ids:
        cluster = all_clusters[cid]
        conns = cluster.get("connections", {})
        for target_id_str, weight in conns.items():
            target_id = int(target_id_str)
            if target_id not in sibling_set:
                continue
            pair = (min(cid, target_id), max(cid, target_id))
            if pair in seen:
                continue
            seen.add(pair)
            pin_a = Pin(
                block=sibling_names[cid],
                name=f"to_{target_id}",
                side="C",
                frac=0.5,
            )
            pin_b = Pin(
                block=sibling_names[target_id],
                name=f"to_{cid}",
                side="C",
                frac=0.5,
            )
            nets.append(Net(
                name=f"n_{cid}_{target_id}",
                pins=[pin_a, pin_b],
                weight=float(weight),
            ))
    return nets


def _add_io_boundary_blocks(
    problem: FloorplanProblem,
    io_pins: list[dict],
    die_w: float,
    die_h: float,
    clusters_by_id: dict[int, dict],
) -> None:
    """Add thin fixed HardBlocks on each die edge for IO pin anchoring."""
    edge_thickness = 1.0

    # Create one fixed block per edge
    edges = {
        "W": HardBlock(name="__io_west", w=edge_thickness, h=die_h,
                        fixed_x=0.0, fixed_y=0.0),
        "E": HardBlock(name="__io_east", w=edge_thickness, h=die_h,
                        fixed_x=die_w - edge_thickness, fixed_y=0.0),
        "S": HardBlock(name="__io_south", w=die_w, h=edge_thickness,
                        fixed_x=0.0, fixed_y=0.0),
        "N": HardBlock(name="__io_north", w=die_w, h=edge_thickness,
                        fixed_x=0.0, fixed_y=die_h - edge_thickness),
    }
    for ename, eblk in edges.items():
        problem.blocks[eblk.name] = eblk

    # Create nets from IO pins to their connected clusters
    for pin_data in io_pins:
        side = pin_data.get("side", "W")
        # Compute fractional position along the edge
        if side in ("W", "E"):
            frac = pin_data["y"] / die_h if die_h > 0 else 0.5
            pin_side = "E" if side == "W" else "W"
        else:
            frac = pin_data["x"] / die_w if die_w > 0 else 0.5
            pin_side = "N" if side == "S" else "S"

        edge_name = {"W": "__io_west", "E": "__io_east",
                     "S": "__io_south", "N": "__io_north"}[side]
        io_pin = Pin(block=edge_name, name=pin_data["name"],
                     side=pin_side, frac=max(0.0, min(1.0, frac)))

        # Connect to clusters that reference this IO pin
        # For now, we create the pin but don't know which cluster it connects to
        # The cluster JSON connections already capture IO connectivity via
        # RTLMP's bundled pin model, so this is mainly for anchor biasing.
