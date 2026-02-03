
#!/usr/bin/env python3
"""
hfn_analyze.py

Find High-Fanout Nets (HFNs) in (structural / gate-level) Verilog and report,
for each HFN, how many downstream cells also connect to other HFNs.

Assumptions (practical, not “pure Verilog”):
- Works best on synthesized / structural netlists with explicit instances like:
    NAND2_X1 U1 ( .A(n1), .B(n2), .ZN(n3) );
- Tries to infer input vs output ports using:
    (1) optional per-cell port-direction JSON, otherwise
    (2) heuristics on port names (Q/Z/Y/O/etc => outputs)
- Handles simple continuous assigns:
    assign a = b;   (treated as net alias)
  More complex assigns are parsed as dependencies (not full Boolean modeling).

Usage examples:
  python3 hfn_analyze.py --verilog netlist.v --threshold 50 --depth 1 --top 30
  python3 hfn_analyze.py --verilog netlist.v --threshold 200 --depth 3 --out report.json --csv report.csv
  python3 hfn_analyze.py --verilog netlist.v --threshold 100 --portdir-json lib_portdirs.json

Port-direction JSON schema (optional):
{
  "NAND2_X1": {"inputs": ["A","B"], "outputs": ["ZN"]},
  "DFF_X1":   {"inputs": ["D","CK","RN"], "outputs": ["Q","QN"]}
}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import fnmatch
from collections import defaultdict, deque, Counter
from dataclasses import dataclass
from typing import Dict, List, Tuple, Set, Optional, Iterable


# ----------------------------
# Small utilities
# ----------------------------

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def strip_comments(verilog_text: str) -> str:
    """Remove // and /* */ comments."""
    # Remove /* ... */ first (non-greedy, DOTALL)
    verilog_text = re.sub(r"/\*.*?\*/", "", verilog_text, flags=re.DOTALL)
    # Remove // ... endline
    verilog_text = re.sub(r"//.*?$", "", verilog_text, flags=re.MULTILINE)
    return verilog_text

def split_top_level_commas(s: str) -> List[str]:
    """Split by commas, but only at top-level (not inside (), {}, [])."""
    parts = []
    buf = []
    par = br = cr = 0
    for ch in s:
        if ch == "(":
            par += 1
        elif ch == ")":
            par = max(0, par - 1)
        elif ch == "[":
            br += 1
        elif ch == "]":
            br = max(0, br - 1)
        elif ch == "{":
            cr += 1
        elif ch == "}":
            cr = max(0, cr - 1)

        if ch == "," and par == 0 and br == 0 and cr == 0:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
        else:
            buf.append(ch)

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts

# Identifier extraction (supports escaped identifiers somewhat)
_ESCAPED_ID = r"\\[^ \t\r\n\),;]+"
_NORMAL_ID = r"[A-Za-z_][A-Za-z0-9_\$]*"
_INDEX = r"(?:\[[^\]]+\])?"  # keep simple (one bracket)
ID_RE = re.compile(rf"(?:{_ESCAPED_ID}|{_NORMAL_ID}{_INDEX})")

CONST_RE = re.compile(r"^\s*\d+'[bdhoBDHO][0-9a-fA-FxXzZ_]+\s*$|^\s*\d+\s*$")

def extract_identifiers(expr: str) -> List[str]:
    """
    Extract likely net identifiers from an expression.
    Filters out obvious constants.
    """
    expr = expr.strip()
    if not expr:
        return []

    # Remove surrounding braces for concatenations (we still want IDs inside)
    # Keep it simple: just find all identifiers.
    ids = ID_RE.findall(expr)

    # Filter: drop keywords & constants-like tokens
    keywords = {
        "input","output","inout","wire","reg","logic","assign",
        "module","endmodule","begin","end","generate","endgenerate",
        "if","else","case","endcase","for","while","always","always_ff","always_comb",
        "posedge","negedge","parameter","localparam","supply0","supply1","tri",
    }
    out = []
    for t in ids:
        # Clean escaped identifiers: keep leading backslash as part of name
        tt = t.strip()
        if tt in keywords:
            continue
        if CONST_RE.match(tt):
            continue
        # Drop 1'b0 style (won't match ID_RE anyway, but keep robust)
        if "'" in tt:
            continue
        out.append(tt)
    return out


