# Vastu Integration for OpenROAD-flow-scripts

## What this does

This replaces RTLMP's built-in simulated annealing macro placer with vastu's hierarchical sequence-pair floorplanner. The key idea: RTLMP is good at clustering (grouping macros and standard cells from the RTL hierarchy), but its per-level SA placement can be improved by vastu's Stockmeyer-style hierarchical SP+SA, which jointly optimizes shape selection and placement across hierarchy levels.

The integration splits RTLMP in half. Normally RTLMP does:

```
1. runMultilevelAutoclustering()    — build physical hierarchy from RTL
2. runCoarseShaping()               — bottom-up shape functions for clusters
3. runHierarchicalMacroPlacement()  — top-down SP+SA at each level
4. commitMacroPlacement()           — write placements back to ODB
```

We keep step 1 (clustering) and replace steps 2-4 with vastu. The data flows through a JSON file so that the Python floorplanner is decoupled from the C++ EDA engine.

## The flow

```
                         ORFS Makefile
                              |
           make DESIGN_CONFIG=... floorplan
                              |
                              v
                 ┌─────────────────────────┐
                 │  Stage 1: Synthesis      │  Yosys
                 │  Input:  RTL (.v), SDC   │
                 │  Output: 1_synth.odb     │  gate-level netlist in ODB
                 └────────────┬────────────┘
                              │
                              v
                 ┌─────────────────────────┐
                 │  Stage 2_1: Floorplan    │  OpenROAD
                 │  Input:  1_synth.odb     │
                 │  Does:   die/core area,  │  initialize_floorplan
                 │          routing tracks,  │  make_tracks
                 │          IO constraints   │  repair_tie_fanout
                 │  Output: 2_1_floorplan.odb
                 └────────────┬────────────┘
                              │
        ┌─────────────────────┴──────────────────────┐
        │  Stage 2_2: Macro Placement                │
        │  THIS IS WHERE VASTU REPLACES RTLMP        │
        │                                            │
        │  Controlled by:                            │
        │    MACRO_PLACEMENT_TCL = macro_place_vastu.tcl
        │                                            │
        │  ┌──────────────────────────────────────┐  │
        │  │  Step A: dump_cluster_tree            │  │
        │  │  Engine: RTLMP C++ (inside OpenROAD)  │  │
        │  │                                       │  │
        │  │  Reads:  2_1_floorplan.odb            │  │
        │  │          (netlist, macros, IO pins,    │  │
        │  │           die area — all in ODB)       │  │
        │  │                                       │  │
        │  │  Does:   runMultilevelAutoclustering() │  │
        │  │          - map RTL hierarchy to        │  │
        │  │            physical hierarchy          │  │
        │  │          - merge small peer clusters   │  │
        │  │            by connection signature     │  │
        │  │          - dissolve oversized clusters  │  │
        │  │            via TritonPart min-cut       │  │
        │  │          - group macros by footprint   │  │
        │  │          - bundle IO pins by die edge  │  │
        │  │          - add virtual connections     │  │
        │  │            for timing (register hops)  │  │
        │  │                                       │  │
        │  │  Writes: cluster_tree.json            │  │
        │  │          (to $OBJECTS_DIR/)            │  │
        │  └──────────────┬───────────────────────┘  │
        │                 │                          │
        │                 │  cluster_tree.json       │
        │                 │  contains:               │
        │                 │  - cluster hierarchy     │
        │                 │    (id, parent, children) │
        │                 │  - cluster types          │
        │                 │    (stdcell/macro/mixed)  │
        │                 │  - metrics per cluster   │
        │                 │    (std_cell_area,        │
        │                 │     macro_area, counts)   │
        │                 │  - inter-cluster          │
        │                 │    connection weights     │
        │                 │  - macro details          │
        │                 │    (w, h, halo, orient)   │
        │                 │  - leaf std-cell names   │
        │                 │  - IO pin positions      │
        │                 │  - die/core dimensions   │
        │                 │  - dbu_per_micron        │
        │                 │                          │
        │                 v                          │
        │  ┌──────────────────────────────────────┐  │
        │  │  Step B: vastu floorplan-clusters     │  │
        │  │  Engine: Python (in isolated venv)    │  │
        │  │  Called via: run_vastu.sh             │  │
        │  │                                       │  │
        │  │  Reads:  cluster_tree.json            │  │
        │  │                                       │  │
        │  │  Does:                                │  │
        │  │  1. cluster_import.py parses JSON     │  │
        │  │     and builds FloorplanProblem:      │  │
        │  │     - macro clusters -> HierBlock     │  │
        │  │       (inner SA tiles the macros)     │  │
        │  │     - stdcell clusters -> SoftBlock   │  │
        │  │       (area = cell_area / util)       │  │
        │  │     - mixed clusters -> HierBlock     │  │
        │  │       (macros + soft cell block)      │  │
        │  │     - connections -> weighted Nets    │  │
        │  │     - die size -> outline constraint  │  │
        │  │                                       │  │
        │  │  2. solve_hierarchical() runs:        │  │
        │  │     Bottom-up:                        │  │
        │  │       For each HierarchicalBlock,     │  │
        │  │       solve inner problem at K        │  │
        │  │       aspect-ratio targets to build   │  │
        │  │       a Pareto (w,h) shape curve.     │  │
        │  │     Current level:                    │  │
        │  │       SP+SA over all blocks.          │  │
        │  │       Moves: swap in Γ+/Γ−, rotate,  │  │
        │  │       perturb soft AR, pick hier      │  │
        │  │       shape. Cost: HPWL + outline.    │  │
        │  │     Top-down:                         │  │
        │  │       Commit chosen shapes, re-solve  │  │
        │  │       each inner problem at that      │  │
        │  │       outline.                        │  │
        │  │                                       │  │
        │  │  3. tcl_writer.py converts result     │  │
        │  │     to OpenROAD Tcl commands:         │  │
        │  │     - place_macro for macros          │  │
        │  │     - setLocation/setPlacementStatus  │  │
        │  │       for std-cell seed positions     │  │
        │  │                                       │  │
        │  │  Writes: vastu_placement.tcl          │  │
        │  │          (to $OBJECTS_DIR/)            │  │
        │  │          optional: floorplan.png      │  │
        │  └──────────────┬───────────────────────┘  │
        │                 │                          │
        │                 │  vastu_placement.tcl     │
        │                 │  contains:               │
        │                 │  - place_macro commands  │
        │                 │    (x, y, orientation    │
        │                 │     for each macro)      │
        │                 │  - setLocation +         │
        │                 │    setPlacementStatus    │
        │                 │    for each std cell     │
        │                 │    (seed positions that  │
        │                 │     GPL starts from)     │
        │                 │                          │
        │                 v                          │
        │  ┌──────────────────────────────────────┐  │
        │  │  Step C: source vastu_placement.tcl   │  │
        │  │  Engine: OpenROAD Tcl interpreter     │  │
        │  │                                       │  │
        │  │  Executes:                            │  │
        │  │  - place_macro sets macro locations   │  │
        │  │    in ODB (dbInst::setLocation +      │  │
        │  │    setPlacementStatus FIRM)            │  │
        │  │  - setLocation + setPlacementStatus   │  │
        │  │    PLACED on each std cell — GPL      │  │
        │  │    reads these as initial positions    │  │
        │  │    (isPlaced() returns true) and       │  │
        │  │    starts from the clustered layout    │  │
        │  │                                       │  │
        │  │  Output: 2_2_floorplan_macro.odb      │  │
        │  └──────────────────────────────────────┘  │
        └────────────────────┬───────────────────────┘
                              │
                              v
                 ┌─────────────────────────┐
                 │  Stage 2_3: Tapcell      │  tapcell insertion
                 │  Stage 2_4: PDN          │  power distribution
                 └────────────┬────────────┘
                              │
                              v
                 ┌─────────────────────────┐
                 │  Stage 3: Placement      │
                 │  3_1: Global placement   │  RePlAce/GPL — respects the
                 │       (skip IO)          │  starts from vastu's seed
                 │  3_2: IO placement       │  positions (clustered layout)
                 │  3_3: Global placement   │
                 │       (with IO)          │
                 │  3_4: Resize + buffer    │
                 │  3_5: Detail placement   │
                 └────────────┬────────────┘
                              │
                              v
                   CTS → Routing → Finish

```

