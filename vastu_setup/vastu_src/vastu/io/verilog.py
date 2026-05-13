"""Verilog → Netlist IR using pyverilog.

We support a structural subset: module declarations, scalar/vector ports,
wire declarations, and instantiations with named or positional port maps where
each port arg is an Identifier (whole-net connection).

Bit-slice and concat connections are recognized but recorded with the slice as
part of the net name (e.g. ``data[7:0]``); HPWL still treats this as a single
hyperedge endpoint, which is conservative.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pyverilog.vparser.ast as vast
from pyverilog.vparser.parser import parse as vparse

from vastu.core.netlist import Instance, Module, Netlist, Port, PortDir


def _port_dir(node: vast.Node) -> PortDir:
    if isinstance(node, vast.Input):
        return PortDir.INPUT
    if isinstance(node, vast.Output):
        return PortDir.OUTPUT
    if isinstance(node, vast.Inout):
        return PortDir.INOUT
    raise ValueError(f"unhandled port direction node: {type(node).__name__}")


def _width_of(width_node: vast.Width | None) -> int:
    if width_node is None:
        return 1
    msb = _eval_int(width_node.msb)
    lsb = _eval_int(width_node.lsb)
    return abs(msb - lsb) + 1


def _eval_int(node: vast.Node) -> int:
    if isinstance(node, vast.IntConst):
        s = node.value
        if "'" in s:
            s = s.split("'")[-1]
            base = s[0].lower() if s and s[0].lower() in "bohd" else "d"
            digits = s[1:] if base in "bohd" else s
            return int(digits, {"b": 2, "o": 8, "h": 16, "d": 10}[base])
        return int(s)
    if isinstance(node, vast.Minus):
        return _eval_int(node.left) - _eval_int(node.right)
    if isinstance(node, vast.Plus):
        return _eval_int(node.left) + _eval_int(node.right)
    raise ValueError(f"cannot constant-fold {type(node).__name__}")


def _net_name(node: vast.Node) -> str:
    """Stringify a port-arg expression to a net identifier.

    Whole-net Identifier -> name. Bit/range select -> "name[hi:lo]".
    Concatenation -> joined "{a,b,c}". Constants -> "<const>".
    """
    if isinstance(node, vast.Identifier):
        return node.name
    if isinstance(node, vast.Pointer):
        # name[idx]
        return f"{_net_name(node.var)}[{_eval_int(node.ptr)}]"
    if isinstance(node, vast.Partselect):
        msb = _eval_int(node.msb)
        lsb = _eval_int(node.lsb)
        return f"{_net_name(node.var)}[{msb}:{lsb}]"
    if isinstance(node, vast.Concat):
        return "{" + ",".join(_net_name(c) for c in node.list) + "}"
    if isinstance(node, vast.IntConst):
        return f"<const:{node.value}>"
    return f"<expr:{type(node).__name__}>"


def _collect_module(mdef: vast.ModuleDef) -> Module:
    ports: list[Port] = []
    # Pyverilog has two port styles: ANSI (Ioport with embedded direction) and
    # non-ANSI (just identifiers in Portlist + separate Decl(Input/Output) inside).
    declared_dirs: dict[str, PortDir] = {}
    declared_widths: dict[str, int] = {}
    for io in mdef.portlist.ports:
        if isinstance(io, vast.Ioport):
            inner = io.first  # Input/Output/Inout
            ports.append(
                Port(name=inner.name, direction=_port_dir(inner), width=_width_of(inner.width))
            )
        elif isinstance(io, vast.Port):
            # Non-ANSI: name only here; direction comes from inner Decl.
            declared_dirs[io.name] = PortDir.INPUT  # placeholder; overwritten below
            declared_widths[io.name] = 1

    instances: list[Instance] = []
    wires: list[str] = []

    for item in mdef.items:
        if isinstance(item, vast.Decl):
            for sub in item.list:
                if isinstance(sub, (vast.Input, vast.Output, vast.Inout)):
                    declared_dirs[sub.name] = _port_dir(sub)
                    declared_widths[sub.name] = _width_of(sub.width)
                elif isinstance(sub, vast.Wire):
                    if sub.name not in declared_dirs:
                        wires.append(sub.name)
        elif isinstance(item, vast.InstanceList):
            sub_module = item.module
            for inst in item.instances:
                conns: dict[str, str] = {}
                # Pyverilog stores positional ports with portname=None.
                positional = all(pa.portname is None for pa in inst.portlist)
                if positional:
                    # We don't know the child's port order here without
                    # cross-referencing. Use synthetic port names @0,@1,...
                    for i, pa in enumerate(inst.portlist):
                        conns[f"@{i}"] = _net_name(pa.argname)
                else:
                    for pa in inst.portlist:
                        conns[pa.portname] = _net_name(pa.argname)
                instances.append(
                    Instance(name=inst.name, module=sub_module, connections=conns)
                )

    # If non-ANSI, fold declared_dirs back into ports preserving Portlist order.
    if not ports and declared_dirs:
        for io in mdef.portlist.ports:
            if isinstance(io, vast.Port):
                d = declared_dirs.get(io.name, PortDir.INPUT)
                w = declared_widths.get(io.name, 1)
                ports.append(Port(name=io.name, direction=d, width=w))

    return Module(name=mdef.name, ports=ports, instances=instances, wires=wires)


def parse_verilog(
    sources: Iterable[str | Path],
    *,
    top: str | None = None,
    includes: Iterable[str | Path] = (),
    defines: Iterable[str] = (),
) -> Netlist:
    """Parse one or more .v files into a Netlist.

    If `top` is omitted, the root module is detected by elimination — the
    module that no other module instantiates.
    """
    files = [str(s) for s in sources]
    ast, _directives = vparse(
        files,
        preprocess_include=[str(i) for i in includes],
        preprocess_define=list(defines),
    )
    netlist = Netlist()
    for desc in ast.description.definitions:
        if isinstance(desc, vast.ModuleDef):
            mod = _collect_module(desc)
            netlist.modules[mod.name] = mod

    if top is None:
        instantiated = {
            inst.module
            for m in netlist.modules.values()
            for inst in m.instances
        }
        roots = [n for n in netlist.modules if n not in instantiated]
        if len(roots) == 1:
            top = roots[0]
        elif len(roots) == 0 and netlist.modules:
            top = next(iter(netlist.modules))
        else:
            raise ValueError(
                f"could not determine top module; candidates: {roots}. "
                "Pass top= explicitly."
            )
    netlist.top = top
    return netlist