# ----------------------------
# Union-Find for net aliases
# ----------------------------

class UnionFind:
    def __init__(self):
        self.parent: Dict[str, str] = {}
        self.rank: Dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
            return x
        # path compression
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


# ----------------------------
# Data model
# ----------------------------

@dataclass(frozen=True)
class PinRef:
    inst: str
    cell: str
    port: str

@dataclass
class Instance:
    name: str
    cell: str
    inputs: List[str]
    outputs: List[str]


# ----------------------------
# Port direction inference
# ----------------------------

DEFAULT_OUTPUT_PORTS = {
    "Y","Z","ZN","Q","QN","O","OUT","CO","COUT","SUM","S","SO","CARRY"
}
DEFAULT_OUTPUT_PREFIXES = ("Q", "Z", "Y", "O")  # fallback heuristic
DEFAULT_INPUT_PORTS = {
    "A","B","C","D","E","F","G","H","I","J","K",
    "IN","I0","I1","I2","I3","I4","I5","I6","I7",
    "D","DI","SI","SE","CK","CLK","CP","EN","E",
    "RN","RST","RESET","SN","SET","TE","TI",
    "S0","S1","S2","S3","S4"
}

def is_output_port(port: str, cell: str, portdir_map: Optional[dict]) -> bool:
    if portdir_map is not None and cell in portdir_map:
        outs = set(portdir_map[cell].get("outputs", []))
        if port in outs:
            return True
        ins = set(portdir_map[cell].get("inputs", []))
        if port in ins:
            return False
    if port in DEFAULT_OUTPUT_PORTS:
        return True
    # Heuristic: common stdcell output pins often start with Q or Z or Y or O
    # but avoid classifying VDD/VSS as outputs (if present).
    if port.upper() in ("VDD","VSS","VPWR","VGND","VNW","VPB","GND","VCC"):
        return False
    up = port.upper()
    return any(up.startswith(pfx) for pfx in DEFAULT_OUTPUT_PREFIXES) and (up not in DEFAULT_INPUT_PORTS)


# ----------------------------
# Statement splitting
# ----------------------------

def iter_statements(verilog_text: str) -> Iterable[str]:
    """
    Yield statements split on ';' at top-level (not inside (), {}, []).
    """
    buf = []
    par = br = cr = 0
    in_str = False
    esc = False

    for ch in verilog_text:
        if in_str:
            buf.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            buf.append(ch)
            continue

        if ch == "(":
            par += 1
        elif ch == ")":
            par = max(0, par - 1)
        elif ch == "[":
            br += 1
        elif ch == "]":
            br = max(0, br - 1)
        elif ch == "{":
            cr += 1
        elif ch == "}":
            cr = max(0, cr - 1)

        if ch == ";" and par == 0 and br == 0 and cr == 0:
            stmt = "".join(buf).strip()
            buf = []
            if stmt:
                yield stmt
        else:
            buf.append(ch)

    tail = "".join(buf).strip()
    if tail:
        yield tail


# ----------------------------
# Parsing: instances and assigns
# ----------------------------

# Named-port: .PORT(expr)
NAMED_PORT_RE = re.compile(r"\.(?P<port>[A-Za-z_][A-Za-z0-9_\$]*)\s*\(\s*(?P<expr>.*?)\s*\)", re.DOTALL)

# Instance header (best-effort): [(*attrs*)] CELL [#(...)] INST ( ... )
# Accept escaped instance names so hierarchical names (with dots/slashes) are preserved.
INST_NAME_RE = rf"(?:{_ESCAPED_ID}|{_NORMAL_ID})"
INST_HDR_RE = re.compile(
    rf"^\s*(?:\(\*.*?\*\)\s*)*(?P<cell>[A-Za-z_][A-Za-z0-9_\$]*)\s*"
    rf"(?:#\s*\(.*\)\s*)?"
    rf"(?P<inst>{INST_NAME_RE})\s*\(\s*(?P<body>.*)\s*\)\s*$",
    flags=re.DOTALL
)

