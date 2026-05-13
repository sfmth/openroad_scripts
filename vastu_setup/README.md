# Vastu Integration for OpenROAD-flow-scripts

This directory is entirely self-contained. It bundles the complete vastu hierarchical floorplanner, creates its own Python virtual environment with all dependencies, and patches OpenROAD to add the `dump_cluster_tree` command. Nothing is installed outside the ORFS tree. No system Python packages are required.

After running `setup.sh`, RTLMP's multilevel autoclustering can be exported to a JSON file, consumed by vastu's hierarchical SP+SA floorplanner, and the resulting macro placements + soft regions loaded back into OpenROAD for downstream placement, CTS, and routing.

## Directory layout

```
vastu_setup/                            # Everything needed — self-contained
├── setup.sh                            # Installer — run this
├── README.md                           # This file
│
├── vastu_src/
│   ├── requirements.txt                # Python deps (numpy, matplotlib, etc.)
│   └── vastu/                          # Complete vastu package (pre-modified)
│       ├── cli.py                      #   CLI with floorplan-clusters subcommand
│       ├── sa/cost.py                  #   Cost function with fixed_outline flag
│       ├── floorplan/hierarchy.py      #   Hierarchical solver with fixed-outline mode
│       ├── io/cluster_import.py        #   RTLMP cluster JSON reader (new)
│       ├── io/tcl_writer.py            #   OpenROAD Tcl output writer (new)
│       └── ...                         #   All other vastu modules (unchanged)
│
├── openroad_cpp/
│   ├── dump_cluster_tree.cpp           # New C++ source for cluster JSON export
│   ├── mpl_dump.tcl                    # New Tcl proc definition
│   ├── rtl_mp_dump.cpp                 # Reference: factory method (inlined by setup.sh)
│   └── mpl_dump_swig.i                # Reference: SWIG binding (inlined by setup.sh)
│
├── flow_scripts/
│   ├── macro_place_vastu.tcl           # ORFS macro placement stage replacement
│   └── run_vastu.sh                    # Wrapper: uses venv Python, sets PYTHONPATH
│
└── patches/                            # Reference diffs (setup.sh uses sed instead)
    ├── hier_rtlmp_h.patch
    └── rtl_mp_h.patch

After setup.sh runs, the ORFS tree gains:

<orfs>/
├── tools/vastu/
│   ├── venv/                           # Python virtual environment (created by setup.sh)
│   │   └── bin/python                  # Isolated Python with numpy, matplotlib, etc.
│   └── src/vastu/                      # Vastu source (copied from vastu_src/)
├── tools/OpenROAD/src/mpl/src/
│   └── dump_cluster_tree.cpp           # New C++ file (+ patches to 6 existing files)
└── flow/scripts/
    ├── macro_place_vastu.tcl           # Flow script
    └── run_vastu.sh                    # Vastu launcher
```

## Prerequisites

- **Python 3.10+** on PATH (only needed to create the venv — the venv is self-contained after that)
- **C++ build tools** for OpenROAD (CMake, GCC/Clang, SWIG, Tcl headers)

That's it. No `pip install` on the system. No external vastu clone. No pre-installed Python packages.

## Installation

```bash
# 1. Run setup (creates venv, installs deps, patches C++ files, copies scripts)
bash <orfs>/vastu_setup/setup.sh

# 2. Rebuild OpenROAD (for the dump_cluster_tree Tcl command)
cd <orfs>
source dev_env.sh
./build_openroad.sh --no_init --openroad-args "-DCMAKE_BUILD_TYPE=DEBUG" --local
```

Step 1 takes ~30 seconds (venv creation + pip install). Step 2 is the normal OpenROAD build.

The vastu Python side works immediately after step 1 — the OpenROAD rebuild is only needed for the `dump_cluster_tree` Tcl command.

---

## What setup.sh does

The script is idempotent — safe to run multiple times. Patched files are backed up as `.bak`. The venv is reused if it already exists.

### Step 1: Create Python venv and install dependencies

- Finds Python >= 3.10 on PATH (tries python3.12, python3.11, python3.10, python3)
- Creates `<orfs>/tools/vastu/venv/` via `python -m venv`
- Runs `pip install -r requirements.txt` inside the venv
- Dependencies: numpy, matplotlib, pyyaml, networkx, pyverilog
- Nothing touches system Python or system site-packages

### Step 2: Install vastu source

- Copies `vastu_src/vastu/` to `<orfs>/tools/vastu/src/vastu/`
- Verifies the import works: `from vastu.cli import main`

### Step 3: Copy new C++ source

- `dump_cluster_tree.cpp` -> `tools/OpenROAD/src/mpl/src/`
- Implements `HierRTLMP::dumpClusterTree()`: runs RTLMP autoclustering, serializes cluster tree to JSON

### Step 4: Patch existing C++ files

| File | What's added |
|------|-------------|
| `hier_rtlmp.h` | `dumpClusterTree()` declaration |
| `rtl_mp.h` | Factory method declaration |
| `rtl_mp.cpp` | Factory method implementation |
| `mpl.i` | SWIG binding for Tcl |
| `mpl.tcl` | `dump_cluster_tree` Tcl proc |
| `CMakeLists.txt` | `dump_cluster_tree.cpp` in source list |

### Step 5: Install flow scripts

