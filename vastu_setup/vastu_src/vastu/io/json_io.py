from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vastu.core.block import (
    Block,
    HardBlock,
    Orient,
    Placement,
    SoftBlock,
    StructuredBlock,
)
from vastu.core.hypergraph import Net, Pin


def _block_to_dict(b: Block) -> dict[str, Any]:
    pins = [
        {"name": p.name, "dx": p.dx, "dy": p.dy} for p in b.pins
    ]
    if isinstance(b, SoftBlock):
        return {
            "kind": "soft",
            "name": b.name,
            "area": b.area,
            "ar_lo": b.ar_lo,
            "ar_hi": b.ar_hi,
            "cells": list(b.cells),
            "pins": pins,
        }
    if isinstance(b, HardBlock):
        return {
            "kind": "hard",
            "name": b.name,
            "w": b.w,
            "h": b.h,
            "fixed_x": b.fixed_x,
            "fixed_y": b.fixed_y,
            "fixed_orient": b.fixed_orient.value if b.fixed_orient else None,
            "allowed_orients": [o.value for o in b.allowed_orients],
            "pins": pins,
        }
    if isinstance(b, StructuredBlock):
        return {
            "kind": "structured",
            "name": b.name,
            "rows": b.rows,
            "cols": b.cols,
            "tile_w": b.tile_w,
            "tile_h": b.tile_h,
            "pitch_x": b.pitch_x,
            "pitch_y": b.pitch_y,
            "child_layout": list(b.child_layout),
            "pins": pins,
        }
    raise TypeError(f"unsupported block kind {type(b).__name__}")


def _block_from_dict(d: dict[str, Any]) -> Block:
    pins = [Pin(block=d["name"], name=p["name"], dx=p["dx"], dy=p["dy"]) for p in d.get("pins", [])]
    kind = d["kind"]
    if kind == "soft":
        b = SoftBlock(
            name=d["name"],
            area=d["area"],
            ar_lo=d.get("ar_lo", 0.5),
            ar_hi=d.get("ar_hi", 1.5),
            cells=list(d.get("cells", [])),
        )
    elif kind == "hard":
        allowed = tuple(Orient(o) for o in d.get("allowed_orients", ["R0"]))
        fo = Orient(d["fixed_orient"]) if d.get("fixed_orient") else None
        b = HardBlock(
            name=d["name"],
            w=d["w"],
            h=d["h"],
            fixed_x=d.get("fixed_x"),
            fixed_y=d.get("fixed_y"),
            fixed_orient=fo,
            allowed_orients=allowed,
        )
    elif kind == "structured":
        b = StructuredBlock(
            name=d["name"],
            rows=d["rows"],
            cols=d["cols"],
            tile_w=d["tile_w"],
            tile_h=d["tile_h"],
            pitch_x=d.get("pitch_x", 0.0),
            pitch_y=d.get("pitch_y", 0.0),
            child_layout=list(d.get("child_layout", [])),
        )
    else:
        raise ValueError(f"unknown block kind '{kind}'")
    b.pins.extend(pins)
    return b


def dump_problem(
    blocks: dict[str, Block],
    nets: list[Net],
    path: str | Path,
    *,
    placements: dict[str, Placement] | None = None,
) -> None:
    payload = {
        "blocks": [_block_to_dict(b) for b in blocks.values()],
        "nets": [
            {
                "name": n.name,
                "weight": n.weight,
                "fanout_cap": n.fanout_cap,
                "pins": [
                    {"block": p.block, "name": p.name, "dx": p.dx, "dy": p.dy}
                    for p in n.pins
                ],
            }
            for n in nets
        ],
    }
    if placements:
        payload["placements"] = {
            name: {
                "x": p.x,
                "y": p.y,
                "w": p.w,
                "h": p.h,
                "orient": p.orient.value,
            }
            for name, p in placements.items()
        }
    Path(path).write_text(json.dumps(payload, indent=2))


def load_problem(path: str | Path) -> tuple[dict[str, Block], list[Net], dict[str, Placement] | None]:
    payload = json.loads(Path(path).read_text())
    blocks = {b["name"]: _block_from_dict(b) for b in payload["blocks"]}
    nets = [
        Net(
            name=n["name"],
            weight=n.get("weight", 1.0),
            fanout_cap=n.get("fanout_cap", 0),
            pins=[
                Pin(block=p["block"], name=p["name"], dx=p["dx"], dy=p["dy"])
                for p in n["pins"]
            ],
        )
        for n in payload.get("nets", [])
    ]
    placements = None
    if "placements" in payload:
        placements = {
            name: Placement(
                x=p["x"], y=p["y"], w=p["w"], h=p["h"], orient=Orient(p.get("orient", "R0"))
            )
            for name, p in payload["placements"].items()
        }
    return blocks, nets, placements