## Why each file is modified

### OpenROAD C++ changes

RTLMP's `HierRTLMP::run()` is a monolithic method that does clustering + shaping + placement + commit in one shot. There's no API to run just the clustering and get the result out. We add one.

**`dump_cluster_tree.cpp`** (new file)

Implements `HierRTLMP::dumpClusterTree(path)`. This calls `runMultilevelAutoclustering()` — the same method that RTLMP's `run()` calls as its first step — then walks the resulting `PhysicalHierarchy` tree and serializes every `Cluster` object to JSON. The tree structure, cluster metrics, inter-cluster connection weights, macro details, IO pin locations, and die dimensions all get written out. For std-cell clusters with >10K instances, the instance name list is written to a separate `.txt` file to keep the JSON manageable.

Why a new file instead of modifying `hier_rtlmp.cpp`: keeps the vastu code isolated. The new file only includes headers that already exist.

**`hier_rtlmp.h`** (patched — 1 line added)

Adds the `void dumpClusterTree(const char*)` declaration to the `HierRTLMP` class. Without this, the new `.cpp` file can't define the method.

**`rtl_mp.h` and `rtl_mp.cpp`** (patched)

`rtl_mp.h`/`.cpp` define `MacroPlacer`, the public-facing factory class that the Tcl layer talks to. We add `MacroPlacer::dumpClusterTree(...)` which mirrors `MacroPlacer::place(...)`: it creates a `HierRTLMP` instance, sets the clustering parameters, and calls `dumpClusterTree()` instead of `run()`.