- `macro_place_vastu.tcl` -> `<orfs>/flow/scripts/`
- `run_vastu.sh` -> `<orfs>/flow/scripts/`

---

## How it runs

`run_vastu.sh` is the entry point for running vastu. It:
1. Locates `<orfs>/tools/vastu/` (via `FLOW_HOME` or script location)
2. Uses `tools/vastu/venv/bin/python` (the isolated venv Python with all deps)
3. Sets `PYTHONPATH` to `tools/vastu/src/` (the bundled vastu source)
4. Calls `vastu.cli.main()` with all arguments passed through

The ORFS flow script (`macro_place_vastu.tcl`) calls `run_vastu.sh`. For standalone testing, you can call it directly.

---

## Vastu modifications (vs. upstream)

| File | Change |
|------|--------|
| `sa/cost.py` | `fixed_outline: bool` on `CostWeights` — cubic penalty (`ow^3`) for outline violations |
| `floorplan/hierarchy.py` | `target_w`/`target_h` args on `solve_hierarchical()` — fixed-outline mode for top level |
| `cli.py` | `floorplan-clusters` subcommand + `--die-width`/`--die-height`/`--outline-penalty` flags |
| `io/cluster_import.py` | **New.** RTLMP cluster JSON -> vastu `FloorplanProblem` |
| `io/tcl_writer.py` | **New.** Vastu result -> `place_macro` + `create_region` Tcl |

---

## CLI reference

```
run_vastu.sh floorplan-clusters \
  --clusters FILE            # RTLMP cluster tree JSON (required)
  --out FILE                 # Output Tcl for OpenROAD (required)
  --seed N                   # SA random seed (default: 0)
  --die-width MICRONS        # Override die width from JSON
  --die-height MICRONS       # Override die height from JSON
  --wirelength-weight F      # HPWL weight (default: 5.0)
  --outline-penalty F        # Outline violation weight (default: 50.0)
  --target-utilization F     # Std-cell area inflation (default: 0.7)
  --max-temp-steps N         # SA temperature steps (default: 100)
  --moves-per-temp N         # SA moves per temperature (default: 600)
  --plot FILE                # Save PNG visualization
  --plot-size F              # Plot size in inches (default: 10.0)
```

---

## ORFS flow integration

Add to your design's `config.mk`:
```makefile
export MACRO_PLACEMENT_TCL = $(FLOW_HOME)/scripts/macro_place_vastu.tcl
```

**Environment variables** (all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `RTLMP_MAX_LEVEL` | RTLMP default | Cluster hierarchy depth |
| `RTLMP_MAX_INST` | RTLMP default | Max std cells per cluster |
| `RTLMP_MIN_INST` | RTLMP default | Min std cells per cluster |
| `RTLMP_MAX_MACRO` | RTLMP default | Max macros per cluster |
| `RTLMP_MIN_MACRO` | RTLMP default | Min macros per cluster |
| `MACRO_PLACE_HALO` | design default | Macro halo: `"x y"` |
| `VASTU_SEED` | 42 | SA random seed |
| `VASTU_WL_WEIGHT` | 5.0 | Wirelength cost weight |
| `VASTU_OUTLINE_PENALTY` | 50.0 | Outline violation weight |
| `VASTU_UTILIZATION` | 0.7 | Std-cell area inflation |
| `VASTU_MAX_TEMP_STEPS` | 100 | SA temperature steps |
| `VASTU_MOVES_PER_TEMP` | 600 | SA moves per temperature |
| `VASTU_PLOT` | (none) | Path to save PNG |

---

## Quick test

After running `setup.sh` (no OpenROAD rebuild needed):

```bash
# Create test JSON
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

# Run using run_vastu.sh (uses the venv — no system deps needed)
<orfs>/flow/scripts/run_vastu.sh \
  floorplan-clusters \
  --clusters /tmp/test_clusters.json \
  --out /tmp/test_placement.tcl \
  --plot /tmp/test_floorplan.png \
  --seed 42

cat /tmp/test_placement.tcl
```

## Full ORFS test (requires OpenROAD rebuild)

```bash
cd <orfs>/flow
make DESIGN_CONFIG=./designs/nangate45/ariane133/config.mk \
     MACRO_PLACEMENT_TCL=./scripts/macro_place_vastu.tcl \
     floorplan
```

## Uninstallation

```bash
# Remove vastu (venv + source)
rm -rf <orfs>/tools/vastu

# Restore patched C++ files
cd <orfs>/tools/OpenROAD/src/mpl
for f in src/hier_rtlmp.h src/rtl_mp.cpp src/mpl.i src/mpl.tcl CMakeLists.txt; do
    [ -f "$f.bak" ] && mv "$f.bak" "$f"
done
[ -f include/mpl/rtl_mp.h.bak ] && mv include/mpl/rtl_mp.h.bak include/mpl/rtl_mp.h
rm -f src/dump_cluster_tree.cpp

# Remove flow scripts
rm -f <orfs>/flow/scripts/macro_place_vastu.tcl
rm -f <orfs>/flow/scripts/run_vastu.sh

# Rebuild OpenROAD
cd <orfs>
source dev_env.sh
./build_openroad.sh --no_init --openroad-args "-DCMAKE_BUILD_TYPE=DEBUG" --local
```
