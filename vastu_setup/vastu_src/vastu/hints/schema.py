"""Hint file schema (YAML) — the overlay that transforms a Verilog hierarchy
into a floorplan hierarchy.

Milestone 2 supports `hard_macro`, `opaque`, and `aspect_ratio` only.
Later milestones add `structured`, `flatten`, `group`, `fixed_location`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from vastu.core.block import Orient


@dataclass(frozen=True)
class HardMacroHint:
    w: float
    h: float
    allowed_orients: tuple[Orient, ...] = (Orient.R0,)


@dataclass(frozen=True)
class OpaqueHint:
    area: float
    ar_lo: float = 0.5
    ar_hi: float = 1.5


@dataclass(frozen=True)
class AspectRatioHint:
    lo: float
    hi: float


@dataclass(frozen=True)
class StructuredHint:
    kind: str  # "array" or "bitslice"
    rows: int = 1
    cols: int = 1
    pitch_x: float = 0.0
    pitch_y: float = 0.0


@dataclass(frozen=True)
class FixedLocationHint:
    x: float
    y: float
    orient: Orient = Orient.R0


@dataclass(frozen=True)
class GroupHint:
    parent: str
    instances: tuple[str, ...]
    name: str


@dataclass
class Hints:
    """Parsed hint file. Keys are module names (or instance paths for fixed_location)."""

    top: Optional[str] = None
    hard_macro: dict[str, HardMacroHint] = field(default_factory=dict)
    opaque: dict[str, OpaqueHint] = field(default_factory=dict)
    aspect_ratio: dict[str, AspectRatioHint] = field(default_factory=dict)
    structured: dict[str, StructuredHint] = field(default_factory=dict)
    flatten: set[str] = field(default_factory=set)
    fixed_location: dict[str, FixedLocationHint] = field(default_factory=dict)
    group: list[GroupHint] = field(default_factory=list)


def _orient(value: str | None, default: Orient = Orient.R0) -> Orient:
    if value is None:
        return default
    return Orient(value)


def _orients(values: list[str] | None) -> tuple[Orient, ...]:
    if not values:
        return (Orient.R0,)
    return tuple(Orient(v) for v in values)


def load_hints(path: str | Path | None) -> Hints:
    if path is None:
        return Hints()
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
    h = Hints(top=raw.get("top"))

    for mod, spec in (raw.get("hard_macro") or {}).items():
        h.hard_macro[mod] = HardMacroHint(
            w=float(spec["w"]),
            h=float(spec["h"]),
            allowed_orients=_orients(spec.get("allowed_orients")),
        )
    for mod, spec in (raw.get("opaque") or {}).items():
        h.opaque[mod] = OpaqueHint(
            area=float(spec["area"]),
            ar_lo=float(spec.get("ar_lo", 0.5)),
            ar_hi=float(spec.get("ar_hi", 1.5)),
        )
    for mod, spec in (raw.get("aspect_ratio") or {}).items():
        h.aspect_ratio[mod] = AspectRatioHint(
            lo=float(spec["lo"]), hi=float(spec["hi"])
        )
    for mod, spec in (raw.get("structured") or {}).items():
        h.structured[mod] = StructuredHint(
            kind=spec["kind"],
            rows=int(spec.get("rows", 1)),
            cols=int(spec.get("cols", 1)),
            pitch_x=float(spec.get("pitch_x", 0.0)),
            pitch_y=float(spec.get("pitch_y", 0.0)),
        )
    h.flatten = set(raw.get("flatten") or [])
    for path_str, spec in (raw.get("fixed_location") or {}).items():
        h.fixed_location[path_str] = FixedLocationHint(
            x=float(spec["x"]),
            y=float(spec["y"]),
            orient=_orient(spec.get("orient")),
        )
    for spec in raw.get("group") or []:
        h.group.append(
            GroupHint(
                parent=spec["parent"],
                instances=tuple(spec["instances"]),
                name=spec["name"],
            )
        )
    return h