Why a factory method: the Tcl/SWIG layer can't talk to `HierRTLMP` directly (it's a private implementation class). All OpenROAD Tcl commands go through `MacroPlacer`.

**`mpl.i`** (patched — SWIG binding)

This is the SWIG interface file that generates the C++/Tcl bridge. We add `dump_cluster_tree_cmd()` inside the `%inline` block — a thin wrapper that calls `MacroPlacer::dumpClusterTree()`. SWIG auto-generates the Tcl-callable C function from this.

**`mpl.tcl`** (patched — Tcl command)

Defines the user-facing `dump_cluster_tree` Tcl proc. It parses named arguments (`-max_num_level`, `-output`, etc.), converts them to the right types, and calls `dump_cluster_tree_cmd`. This is the same pattern every OpenROAD Tcl command uses: `.tcl` defines the user-facing proc, `.i` defines the SWIG bridge, `.cpp` implements the logic.

**`CMakeLists.txt`** (patched — 1 line added)

Adds `src/dump_cluster_tree.cpp` to the mpl library's source list so CMake compiles it.

### Vastu Python changes

**`io/cluster_import.py`** (new)

Reads the JSON that `dump_cluster_tree` produces and builds vastu's `FloorplanProblem` tree — the internal representation that the hierarchical solver operates on.

The mapping:
- The root cluster's children become the top-level blocks in the problem.
- Each non-leaf cluster becomes a `HierarchicalBlock` whose `inner_problem` contains its children, recursed.
- Each leaf macro cluster becomes a `HierarchicalBlock` whose `inner_problem` contains one `HardBlock` per macro. Macro dimensions are inflated by halo (the actual macro is smaller than the block vastu packs — the halo is subtracted back when writing Tcl output).
- Each leaf std-cell cluster becomes a `SoftBlock` with area = `std_cell_area / target_utilization`. The utilization factor (default 0.7) inflates the area to account for routing overhead — same concept as RTLMP's `target_util`.
- Each leaf mixed cluster becomes a `HierarchicalBlock` with `HardBlock`s for macros + a `SoftBlock` for the standard-cell portion.
- Inter-cluster connection weights from the JSON become vastu `Net` objects with `weight` set to the RTLMP connection weight. Vastu's HPWL function already multiplies by `net.weight`, so heavily-connected clusters are pulled closer together. Only sibling connections matter at each level — parent-level connectivity is handled by the parent's solve.
- Die dimensions from the JSON become the fixed-outline constraint for the top-level solve.

