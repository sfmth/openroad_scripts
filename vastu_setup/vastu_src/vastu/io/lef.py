"""Minimal LEF subset parser.

Extracts MACRO name + SIZE w BY h from a LEF file. Pin offsets and layer info
are skipped — those come into play later. The parser is line-oriented and
ignores everything outside MACRO blocks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LefMacro:
    name: str
    w: float = 0.0
    h: float = 0.0
    pins: dict[str, tuple[float, float]] = field(default_factory=dict)


_TOKEN = re.compile(r"\S+")


def parse_lef(path: str | Path) -> dict[str, LefMacro]:
    """Return a dict keyed by macro name."""
    macros: dict[str, LefMacro] = {}
    text = Path(path).read_text()
    cur: LefMacro | None = None
    in_pin: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        if toks[0] == "MACRO" and len(toks) >= 2:
            cur = LefMacro(name=toks[1])
            continue
        if cur is None:
            continue
        if toks[0] == "SIZE" and "BY" in toks:
            # SIZE 200 BY 400 ;
            try:
                w = float(toks[1])
                bi = toks.index("BY")
                h = float(toks[bi + 1])
                cur.w = w
                cur.h = h
            except (ValueError, IndexError):
                pass
            continue
        if toks[0] == "PIN" and len(toks) >= 2:
            in_pin = toks[1]
            continue
        if toks[0] == "END" and in_pin is not None and toks[-1] == in_pin:
            in_pin = None
            continue
        if toks[0] == "END" and len(toks) >= 2 and toks[1] == cur.name:
            macros[cur.name] = cur
            cur = None
            in_pin = None
            continue
    return macros
