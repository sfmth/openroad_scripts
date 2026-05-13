"""Raw netlist IR — direct Verilog hierarchy, before any hint-driven rewrite.

This is the bridge between the Verilog parser and the floorplan IR. Hints
operate on this representation; the rewrite pass produces `Block` objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PortDir(Enum):
    INPUT = "input"
    OUTPUT = "output"
    INOUT = "inout"


@dataclass(frozen=True)
class Port:
    name: str
    direction: PortDir
    width: int = 1  # 1 = scalar


@dataclass
class Instance:
    """A child instance within a module."""

    name: str
    module: str  # name of the instantiated module
    # port name on the *child* module -> net name in the *parent* module
    connections: dict[str, str] = field(default_factory=dict)


@dataclass
class Module:
    """A Verilog module (or a synthetic group introduced by hints)."""

    name: str
    ports: list[Port] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    # Local wire names declared inside this module body (not including ports).
    wires: list[str] = field(default_factory=list)

    def port_names(self) -> set[str]:
        return {p.name for p in self.ports}


@dataclass
class Netlist:
    """A flat collection of all parsed modules; `top` names the design root."""

    modules: dict[str, Module] = field(default_factory=dict)
    top: str = ""

    def get_top(self) -> Module:
        if self.top not in self.modules:
            raise KeyError(f"top module {self.top!r} not in netlist")
        return self.modules[self.top]

    def is_leaf(self, module_name: str) -> bool:
        """A module is a leaf if it has no instances (synthesizable RTL)."""
        m = self.modules.get(module_name)
        if m is None:
            return True  # external/blackbox
        return len(m.instances) == 0