**`io/tcl_writer.py`** (new)

Converts vastu's hierarchical solve result into Tcl commands that OpenROAD can execute.

For each placed block in the result:
- **Macros**: emit `place_macro -macro_name {inst} -location {x y} -orientation R0`. The halo is subtracted from coordinates because vastu packed with inflated dimensions — the actual macro sits centered within the halo padding.
- **Std-cell clusters**: spread the cluster's member cells uniformly across the cluster's bounding box using a grid, then emit ODB Tcl calls to set each cell's position: `$inst setOrient R0; $inst setLocation x y; $inst setPlacementStatus PLACED`. GPL's initialization code checks `dbInst::isPlaced()` — cells with status PLACED are initialized at their stored position instead of core center. This means GPL starts from a clustered arrangement that preserves vastu's structure.

For clusters with >500 cells, the seed commands are written to a companion `.tcl` file and `source`d from the main output. For clusters whose instances are in a separate file (`leaf_instances_file` in the JSON), the Tcl uses a loop to read names and compute grid positions on the fly.

Note: we don't use `dbRegion` / `dbGroup` for soft regions because GPL ignores INCLUSIVE regions — it only treats them as site blockage, not as "keep cells in" constraints. The seed position approach achieves the same goal (biasing GPL toward cluster-respecting solutions) through a mechanism GPL actually respects.

**`sa/cost.py`** (modified)

Added `fixed_outline: bool` to `CostWeights`. The cost terms:
- `area_weight * total_w * total_h` — packing-bbox area. Non-zero even in fixed-outline mode (drives compactness *inside* the outline, leaves slack).
- `wirelength * HPWL(nets)` — half-perimeter wirelength weighted by per-net weight.
- `outline_penalty * (max(0, W−Wt)^3 + max(0, H−Ht)^3)` — soft overflow term (cubic when `fixed_outline=True`). Mostly redundant now: the annealer's accept rule rejects any move that grows the outline violation (see `sa/annealer.py`), so this is just a tie-breaker when both states are infeasible.
- `overlap_penalty * Σ overlap_with_fixed` — penalizes overlap with pre-placed fixed blocks.

**`sa/annealer.py`** (modified)

The vanilla Metropolis criterion (`accept if Δcost ≤ 0 or rand < exp(−Δ/T)`) was the original solver and could happily wander outside the target outline as long as the cost dropped overall. In `fixed_outline=True` mode, this is now a **hard constraint**:

- `feasible → infeasible`: REJECT (no temperature-dependent escape).
- `infeasible → feasible`: ACCEPT (always).
- `infeasible → infeasible`: smaller-violation always wins; equal violations fall back to Metropolis on cost.
- `feasible → feasible`: standard Metropolis on cost.

The best-state tracker (`_better_than`) ranks feasible > infeasible first, then by violation magnitude, then by cost. This guarantees the final layout fits in the target outline as long as a feasible state was visited.

**`floorplan/hierarchy.py`** (modified)

Top-level solve and every inner cluster solve now both run with `fixed_outline=True` so each level respects its parent's chosen shape. Without this, cluster contents would leak past the parent's bbox at composition time and macros would land outside the core.

`sample_shapes` slack reduced to `1.03` (was `1.15`) so the Pareto shapes for each HierarchicalBlock are tight — committing less empty space when the parent picks a shape.

**Connection extraction & usage**

This is the data flow that turns RTLMP's connection metric into vastu's wirelength signal:

