"""Minimal DEF writer for COMPONENTS + DIEAREA.

Produces enough DEF for OpenROAD to read and run global placement against.
Coordinates are emitted as integers in DEF database units.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable

from vastu.core.block import Orient, Placement


_ORIENT_TO_DEF = {
    Orient.R0: "N",
    Orient.R90: "W",
    Orient.R180: "S",
    Orient.R270: "E",
    Orient.MX: "FN",
    Orient.MY: "FS",
    Orient.MXR90: "FW",
    Orient.MYR90: "FE",
}


def write_def(
    path: str | Path,
    *,
    design_name: str,
    components: Iterable[tuple[str, str, Placement]],
    die_w: float,
    die_h: float,
    units: int = 1000,
    components_status: str = "PLACED",
) -> None:
    """Write a DEF.

    `components` is an iterable of (instance_name, macro_name, Placement). The
    Placement's (x, y) is the lower-left corner in user units; we multiply by
    `units` to get DEF database units (default 1000 = nm with µm input).
    """
    lines: list[str] = []
    lines.append("VERSION 5.8 ;")
    lines.append("DIVIDERCHAR \"/\" ;")
    lines.append("BUSBITCHARS \"[]\" ;")
    lines.append(f"DESIGN {design_name} ;")
    lines.append(f"UNITS DISTANCE MICRONS {units} ;")
    lines.append(
        f"DIEAREA ( 0 0 ) ( {int(round(die_w * units))} {int(round(die_h * units))} ) ;"
    )
    comp_list = list(components)
    lines.append(f"COMPONENTS {len(comp_list)} ;")
    for inst_name, macro_name, pl in comp_list:
        ox = int(round(pl.x * units))
        oy = int(round(pl.y * units))
        orient = _ORIENT_TO_DEF.get(pl.orient, "N")
        lines.append(
            f"    - {inst_name} {macro_name} + {components_status} ( {ox} {oy} ) {orient} ;"
        )
    lines.append("END COMPONENTS")
    lines.append("END DESIGN")
    Path(path).write_text("\n".join(lines) + "\n")