ASSIGN_RE = re.compile(r"^\s*assign\s+(?P<lhs>.+?)\s*=\s*(?P<rhs>.+)\s*$", flags=re.DOTALL)

PRIM_RE = re.compile(
    rf"^\s*(?P<prim>buf|not|and|or|xor|xnor|nand|nor)\s+"
    rf"(?:(?P<inst>{INST_NAME_RE})\s*)?"
    rf"\(\s*(?P<body>.*)\s*\)\s*$",
    flags=re.IGNORECASE | re.DOTALL
)

def parse_named_ports(body: str) -> List[Tuple[str, str]]:
    """Return list of (port, expr) pairs for named port connections."""
    return [(m.group("port"), m.group("expr")) for m in NAMED_PORT_RE.finditer(body)]

def parse_positional_ports(body: str) -> List[str]:
    """Return list of expressions in positional port connection list."""
    return split_top_level_commas(body)

def parse_verilog_netlist(
    path: str,
    portdir_map: Optional[dict] = None,
) -> Tuple[List[Instance], UnionFind]:
    """
    Parse a structural Verilog file into instances (with inferred input/output nets),
    and net alias unions from simple assigns.
    Module-local names are scoped as <module>/<name> to avoid cross-module collisions.
    """
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    txt = strip_comments(raw)

    instances: List[Instance] = []
    uf = UnionFind()

    # First pass: find simple assigns for aliasing
    # Second pass: parse instances and non-trivial assigns as pseudo-cells
    # (We do it in one pass here, unioning whenever we can.)
    assign_counter = 0
    prim_counter = 0

    current_module: Optional[str] = None
    # Accept both normal and escaped module identifiers (Yosys uses escaped names with dots).
    MODULE_HDR_RE = re.compile(rf"\bmodule\s+(?P<name>(?:{_ESCAPED_ID}|{_NORMAL_ID}))\b")

    def scope_name(name: str) -> str:
        return f"{current_module}/{name}" if current_module else name

    for stmt in iter_statements(txt):
        st = stmt.strip()
        if not st:
            continue

        # Track module scopes and skip the declarations themselves
        m_mod = MODULE_HDR_RE.search(st)
        if m_mod:
            current_module = m_mod.group("name")
            continue
        if st.lower().startswith("endmodule"):
            current_module = None
            continue

        # Skip net/param declarations (module scope tracked above)
        low = st.lstrip().lower()
        if low.startswith(("input ", "output ", "inout ", "wire ", "reg ", "logic ", "tri ", "parameter ", "localparam ")):
            continue

        # Continuous assign
        m_as = ASSIGN_RE.match(st)
        if m_as:
            lhs_expr = m_as.group("lhs").strip()
            rhs_expr = m_as.group("rhs").strip()

            lhs_ids = [scope_name(x) for x in extract_identifiers(lhs_expr)]
            rhs_ids = [scope_name(x) for x in extract_identifiers(rhs_expr)]

            # If assign is a simple alias "assign a = b;" (single id each, no operators)
            # we union them. Otherwise create a pseudo-instance dependency.
            simple_alias = False
            if len(lhs_ids) == 1 and len(rhs_ids) == 1:
                # Check for operators in rhs/lhs (very rough)
                if re.fullmatch(rf"\s*(?:{_ESCAPED_ID}|{_NORMAL_ID}{_INDEX})\s*", lhs_expr) and \
                   re.fullmatch(rf"\s*(?:{_ESCAPED_ID}|{_NORMAL_ID}{_INDEX})\s*", rhs_expr):
                    simple_alias = True

            if simple_alias:
                uf.union(lhs_ids[0], rhs_ids[0])
            else:
                assign_counter += 1
                inst_name = scope_name(f"__assign_{assign_counter}")
                cell_name = "__assign"
                # Model as: rhs nets are inputs, lhs nets are outputs (often one net)
                instances.append(Instance(
                    name=inst_name,
                    cell=cell_name,
                    inputs=rhs_ids,
                    outputs=lhs_ids,
                ))
            continue

        # Primitive gate (positional)
        m_prim = PRIM_RE.match(st)
        if m_prim:
            prim_counter += 1
            inst_name = m_prim.group("inst") or f"__prim_{prim_counter}"
            inst_name = scope_name(inst_name)
            cell_name = m_prim.group("prim").lower()
            body = m_prim.group("body")
            exprs = parse_positional_ports(body)
            if len(exprs) >= 1:
                outs = [scope_name(n) for n in extract_identifiers(exprs[0])]
                ins = []
                for e in exprs[1:]:
                    ins.extend(scope_name(n) for n in extract_identifiers(e))
                instances.append(Instance(
                    name=inst_name,
                    cell=cell_name,
                    inputs=ins,
                    outputs=outs,
                ))
            continue

        # Named-port instance
        m_inst = INST_HDR_RE.match(st)
        if m_inst:
            cell = m_inst.group("cell")
            inst = scope_name(m_inst.group("inst"))
            body = m_inst.group("body")

            conns = parse_named_ports(body)
            in_nets: List[str] = []
            out_nets: List[str] = []

            for port, expr in conns:
                ids = [scope_name(n) for n in extract_identifiers(expr)]
                if not ids:
                    continue
                # If multiple IDs in an expr (rare in gate-level named ports),
                # treat them as inputs; for outputs, keep the first.
                if is_output_port(port, cell, portdir_map):
                    out_nets.append(ids[0])
                else:
                    in_nets.extend(ids)

            instances.append(Instance(
                name=inst,
                cell=cell,
                inputs=in_nets,
                outputs=out_nets,
            ))
            continue

        # Otherwise: ignore (could be specify blocks, etc.)
        continue

    return instances, uf