```
   RTLMP cluster engine
   ┌────────────────────────────┐
   │ Each Cluster has a         │   in tools/OpenROAD/src/mpl
   │ ConnectionsMap:            │   ─ Cluster::getConnectionsMap()
   │   {target_id: weight}      │     returns map<int, float> built from
   │                            │     flat netlist traversal (every multi-
   │                            │     pin net contributes weight=1/(fanout)
   │                            │     to each connected cluster pair).
   └─────────────┬──────────────┘
                 │
                 v
   ┌────────────────────────────┐
   │ dump_cluster_tree.cpp      │   for each cluster c:
   │ writes JSON:               │     "connections": {
   │                            │       "<target_id>": weight, …
   │                            │     }
   └─────────────┬──────────────┘
                 │
                 v   (cluster_tree.json)
                 │
                 v
   ┌────────────────────────────┐
   │ vastu/io/cluster_import.py │   _build_nets(child_ids, ...) walks each
   │ -> _build_nets()           │   cluster's "connections" map, dedupes
   │                            │   sibling pairs (min-id, max-id), and
   │                            │   emits one vastu Net per pair:
   │                            │     Net(name="n_{a}_{b}",
   │                            │         pins=[Pin(block=a, side="C"),
   │                            │               Pin(block=b, side="C")],
   │                            │         weight=conn_weight)
   │                            │
   │                            │   Only SIBLING-to-sibling connections at
   │                            │   each level become Nets at that level —
   │                            │   parent-to-parent connectivity is
   │                            │   represented at the parent's level.
   └─────────────┬──────────────┘
                 │
                 v
   ┌────────────────────────────┐
   │ vastu/core/hypergraph.py   │   hpwl(nets, placements) computes
   │ -> hpwl()                  │   Σ_net  net.weight * HPWL(net.pins)
   │                            │   where HPWL is bounding-box half-
   │                            │   perimeter of the net's pin centroids
   │                            │   (`pin_position(pin, placement)`).
   └─────────────┬──────────────┘
                 │
                 v
   ┌────────────────────────────┐
   │ vastu/sa/cost.py -> cost() │   total cost includes
   │                            │     weights.wirelength * hpwl
   │                            │   The SA then minimizes this jointly
   │                            │   with the area/outline/overlap terms.
   └────────────────────────────┘
```

Two corollaries worth noting:

1. **Pin location is the block centre** (`Pin(side="C", frac=0.5)`). HPWL is computed against block centres, not block edges. So vastu's wirelength minimizes centre-to-centre Manhattan-ish distance weighted by connection strength.
2. **IO pin connectivity** is captured via thin fixed-edge blocks (`__io_west` / `__io_east` / `__io_north` / `__io_south` from `_add_io_boundary_blocks`). Each IO pin in the JSON becomes a `Pin` on the appropriate edge block at its true die position; nets between clusters and IO pins then pull clusters toward the right die side automatically.

**`cli.py`** (modified)

Added the `floorplan-clusters` subcommand which wires together: `cluster_import.py` (read JSON) → `solve_hierarchical()` (run SP+SA) → `tcl_writer.py` (write Tcl). Also added `--die-width`/`--die-height` to the existing `floorplan` subcommand for standalone testing with fixed outlines.

### Flow scripts

**`macro_place_vastu.tcl`**

This is the ORFS flow script that orchestrates the three steps. ORFS has a mechanism for replacing the macro placement stage: set `MACRO_PLACEMENT_TCL` in `config.mk` and it gets sourced during stage 2_2 instead of calling `rtl_macro_placer`.

The script:
1. Checks if the design has macros (`find_macros`). If not, skips.
2. Calls `dump_cluster_tree` with clustering parameters from RTLMP env vars.
3. Calls `run_vastu.sh` with the JSON path and vastu parameters from env vars.
4. Sources the output Tcl to load placements and regions into ODB.

**`run_vastu.sh`**

Wrapper that locates the vastu venv and source installed by `setup.sh`, sets `PYTHONPATH`, and calls `vastu.cli.main()` using the venv's Python. This is what makes vastu callable from the ORFS Tcl flow without any system Python dependencies.

