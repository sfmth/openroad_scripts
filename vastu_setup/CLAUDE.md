# vastu_setup — agent context

`vastu_setup/` installs an experimental macro placer ("vastu") into this ORFS
checkout. The README describes the intent; this file records what actually
broke and the fixes that got applied so the next agent doesn't repeat the
debugging path.

## What it does

`setup.sh` is a self-contained installer that:

1. Creates a Python venv at `tools/vastu/venv/` and installs the Python deps
   from `vastu_src/requirements.txt`.
2. Copies the bundled vastu Python source to `tools/vastu/src/vastu/`.
3. Patches the OpenROAD `mpl` module (C++ + SWIG + Tcl) to add a new
   `dump_cluster_tree` command — runs RTLMP's autoclustering only, writes the
   cluster tree to JSON.
4. Installs `flow/scripts/macro_place_vastu.tcl` (the ORFS hook) and
   `flow/scripts/run_vastu.sh` (the venv-wrapper that invokes vastu's CLI).
5. Patches `flow/scripts/macro_place_util.tcl` (added by us — see
   "MACRO_PLACEMENT_TCL_FULL" below).

## The intended flow

```
ORFS make floorplan
  └─ 2_2_floorplan_macro stage
       └─ macro_place_util.tcl
            └─ source $MACRO_PLACEMENT_TCL  (=macro_place_vastu.tcl)
                 ├─ dump_cluster_tree  -> cluster_tree.json   (OpenROAD C++)
                 ├─ run_vastu.sh floorplan-clusters …         (Python venv)
                 │    └─ load JSON, SP+SA, write vastu_placement.tcl
                 └─ source vastu_placement.tcl                (back into OR)
                      └─ 132x place_macro …
            (rtl_macro_placer is SKIPPED because vastu sets
             MACRO_PLACEMENT_TCL_FULL=1)
```

## Bugs found and fixed during initial bring-up

These are baked into `vastu_setup/` so re-running `setup.sh` produces a
working install. Six independent bugs across C++, Tcl, and Python:

| # | File (in vastu_setup) | Bug | Fix |
|---|---|---|---|
| 1 | `setup.sh` (rtl_mp.cpp patch) | `cat >>` appended `MacroPlacer::dumpClusterTree` *after* the `}  // namespace mpl` closing brace → "`MacroPlacer` has not been declared" | Replaced `cat >>` with a Python patch that inserts the block *before* the last `}  // namespace mpl` |
| 2 | `openroad_cpp/dump_cluster_tree.cpp` | `HierRTLMP::dumpClusterTree` never called `setGlobalFence`, so `tree_->global_fence` stayed default `Rect(0,0,0,0)` and `setFloorplanShape()` computed core ∩ empty = empty → MPL-0065 "movable cells do not fit" | Added `setGlobalFence(odb::Rect())` at the top — empty rect triggers `setGlobalFence`'s fallback to core area |
| 3 | `openroad_cpp/mpl_dump.tcl` | Called `mpl::set_macro_base_halo_cmd` (doesn't exist) with DBU ints | Switched to `mpl::set_base_halo` with micron floats (matches mpl.i:135 signature) |
| 4 | `setup.sh` + `flow_scripts/macro_place_vastu.tcl` | ORFS's `macro_place_util.tcl` unconditionally runs `rtl_macro_placer` after sourcing `MACRO_PLACEMENT_TCL`. With vastu's std-cell seeds + unplaced macros, RTLMP SEGV'd in `mergeChildrenBelowThresholds`. The README claimed "instead of" but ORFS has always been "in addition to" | Vastu's hook sets `set ::env(MACRO_PLACEMENT_TCL_FULL) 1`. `setup.sh` patches `macro_place_util.tcl` to honor that flag and skip `rtl_macro_placer` |
| 5 | `vastu_src/vastu/io/tcl_writer.py` | `_leaf_name(path) = path.rsplit('/', 1)[-1]` couldn't recover macro `inst_name` because the path is `<cluster_name>/<inst_name>` and *both halves contain `/`*. Result: 0 `place_macro` lines emitted | Replaced with progressive suffix-match: walk slash-suffixes of the path until one matches a known macro `inst_name`. Now emits all 132 |
| 6 | `vastu_src/vastu/io/cluster_import.py` + `tcl_writer.py` | JSON's `halo_x`/`halo_y` are **total** (left+right, top+bottom — see `dump_cluster_tree.cpp` summing `base_halo_.left + base_halo_.right`). Python treated them as **per-side**, inflating blocks 2× and offsetting macros 2× — wasted space + suboptimal packing | `cluster_import`: `w = w + halo_x_total` (not `2*halo_x`). `tcl_writer`: `actual_x = pl.x + halo_x_total/2` (per-side offset) |

After (1)–(6): vastu runs end-to-end and emits 132 valid `place_macro` calls.

## The remaining issue, and the fix in `vastu/sa/`

Even after fixing emission, vastu's SP+SA solver produced layouts that
**exceeded the fixed outline target** for ariane133 (146 blocks total,
top-level outline 1191.3×1190.0 μm). Empirically:

```
vastu: solved 146 blocks, outline 1343.6×1335.5 (target 1191.3×1190.0)
```

→ OpenROAD's `place_macro` then rejects with MPL-0034 "outside of the core".

Root cause: the cost function in `vastu/sa/cost.py` treats outline overflow
as a **soft penalty** (`outline_penalty * (ow^3 + oh^3)` in fixed_outline
mode). The Metropolis accept rule could happily accept moves that exit the
feasibility region, especially at high temperatures, and the solver settled
in a local minimum with the outline 13% too big.

Fix applied to `vastu_src/vastu/sa/annealer.py`:

1. Added `_outline_violation()`: total μm of overflow in the (w, h) target.
2. Added `_better_than()`: best-tracking comparator — feasibility beats
   infeasibility, smaller violation beats larger, then cost tie-breaks.
3. Rewrote the SA accept rule in `anneal()`:
   - **Feasible → infeasible: HARD REJECT** (never leave feasibility)
   - **Infeasible → feasible: HARD ACCEPT** (always enter feasibility)
   - **Both infeasible**: prefer smaller violation; if equal, Metropolis on cost
   - **Both feasible**: regular Metropolis on cost (outline penalty term is
     now effectively redundant but harmless inside the feasible region)
4. Warmup is unchanged (still does an unconstrained random walk to calibrate
   T), but its best-tracking uses `_better_than` so the best feasible state
   it visits is preserved.

Additionally, in `vastu/floorplan/hierarchy.py`, `inner_weights` now sets
`fixed_outline=True`. Each cluster's inner SA must respect the cluster's
chosen (w, h) shape as a hard outline too — otherwise inner macros leak
past the cluster boundary even when the top level is feasible.

`sample_shapes` is intentionally left with soft outline: it's exploring
Pareto shapes for a cluster, not committing to one.

## Coordinate-origin fix (`tcl_writer.py`)

After hard outline got macros inside vastu's `(0, 0) → (core_w, core_h)`
box, the next failure was **PDN-0179 "Unable to repair all channels"**:
vastu emitted `place_macro` at die-absolute coords matching its own
`(0, 0)`, but OpenROAD's core lower-left is `(core_x_min, core_y_min)`
(e.g. `(5.13, 5.6)` on nangate45). Macros at vastu's `(0, 0)` landed at
die-absolute `(8, 8)` — only 2.87 μm from the core edge — leaving sub-μm
channels that PDN couldn't fill with power stripes.

`tcl_writer.py` now reads `floorplan.core_area` from the JSON and adds
`core_x_min` / `core_y_min` to every emitted `place_macro` location.
Result: vastu's `(0, 0)` maps to OpenROAD's `(core_x_min, core_y_min)`
and the halo width (8 μm) is preserved against the core boundary.

After this fix, `make floorplan` runs end-to-end:
`1_synth → 2_1_floorplan → 2_2_floorplan_macro (vastu, 132 place_macro)
→ 2_3_floorplan_tapcell → 2_4_floorplan_pdn`.

## Layout dump + compactness

After bring-up, the plot looked bad: cluttered with full-path leaf labels and
spread out as 10 wide horizontal strips because the SA was minimizing HPWL
without any compactness pressure. Three follow-up changes:

1. **Auto-dump PNG to `$REPORTS_DIR`** (`macro_place_vastu.tcl`). The vastu
   hook now always passes `--plot $REPORTS_DIR/vastu_floorplan.png`. The env
   var `VASTU_PLOT` overrides if set.
2. **Suppress leaf labels** in the cluster-floorplan plot
   (`vastu/viz/plot.py` + `vastu/cli.py`). The new `show_leaf_labels=False`
   keyword drops the per-macro labels (they're long hierarchical RTL paths
   and just overlap into illegible noise); the outer cluster labels stay.
3. **Compactness via `area_weight`** (`vastu/cli.py`,
   `vastu/floorplan/hierarchy.py`). The fixed-outline mode used to force
   `area_weight=0.0` in `top_weights`, leaving HPWL as the only driving
   force. SA happily spread blocks all the way to the outline. Now:
   - CLI default `--area-weight 10`, `--wirelength-weight 1` (was 5).
   - `solve_hierarchical` passes `weights.area_weight` through to
     `top_weights` instead of zeroing it.
   The cost `area_weight * total_w * total_h` inside the hard outline
   directly drives compactness — the SA now minimizes the bbox of placed
   blocks, leaving slack inside the outline.

### Bonus fix — Tcl exec & matplotlib stderr

Tcl's `exec` treats *any* stderr output from a subprocess as a fatal error.
matplotlib's `tight_layout` emits a benign `UserWarning` on stderr when
label boxes overflow margins, which crashed the whole flow even though
vastu's exit code was 0. Two layers of defense:
- `macro_place_vastu.tcl` uses `exec -ignorestderr ... 2>@stderr`.
- `vastu/viz/plot.py` wraps `tight_layout()` in `warnings.catch_warnings()`
  to suppress the `UserWarning` at the source.

## File map (vastu_setup tree)

```
vastu_setup/
├── README.md              # Original docs (still partly out of date on flow)
├── CLAUDE.md              # THIS FILE
├── setup.sh               # Installer — see "Bugs found and fixed" rows 1, 4
├── openroad_cpp/
│   ├── dump_cluster_tree.cpp   # row 2 (setGlobalFence)
│   ├── mpl_dump.tcl            # row 3 (set_base_halo)
│   ├── mpl_dump_swig.i         # SWIG binding for dump_cluster_tree_cmd
│   └── rtl_mp_dump.cpp         # (unused, kept for reference)
├── flow_scripts/
│   ├── macro_place_vastu.tcl   # row 4 (sets MACRO_PLACEMENT_TCL_FULL=1)
│   └── run_vastu.sh            # venv wrapper
├── patches/                # plain .patch files (informational, not used by setup.sh)
└── vastu_src/
    ├── requirements.txt
    └── vastu/
        ├── io/
        │   ├── cluster_import.py   # row 6 (halo math)
        │   └── tcl_writer.py       # rows 5, 6 (suffix match + halo math)
        ├── sa/
        │   └── annealer.py         # hard-outline SA accept rule
        └── floorplan/
            └── hierarchy.py        # inner_weights.fixed_outline=True
```

## Operational notes

- The vastu hook bumps `VASTU_MAX_TEMP_STEPS` / `VASTU_MOVES_PER_TEMP` via
  env vars at runtime if you need more SA budget. Defaults: 100 / 600.
- The OpenROAD C++ patches require a rebuild after `setup.sh`:
  `./build_openroad.sh --no_init --local --threads N` from the ORFS root.
  Tcl-only changes (e.g., further edits to `mpl_dump.tcl`) ALSO require a
  rebuild because `mpl.tcl` is compiled into the binary (CMake `SCRIPTS` arg).
- The flow scripts (`flow/scripts/*.tcl`) do not require a rebuild.
- Designs known to use `MACRO_PLACEMENT_TCL` for manual placements
  (`nangate45/black_parrot`, `ihp-sg13g2/i2c-gpio-expander`) are unaffected:
  they don't set `MACRO_PLACEMENT_TCL_FULL`, so `rtl_macro_placer` still runs
  after their TCL — preserving the existing "manual seed + RTLMP fills in"
  behavior.

## Reference design

`nangate45/ariane133` — 132 SRAM macros (`fakeram45_256x16`), 155k std
cells, hierarchical synthesis. The config has pre-tuned `RTLMP_MAX_LEVEL`
etc. exposed via env vars, which `macro_place_vastu.tcl` forwards to
`dump_cluster_tree`. Used as the test design throughout bring-up.

Run command (from `flow/`):
```
env MACRO_PLACEMENT_TCL=$PWD/scripts/macro_place_vastu.tcl \
    make DESIGN_CONFIG=./designs/nangate45/ariane133/config.mk floorplan
```