# ----------------------------
# Analysis
# ----------------------------

@dataclass
class HFNReport:
    net: str
    fanout: int
    drivers: int
    downstream_cells: int
    downstream_cells_with_other_hfn: int
    downstream_other_hfn_pins: int

    cone_depth: int
    cone_cells: int
    cone_cells_with_hfn_input: int
    cone_hfns_seen: int

def build_connectivity(instances: List[Instance], uf: UnionFind):
    """
    Build canonicalized net -> loads/drivers and instance netlists.
    """
    inst_map: Dict[str, Instance] = {i.name: i for i in instances}

    net_loads: Dict[str, List[PinRef]] = defaultdict(list)   # net -> pins that read it
    net_drivers: Dict[str, List[PinRef]] = defaultdict(list) # net -> pins that drive it
    inst_inputs: Dict[str, List[str]] = {}
    inst_outputs: Dict[str, List[str]] = {}

    def canon(n: str) -> str:
        return uf.find(n)

    for inst in instances:
        ins = [canon(n) for n in inst.inputs]
        outs = [canon(n) for n in inst.outputs]
        inst_inputs[inst.name] = ins
        inst_outputs[inst.name] = outs

        # Loads
        for n in ins:
            net_loads[n].append(PinRef(inst=inst.name, cell=inst.cell, port="IN"))
        # Drivers
        for n in outs:
            net_drivers[n].append(PinRef(inst=inst.name, cell=inst.cell, port="OUT"))

    return inst_map, inst_inputs, inst_outputs, net_loads, net_drivers