## The JSON schema (cluster_tree.json)

This is the interface between the C++ world (OpenROAD/RTLMP) and the Python world (vastu). Every field exists because vastu needs it for floorplanning.

```json
{
  "floorplan": {
    "width": 1000.0,            // core width in microns — becomes vastu's target_w
    "height": 800.0,            // core height — becomes target_h
    "die_area": [0, 0, 1100, 900],
    "core_area": [50, 50, 1050, 850]
  },
  "dbu_per_micron": 1000,       // for coordinate conversion in tcl_writer

  "clusters": [
    {
      "id": 0,                  // unique cluster ID
      "name": "root",           // from RTLMP's cluster naming
      "type": "mixed",          // "stdcell" | "macro" | "mixed"
      "parent": null,           // root has no parent
      "children": [1, 2, 3],    // child cluster IDs — defines the tree
      "std_cell_area": 50000,   // total std cell area in microns^2
      "macro_area": 120000,     // total macro area
      "num_std_cells": 3400,    // cell count
      "num_macros": 12,         // macro count
      "connections": {          // weighted edges to OTHER clusters
        "2": 150.0,             // (includes RTLMP's virtual timing connections)
        "3": 85.0               // these become vastu Net.weight values
      }
    },
    {
      "id": 1,
      "name": "c_mem",
      "type": "macro",          // leaf macro cluster — vastu makes HierarchicalBlock
      "parent": 0,
      "children": [],
      "macros": [               // only present on leaf clusters with macros
        {
          "name": "SRAM_64x32",       // master cell name
          "inst_name": "u_mem/sram_0", // instance name in netlist
          "width": 100.0,              // master dimensions (microns)
          "height": 50.0,
          "fixed": false,              // pre-placed?
          "halo_x": 2.0,              // routing clearance around macro
          "halo_y": 2.0,
          "orientation": "R0"
        }
      ],
      "connections": {"2": 200.0}
    },
    {
      "id": 2,
      "name": "c_logic",
      "type": "stdcell",        // leaf std-cell cluster — vastu makes SoftBlock
      "parent": 0,
      "children": [],
      "std_cell_area": 25000,
      "num_std_cells": 1800,
      "leaf_instances": ["u_alu/g0", "u_alu/g1", ...],  // for region membership
      // or for large clusters:
      // "leaf_instances_file": "cluster_2_insts.txt"
      "connections": {"1": 200.0}
    }
  ],

  "io_pins": [                  // from dbBlock::getBTerms()
    {"name": "clk", "x": 0.0, "y": 400.0, "side": "W", "direction": "input"}
  ],

  "fixed_macros": [             // pre-placed macros (obstacles)
    {"name": "PAD", "inst_name": "u_pad", "x": 0, "y": 0,
     "width": 50, "height": 50, "orientation": "R0"}
  ]
}
```

## The Tcl output (vastu_placement.tcl)

This is what gets sourced back into OpenROAD. Every line is a standard OpenROAD Tcl/ODB command.

```tcl
set block [ord::get_db_block]

# === Macro placements — calls dbInst::setLocation + setPlacementStatus(FIRM) ===
place_macro -macro_name {u_mem/sram_0} -location {198.0 298.0} -orientation R0
place_macro -macro_name {u_mem/sram_1} -location {198.0 350.0} -orientation R0

# === Std-cell seed positions ===
# GPL checks isPlaced() — cells with PLACED status start at their stored
# position instead of core center. This preserves the cluster structure.
# Seed logic_cluster: 1800 cells in box (100,200)-(400,500)
set inst [$block findInst {u_alu/g0}]
if {$inst != "NULL"} { $inst setOrient R0; $inst setLocation 100000 200000; $inst setPlacementStatus PLACED }
set inst [$block findInst {u_alu/g1}]
if {$inst != "NULL"} { $inst setOrient R0; $inst setLocation 115000 200000; $inst setPlacementStatus PLACED }
# ... (cells spread in a grid across the region, coordinates in DBU)
```