def compute_hfn_reports(
    instances: List[Instance],
    uf: UnionFind,
    threshold: int,
    depth: int,
    top: int,
    ignore_nets: Optional[Iterable[str]] = None,
    auto_ignore_clk: bool = True,
    hop_report_depth: int = 0,
    ignore_undriven: bool = False,
) -> Tuple[List[HFNReport], Dict[str, List[dict]], List[str]]:
    inst_map, inst_inputs, inst_outputs, net_loads, net_drivers = build_connectivity(instances, uf)

    # Fanout is number of load pins (instance input pins) on the net
    fanout = {n: len(loads) for n, loads in net_loads.items()}

    ignore_patterns = list(ignore_nets or [])

    def is_ignored(net: str) -> bool:
        return any(fnmatch.fnmatchcase(net, pat) for pat in ignore_patterns)

    def base_name(net: str) -> str:
        b = net.rsplit("/", 1)[-1]
        if b.startswith("\\"):
            b = b[1:]
        return b

    def is_auto_ignored(net: str) -> bool:
        b = base_name(net)
        if not auto_ignore_clk:
            return False
        if b.lower() == "clk":
            return True
        # Patterns: anything/clknet_* or /clknet_leaf_* (case-insensitive)
        bl = b.lower()
        return bl.startswith("clknet_")

    hfn_set: Set[str] = {
        n for n, fo in fanout.items()
        if fo >= threshold and not is_ignored(n) and not is_auto_ignored(n)
    }

    warnings: List[str] = []

    # Optionally drop undriven HFNs up front so they don't appear in cones.
    undriven_hfns: Set[str] = {n for n in hfn_set if len(net_drivers.get(n, [])) == 0}
    if ignore_undriven:
        hfn_set -= undriven_hfns

    # Sort HFNs by fanout
    hfn_sorted = sorted(hfn_set, key=lambda n: fanout.get(n, 0), reverse=True)
    if top > 0:
        hfn_sorted = hfn_sorted[:top]

    # Only consider HFNs that survive top-slicing for all subsequent stats.
    sliced_hfn_set: Set[str] = set(hfn_sorted)

    # Warnings for removed nets
    def fmt_list_trunc(items: List[str], limit: int = 8) -> str:
        if len(items) <= limit:
            return ", ".join(items)
        return ", ".join(items[:limit]) + f", ... (+{len(items) - limit} more)"

    if ignore_undriven and undriven_hfns:
        removed = sorted(undriven_hfns)
        warnings.append(f"[warn] undriven HFNs removed ({len(removed)}): " + fmt_list_trunc(removed))

    # Warn about auto-ignored clocks that met threshold but were filtered
    clock_filtered = [n for n, fo in fanout.items() if fo >= threshold and is_auto_ignored(n)]
    if clock_filtered:
        warnings.append(f"[warn] clock-like HFNs auto-removed ({len(clock_filtered)}): " + fmt_list_trunc(sorted(clock_filtered)))

    reports: List[HFNReport] = []
    hop_details: Dict[str, List[dict]] = {}

    # Helper: compute cone stats with BFS in the instance graph
    # Edges: net -> load instances, instance -> any connected nets (inputs/outputs)
    def cone_stats(start_net: str, max_depth: int, hop_depth: int) -> Tuple[int,int,int,List[dict]]:
        """
        Returns (cone_cells, cone_cells_with_hfn_input, cone_hfns_seen, hop_list)
        hop_list is per-depth detail if want_hops else [].
        """
        if max_depth <= 0:
            return (0, 0, 0, [])
        want_hops = hop_depth > 0
        hop_depth = min(hop_depth, max_depth)

        visited_insts: Set[str] = set()
        q = deque()
        # seed: instances that load start_net at depth 1
        for pin in net_loads.get(start_net, []):
            q.append((pin.inst, 1))
        # also seed any drivers of start_net (one hop away via the same net)
        for pin in net_drivers.get(start_net, []):
            q.append((pin.inst, 1))

        cone_cells_with_hfn_input = 0
        hfns_seen: Set[str] = set()
        per_depth_insts: Dict[int, Set[str]] = defaultdict(set) if want_hops else {}
        per_depth_hfn_insts: Dict[int, Set[str]] = defaultdict(set) if want_hops else {}
        per_depth_hfn_to_insts: Dict[int, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set)) if want_hops else {}

        while q:
            inst, d = q.popleft()
            if inst in visited_insts:
                continue
            visited_insts.add(inst)

            ins = inst_inputs.get(inst, [])
            inst_hfns = list({n for n in ins if n in sliced_hfn_set})  # dedup HFNs per instance
            if inst_hfns:
                cone_cells_with_hfn_input += 1
                for n in inst_hfns:
                    hfns_seen.add(n)
            if want_hops and d <= hop_depth:
                per_depth_insts[d].add(inst)
                if inst_hfns:
                    per_depth_hfn_insts[d].add(inst)
                    for n in inst_hfns:
                        per_depth_hfn_to_insts[d][n].add(inst)

            if d >= max_depth:
                continue

            # expand via any non-ignored net connected to this instance (inputs and outputs)
            connected_nets = []
            for net in inst_inputs.get(inst, []):
                if is_ignored(net) or is_auto_ignored(net):
                    continue
                connected_nets.append(net)
            for net in inst_outputs.get(inst, []):
                if is_ignored(net) or is_auto_ignored(net):
                    continue
                connected_nets.append(net)
            for net in connected_nets:
                for pin in net_loads.get(net, []):
                    if pin.inst not in visited_insts:
                        q.append((pin.inst, d + 1))
                for pin in net_drivers.get(net, []):
                    if pin.inst not in visited_insts:
                        q.append((pin.inst, d + 1))

        hop_list: List[dict] = []
        if want_hops:
            for depth_idx in range(1, hop_depth + 1):
                insts_at_depth = per_depth_insts.get(depth_idx, set())
                hfn_map = per_depth_hfn_to_insts.get(depth_idx, {})
                hfns_sorted = sorted(
                    ((hfn, len(insts)) for hfn, insts in hfn_map.items()),
                    key=lambda kv: (-kv[1], kv[0])
                )
                hop_list.append({
                    "depth": depth_idx,
                    "insts": len(insts_at_depth),
                    "insts_with_hfn": len(per_depth_hfn_insts.get(depth_idx, set())),
                    "hfns_seen": hfns_sorted,
                })

        return (len(visited_insts), cone_cells_with_hfn_input, len(hfns_seen), hop_list)

    for n in hfn_sorted:
        loads = net_loads.get(n, [])
        drivers = net_drivers.get(n, [])

        downstream_insts = {p.inst for p in loads}
        downstream_cells = len(downstream_insts)

        with_other_hfn = 0
        other_hfn_pins_total = 0

        for inst in downstream_insts:
            ins = inst_inputs.get(inst, [])
            # other HFN inputs besides the start net (restricted to sliced set)
            other_hfns = [x for x in ins if x in sliced_hfn_set and x != n]
            if other_hfns:
                with_other_hfn += 1
                other_hfn_pins_total += len(other_hfns)

        cone_cells, cone_cells_with_hfn_input, cone_hfns_seen, hop_list = cone_stats(n, depth, hop_report_depth)

        reports.append(HFNReport(
            net=n,
            fanout=fanout.get(n, 0),
            drivers=len(drivers),
            downstream_cells=downstream_cells,
            downstream_cells_with_other_hfn=with_other_hfn,
            downstream_other_hfn_pins=other_hfn_pins_total,
            cone_depth=depth,
            cone_cells=cone_cells,
            cone_cells_with_hfn_input=cone_cells_with_hfn_input,
            cone_hfns_seen=cone_hfns_seen,
        ))
        if hop_report_depth > 0:
            hop_details[n] = hop_list
        if len(drivers) == 0:
            if not ignore_undriven:
                warnings.append(f"[warn] undriven HFN: {n} (fanout={fanout.get(n, 0)}, ds_cells={downstream_cells})")

    # final sort by downstream cells (primary) then fanout (secondary) descending
    reports.sort(key=lambda r: (r.downstream_cells, r.fanout), reverse=True)
    # re-sort hop_details to match final report order
    ordered_hop_details = {r.net: hop_details.get(r.net, []) for r in reports}
    return reports, ordered_hop_details, warnings


# ----------------------------
# Output formatting
# ----------------------------

def print_human_summary(instances: List[Instance], reports: List[HFNReport], threshold: int, depth: int, pre_warnings: List[str]):
    total_insts = len(instances)
    for w in pre_warnings:
        eprint(w)
    eprint(f"[info] instances parsed: {total_insts}")
    eprint(f"[info] HFN threshold (fanout >= {threshold})")
    eprint(f"[info] reporting HFNs: {len(reports)} (already top-sliced if --top used)")
    eprint(f"[info] cone depth: {depth} instance-hops\n")

    # Brief column guide to help interpret the table.
    print("Column guide:")
    print("  NET          : canonicalized net name (aliases collapsed)")
    print("  FO           : fanout load pins on the net")
    print("  DRV          : driver pins on the net")
    print("  DS_CELLS     : downstream instances that load the net")
    print("  DS_W/OTH     : those downstream insts that also see another HFN input (after top slice)")
    print("  OTH_PINS     : count of those other HFN pins on the downstream insts")
    print("  CONE_C       : instances reachable within depth hops in fanout cone")
    print("  CONE_C_HFN   : of CONE_C, instances with any HFN input")
    print("  CONE_HFNS    : distinct HFNs seen in that cone (after top slice)\n")

    # Pretty table-ish output
    header = (
        f"{'NET':<40} {'FO':>6} {'DRV':>4} "
        f"{'DS_CELLS':>9} {'DS_W/OTH':>9} {'OTH_PINS':>9} "
        f"{'CONE_C':>7} {'CONE_C_HFN':>11} {'CONE_HFNS':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in reports:
        net_disp = (r.net[:37] + "...") if len(r.net) > 40 else r.net
        print(
            f"{net_disp:<40} {r.fanout:>6} {r.drivers:>4} "
            f"{r.downstream_cells:>9} {r.downstream_cells_with_other_hfn:>9} {r.downstream_other_hfn_pins:>9} "
            f"{r.cone_cells:>7} {r.cone_cells_with_hfn_input:>11} {r.cone_hfns_seen:>9}"
        )

def print_hop_report(reports: List[HFNReport], hop_details: Dict[str, List[dict]], hop_depth: int, max_hfns_display: int = 8):
    if hop_depth <= 0:
        return
    print(f"\nPer-hop detail (depth <= {hop_depth}):")
    for r in reports:
        hops = hop_details.get(r.net, [])
        if not hops:
            continue
        print(f"\n{r.net} (FO={r.fanout}, DS_CELLS={r.downstream_cells})")
        print("depth  insts  insts_with_HFN  HFNs_seen")
        for h in hops:
            hfns = h.get("hfns_seen", [])
            disp_parts = []
            for net, cnt in hfns[:max_hfns_display]:
                disp_parts.append(f"{net}({cnt})")
            if len(hfns) > max_hfns_display:
                disp_parts.append(f"... (+{len(hfns) - max_hfns_display} more)")
            disp = ", ".join(disp_parts)
            print(f"{h['depth']:>5} {h['insts']:>7} {h['insts_with_hfn']:>15}  {disp}")

def write_json(path: str, reports: List[HFNReport], meta: dict, hop_details: Optional[Dict[str, List[dict]]] = None):
    obj = {
        "meta": meta,
        "hfns": [r.__dict__ for r in reports],
    }
    if hop_details:
        obj["hop_details"] = hop_details
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)