---

## Installation

This directory is self-contained. No external dependencies beyond Python 3.10+ and OpenROAD build tools.

```bash
# 1. Run setup — creates venv, installs deps, patches C++, copies scripts
bash <orfs>/vastu_setup/setup.sh

# 2. Rebuild OpenROAD (for the dump_cluster_tree command)
cd <orfs>
source dev_env.sh
./build_openroad.sh --no_init --openroad-args "-DCMAKE_BUILD_TYPE=DEBUG" --local
```

Setup creates `<orfs>/tools/vastu/` containing:
- `venv/` — Python venv with numpy, matplotlib, pyyaml, networkx, pyverilog
- `src/vastu/` — the complete vastu package

The vastu Python side works immediately after step 1. The OpenROAD rebuild is only needed for the `dump_cluster_tree` Tcl command.

## Usage

### Once-only setup

```bash
bash vastu_setup/setup.sh
source dev_env.sh && ./build_openroad.sh --no_init --local --threads $(nproc)
```

`setup.sh` installs the vastu Python package into `tools/vastu/`, patches OpenROAD (C++ + SWIG + Tcl) to expose the new `dump_cluster_tree` command, drops `macro_place_vastu.tcl` + `run_vastu.sh` into `flow/scripts/`, and patches `flow/scripts/macro_place_util.tcl` to honor the `MACRO_PLACEMENT_TCL_FULL` gate (so RTLMP doesn't re-run after vastu). All edits are idempotent — re-running `setup.sh` is safe.

The OpenROAD rebuild is required (the new Tcl command is compiled into the binary). It's an incremental build after the first time.

### Per-design usage

Easiest — pass the placement hook as an env var:

```bash
cd flow
env MACRO_PLACEMENT_TCL=$PWD/scripts/macro_place_vastu.tcl \
    make DESIGN_CONFIG=./designs/nangate45/ariane133/config.mk place
```

Or pin it in the design's `config.mk`:
```makefile
export MACRO_PLACEMENT_TCL = $(FLOW_HOME)/scripts/macro_place_vastu.tcl
```

The hook runs during stage `2_2_floorplan_macro` (`make floorplan` is enough to exercise it; `make place` continues into `gpl`). Vastu writes:
- `objects/<plat>/<design>/<var>/cluster_tree.json` — RTLMP's clustering, vastu's input
- `objects/<plat>/<design>/<var>/vastu_placement.tcl` — `place_macro` + std-cell seeds, sourced back into OpenROAD
- `objects/<plat>/<design>/<var>/vastu_seed_*.tcl` — companion files holding per-cluster seed positions for large clusters
- `reports/<plat>/<design>/<var>/vastu_floorplan.png` — visualization of the layout vastu produced

**Environment variables** (all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `VASTU_MAX_LEVEL` | 3 (floor) | Vastu-specific clustering depth — overrides `RTLMP_MAX_LEVEL`. If the design pins `RTLMP_MAX_LEVEL` < 3 vastu still uses 3, since deeper trees give better hierarchical SP+SA. |
| `RTLMP_MAX_LEVEL` | (none) | Stock RTLMP depth. If set, vastu still floors at 3 (see above). Higher values pass through. |
| `RTLMP_MAX_INST` / `RTLMP_MIN_INST` | RTLMP default | Std cells per cluster bounds. |
| `RTLMP_MAX_MACRO` / `RTLMP_MIN_MACRO` | RTLMP default | Macros per cluster bounds. |
| `MACRO_PLACE_HALO` | design default | Macro halo: `"x y"` (microns per side). |
| `VASTU_SEED` | 42 | SA random seed. |
| `VASTU_WL_WEIGHT` | 1.0 | HPWL cost weight. Reduced from earlier 5.0 so it no longer dominates compactness. |
| `VASTU_AREA_WEIGHT` | 50.0 | Bounding-box-area weight — drives compactness inside the hard outline. Higher = tighter packing. |
| `VASTU_OUTLINE_PENALTY` | 50.0 | Soft-overflow penalty (mostly redundant now — vastu's SA enforces the outline as a hard constraint, see `sa/annealer.py`). |
| `VASTU_UTILIZATION` | 0.7 | Std-cell area inflation factor (`std_cell_area / util` becomes the SoftBlock area). |
| `VASTU_MAX_TEMP_STEPS` | 100 | SA temperature steps. Realistic minimum for ariane133-scale: 300–500. |
| `VASTU_MOVES_PER_TEMP` | 600 | SA moves per temperature. |
| `VASTU_PLOT` | `$REPORTS_DIR/vastu_floorplan.png` | Path for PNG visualization. The flow now always writes a PNG to the reports dir; set this to override the path. |
| `MACRO_PLACEMENT_TCL_FULL` | set by hook | Signals `macro_place_util.tcl` to *skip* `rtl_macro_placer` after sourcing the placement TCL. Vastu's hook sets this automatically; don't set it manually for hand-written placement TCLs that expect RTLMP to fill in unplaced macros. |

## Quick test (no OpenROAD rebuild needed)

```bash
cat > /tmp/test_clusters.json << 'EOF'
{
  "floorplan": {"width": 100, "height": 100, "core_area": [0, 0, 100, 100]},
  "dbu_per_micron": 1000,
  "clusters": [
    {"id": 0, "name": "root", "type": "mixed", "parent": null, "children": [1, 2],
     "std_cell_area": 3000, "macro_area": 2000, "num_std_cells": 500, "num_macros": 4,
     "connections": {}},
    {"id": 1, "name": "mem_cluster", "type": "macro", "parent": 0, "children": [],
     "std_cell_area": 0, "macro_area": 2000, "num_std_cells": 0, "num_macros": 4,
     "macros": [
       {"name": "SRAM", "inst_name": "u_sram_0", "width": 20, "height": 10,
        "fixed": false, "halo_x": 1, "halo_y": 1},
       {"name": "SRAM", "inst_name": "u_sram_1", "width": 20, "height": 10,
        "fixed": false, "halo_x": 1, "halo_y": 1},
       {"name": "SRAM", "inst_name": "u_sram_2", "width": 20, "height": 10,
        "fixed": false, "halo_x": 1, "halo_y": 1},
       {"name": "SRAM", "inst_name": "u_sram_3", "width": 20, "height": 10,
        "fixed": false, "halo_x": 1, "halo_y": 1}
     ],
     "connections": {"2": 50.0}},
    {"id": 2, "name": "logic_cluster", "type": "stdcell", "parent": 0, "children": [],
     "std_cell_area": 3000, "macro_area": 0, "num_std_cells": 500, "num_macros": 0,
     "leaf_instances": ["g0", "g1", "g2"],
     "connections": {"1": 50.0}}
  ],
  "io_pins": [],
  "fixed_macros": []
}
EOF

<orfs>/flow/scripts/run_vastu.sh \
  floorplan-clusters \
  --clusters /tmp/test_clusters.json \
  --out /tmp/test_placement.tcl \
  --plot /tmp/test_floorplan.png \
  --seed 42

cat /tmp/test_placement.tcl
```

## Uninstallation

```bash
rm -rf <orfs>/tools/vastu
cd <orfs>/tools/OpenROAD/src/mpl
for f in src/hier_rtlmp.h src/rtl_mp.cpp src/mpl.i src/mpl.tcl CMakeLists.txt; do
    [ -f "$f.bak" ] && mv "$f.bak" "$f"
done
[ -f include/mpl/rtl_mp.h.bak ] && mv include/mpl/rtl_mp.h.bak include/mpl/rtl_mp.h
rm -f src/dump_cluster_tree.cpp
rm -f <orfs>/flow/scripts/macro_place_vastu.tcl <orfs>/flow/scripts/run_vastu.sh
cd <orfs> && source dev_env.sh && ./build_openroad.sh --no_init --local
```