def write_csv(path: str, reports: List[HFNReport]):
    cols = list(HFNReport.__annotations__.keys())
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(cols) + "\n")
        for r in reports:
            row = []
            d = r.__dict__
            for c in cols:
                v = d[c]
                # minimal CSV escaping
                s = str(v)
                if "," in s or '"' in s:
                    s = '"' + s.replace('"', '""') + '"'
                row.append(s)
            f.write(",".join(row) + "\n")


# ----------------------------
# Main
# ----------------------------

def main():
    help_epilog = """\
What this script does (synthesis-aware summary):
  - Reads a structural / gate-level Verilog netlist (post-synth, flattened or hierarchical).
  - Collapses simple assign aliases (union-find), scopes names as <module>/<inst> to avoid cross-module collisions.
  - Infers port directions (library JSON if given, else name heuristics).
  - Computes fanout per net (load pins), flags nets with FO >= --threshold after optional clock/ignore filtering.
  - Reports downstream reach (DS_CELLS) and a cone walk (--depth) that expands across any connected nets
    but skips ignored/clock nets so CTS clocks don’t short-circuit the search.
  - Optional per-hop breakdown (--hop-report) shows, at each hop, how many instances appear and which HFNs
    they touch (with instance counts per HFN).

Assumptions & limits:
  - Input is structural (instances + wires); behavioral constructs beyond simple assigns are ignored.
  - Port directions are best-effort; supply a portdir JSON for accurate stdcell/IP pin maps.
  - No timing/physical modeling; this is a connectivity/fanout lint to spot nets needing buffering/replication.
  - Cone traversal is logical connectivity only; clocks are auto-filtered (base name clk/clknet_*) unless disabled.
  - Use --ignore-undriven to drop undriven HFNs entirely (otherwise a warning is emitted).

Typical uses:
  - Pre-CTS sanity: find data/control HFNs that may need buffering or logic duplication.
  - IP integration: verify that config/bus broadcasts are intentional.
  - Debug floating HFNs: undriven nets with large DS_CELLS often indicate missing tie-offs.
"""
    ap = argparse.ArgumentParser(
        description="Find High Fanout Nets (HFNs) in a structural Verilog netlist.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=help_epilog,
    )
    ap.add_argument("--verilog", required=True, help="Input verilog netlist (.v)")
    ap.add_argument("--threshold", type=int, default=15, help="HFN fanout threshold (default: 15)")
    ap.add_argument("--depth", type=int, default=3, help="Transitive fanout cone depth in instance-hops (default: 3)")
    ap.add_argument("--top", type=int, default=50, help="Report top N HFNs by fanout (0 means all) (default: 50)")
    ap.add_argument("--portdir-json", default=None, help="Optional JSON mapping cell-> {inputs:[..], outputs:[..]}")
    ap.add_argument("--out", default=None, help="Write JSON report to this path")
    ap.add_argument("--csv", default=None, help="Write CSV report to this path")
    ap.add_argument(
        "--ignore-net",
        action="append",
        default=None,
        help="Net name or glob pattern to ignore as HFN candidate (e.g. foo/bar, */clk_i). Repeatable.",
    )
    ap.add_argument("--hop-report", type=int, default=3, help="Show per-hop stats up to this depth (0=off) (default: 3)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--auto-ignore-clk", dest="auto_ignore_clk", action="store_true", help="Auto-ignore nets whose base name is CLK/clk (default)")
    g.add_argument("--no-auto-ignore-clk", dest="auto_ignore_clk", action="store_false", help="Do not auto-ignore CLK-named nets")
    ap.set_defaults(auto_ignore_clk=True)
    ap.add_argument("--ignore-undriven", action="store_true", default=False, help="Drop HFNs that have zero drivers (no warning emitted for them) (default: on)")
    args = ap.parse_args()

    portdir_map = None
    if args.portdir_json:
        with open(args.portdir_json, "r", encoding="utf-8") as f:
            portdir_map = json.load(f)
        if not isinstance(portdir_map, dict):
            raise ValueError("--portdir-json must be a JSON object at top level")

    instances, uf = parse_verilog_netlist(args.verilog, portdir_map=portdir_map)
    reports, hop_details, warnings = compute_hfn_reports(
        instances=instances,
        uf=uf,
        threshold=args.threshold,
        depth=args.depth,
        top=args.top,
        ignore_nets=args.ignore_net or [],
        auto_ignore_clk=args.auto_ignore_clk,
        hop_report_depth=args.hop_report,
        ignore_undriven=args.ignore_undriven,
    )

    meta = {
        "verilog": args.verilog,
        "threshold": args.threshold,
        "depth": args.depth,
        "top": args.top,
        "instances_parsed": len(instances),
        "hop_report_depth": args.hop_report,
    }

    # warnings already collected pre-table
    print_human_summary(instances, reports, args.threshold, args.depth, warnings)
    print_hop_report(reports, hop_details, args.hop_report)

    if args.out:
        write_json(args.out, reports, meta, hop_details if args.hop_report > 0 else None)
        eprint(f"\n[info] wrote JSON: {args.out}")
    if args.csv:
        write_csv(args.csv, reports)
        eprint(f"[info] wrote CSV: {args.csv}")

if __name__ == "__main__":
    main()
