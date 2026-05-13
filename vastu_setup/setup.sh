#!/usr/bin/env bash
# setup.sh — Install vastu integration into OpenROAD-flow-scripts.
#
# Everything needed is inside this vastu_setup/ directory.
# No external dependencies required (beyond Python 3.10+ and C++ build tools).
#
# The script creates a self-contained Python virtual environment at
# tools/vastu/ with all dependencies installed, so vastu runs without
# touching the system Python or requiring any pre-installed packages.
#
# Run from anywhere:
#   bash /path/to/vastu_setup/setup.sh
#
# What it does:
#   1. Creates a Python venv at tools/vastu/venv/ and installs dependencies
#   2. Installs the bundled vastu package into tools/vastu/src/
#   3. Copies new C++ files into OpenROAD's mpl module
#   4. Patches existing OpenROAD C++ files (with .bak backups)
#   5. Installs ORFS flow scripts
#   6. Prints rebuild instructions

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[setup]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
err() { echo -e "${RED}[error]${NC} $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ORFS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VASTU_BUNDLE="$SCRIPT_DIR/vastu_src"
VASTU_INSTALL="$ORFS_ROOT/tools/vastu"
MPL_SRC="$ORFS_ROOT/tools/OpenROAD/src/mpl/src"
MPL_INCLUDE="$ORFS_ROOT/tools/OpenROAD/src/mpl/include/mpl"
FLOW_SCRIPTS="$ORFS_ROOT/flow/scripts"

log "ORFS root:     $ORFS_ROOT"
log "Vastu bundle:  $VASTU_BUNDLE"
log "Vastu install: $VASTU_INSTALL"
log ""

# --- Sanity checks ---
if [ ! -d "$VASTU_BUNDLE/vastu" ]; then
    err "Vastu source bundle not found at $VASTU_BUNDLE/vastu"
    err "The vastu_setup directory appears incomplete."
    exit 1
fi
if [ ! -d "$MPL_SRC" ]; then
    err "OpenROAD mpl source not found at $MPL_SRC"
    err "Make sure the OpenROAD submodule is initialized:"
    err "  cd $ORFS_ROOT && git submodule update --init --recursive"
    exit 1
fi

# Find a working Python >= 3.10
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major="${ver%%.*}"
        minor="${ver#*.}"
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$(command -v "$candidate")"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    err "Python >= 3.10 not found on PATH."
    err "Install Python 3.10+ and ensure it's on your PATH."
    exit 1
fi
log "Using Python: $PYTHON ($($PYTHON --version))"
log ""

# ============================================================================
# 1. Create Python venv and install dependencies
# ============================================================================
log "Setting up Python virtual environment at $VASTU_INSTALL/venv ..."

mkdir -p "$VASTU_INSTALL"

if [ ! -f "$VASTU_INSTALL/venv/bin/python" ]; then
    "$PYTHON" -m venv "$VASTU_INSTALL/venv"
    log "  Created venv"
else
    log "  Venv already exists"
fi

VENV_PIP="$VASTU_INSTALL/venv/bin/pip"
VENV_PYTHON="$VASTU_INSTALL/venv/bin/python"

log "  Installing Python dependencies..."
"$VENV_PIP" install --quiet --upgrade pip 2>&1 | tail -1 || true
"$VENV_PIP" install --quiet -r "$VASTU_BUNDLE/requirements.txt" 2>&1 | tail -3
log "  Dependencies installed"

# ============================================================================
# 2. Install vastu source package
# ============================================================================
log "Installing vastu source to $VASTU_INSTALL/src/ ..."

mkdir -p "$VASTU_INSTALL/src"
rm -rf "$VASTU_INSTALL/src/vastu"
cp -r "$VASTU_BUNDLE/vastu" "$VASTU_INSTALL/src/vastu"
log "  Installed $(find "$VASTU_INSTALL/src/vastu" -name '*.py' | wc -l) Python files"

# Verify it works
"$VENV_PYTHON" -c "
import sys
sys.path.insert(0, '$VASTU_INSTALL/src')
from vastu.cli import main
print('  vastu import OK')
" || { err "vastu import failed — check Python dependencies"; exit 1; }

# ============================================================================
# 3. OpenROAD C++ files — new source
# ============================================================================
log "Installing OpenROAD C++ additions..."

cp "$SCRIPT_DIR/openroad_cpp/dump_cluster_tree.cpp" "$MPL_SRC/dump_cluster_tree.cpp"
log "  Installed dump_cluster_tree.cpp"

# ============================================================================
# 4. Patches to existing OpenROAD files
# ============================================================================
log "Patching OpenROAD files..."

# --- hier_rtlmp.h ---
if ! grep -q "dumpClusterTree" "$MPL_SRC/hier_rtlmp.h"; then
    sed -i.bak '/void blockMacroChannels();/a\
\
  // Vastu integration: dump cluster tree to JSON after autoclustering.\
  void dumpClusterTree(const char* output_path);' "$MPL_SRC/hier_rtlmp.h"
    log "  Patched hier_rtlmp.h"
else
    warn "  hier_rtlmp.h already patched — skipping"
fi

# --- rtl_mp.h ---
if ! grep -q "dumpClusterTree" "$MPL_INCLUDE/rtl_mp.h"; then
    sed -i.bak '/void blockMacroChannels();/a\
  void dumpClusterTree(int max_num_macro, int min_num_macro,\
                       int max_num_inst, int min_num_inst,\
                       float tolerance, int max_num_level,\
                       float coarsening_ratio, int large_net_threshold,\
                       const char* output_path);' "$MPL_INCLUDE/rtl_mp.h"
    log "  Patched rtl_mp.h"
else
    warn "  rtl_mp.h already patched — skipping"
fi

# --- rtl_mp.cpp ---
if ! grep -q "dumpClusterTree" "$MPL_SRC/rtl_mp.cpp"; then
    cat >> "$MPL_SRC/rtl_mp.cpp" << 'CPPEOF'

// --- Vastu integration: dumpClusterTree factory method ---
void MacroPlacer::dumpClusterTree(const int max_num_macro,
                                  const int min_num_macro,
                                  const int max_num_inst,
                                  const int min_num_inst,
                                  const float tolerance,
                                  const int max_num_level,
                                  const float coarsening_ratio,
                                  const int large_net_threshold,
                                  const char* output_path)
{
  hier_rtlmp_->init();
  hier_rtlmp_->setClusterSize(
      max_num_macro, min_num_macro, max_num_inst, min_num_inst);
  hier_rtlmp_->setClusterSizeTolerance(tolerance);
  hier_rtlmp_->setMaxNumLevel(max_num_level);
  hier_rtlmp_->setClusterSizeRatioPerLevel(coarsening_ratio);
  hier_rtlmp_->setLargeNetThreshold(large_net_threshold);

  hier_rtlmp_->dumpClusterTree(output_path);
}
CPPEOF
    log "  Patched rtl_mp.cpp"
else
    warn "  rtl_mp.cpp already patched — skipping"
fi

# --- mpl.i ---
if ! grep -q "dump_cluster_tree_cmd" "$MPL_SRC/mpl.i"; then
    "$VENV_PYTHON" -c "
with open('$MPL_SRC/mpl.i') as f:
    content = f.read()
insertion = '''
void
dump_cluster_tree_cmd(const int max_num_macro,
                      const int min_num_macro,
                      const int max_num_inst,
                      const int min_num_inst,
                      const float tolerance,
                      const int max_num_level,
                      const float coarsening_ratio,
                      const int large_net_threshold,
                      const char* output_path)
{
  auto macro_placer = getMacroPlacer();
  macro_placer->dumpClusterTree(max_num_macro,
                                min_num_macro,
                                max_num_inst,
                                min_num_inst,
                                tolerance,
                                max_num_level,
                                coarsening_ratio,
                                large_net_threshold,
                                output_path);
}

'''
last_close = content.rfind('%}')
if last_close == -1:
    print('ERROR: could not find closing percent-brace in mpl.i')
    exit(1)
new_content = content[:last_close] + insertion + content[last_close:]
with open('$MPL_SRC/mpl.i', 'w') as f:
    f.write(new_content)
"
    log "  Patched mpl.i (SWIG binding)"
else
    warn "  mpl.i already patched — skipping"
fi

# --- mpl.tcl ---
if ! grep -q "dump_cluster_tree" "$MPL_SRC/mpl.tcl"; then
    cat "$SCRIPT_DIR/openroad_cpp/mpl_dump.tcl" >> "$MPL_SRC/mpl.tcl"
    log "  Patched mpl.tcl (Tcl command)"
else
    warn "  mpl.tcl already patched — skipping"
fi

# --- CMakeLists.txt ---
CMAKE_FILE="$ORFS_ROOT/tools/OpenROAD/src/mpl/CMakeLists.txt"
if ! grep -q "dump_cluster_tree.cpp" "$CMAKE_FILE"; then
    sed -i.bak '/hier_rtlmp.cpp/a\  src/dump_cluster_tree.cpp' "$CMAKE_FILE"
    log "  Patched CMakeLists.txt"
else
    warn "  CMakeLists.txt already patched — skipping"
fi

# ============================================================================
# 5. ORFS flow scripts
# ============================================================================
log "Installing ORFS flow scripts..."
cp "$SCRIPT_DIR/flow_scripts/macro_place_vastu.tcl" "$FLOW_SCRIPTS/macro_place_vastu.tcl"
log "  Installed macro_place_vastu.tcl"
cp "$SCRIPT_DIR/flow_scripts/run_vastu.sh" "$FLOW_SCRIPTS/run_vastu.sh"
chmod +x "$FLOW_SCRIPTS/run_vastu.sh"
log "  Installed run_vastu.sh"

# ============================================================================
# 6. Summary
# ============================================================================
echo ""
log "============================================"
log "Setup complete!"
log ""
log "  Python venv:      $VASTU_INSTALL/venv/"
log "  Vastu source:     $VASTU_INSTALL/src/vastu/ ($(find "$VASTU_INSTALL/src/vastu" -name '*.py' | wc -l) files)"
log "  Venv Python:      $VENV_PYTHON"
log ""
log "  OpenROAD C++:     $MPL_SRC/dump_cluster_tree.cpp (+ 6 patched files)"
log "  Flow scripts:     $FLOW_SCRIPTS/macro_place_vastu.tcl"
log "                    $FLOW_SCRIPTS/run_vastu.sh"
log ""
log "============================================"
log ""
log "Next steps:"
log ""
log "  1. Rebuild OpenROAD (for the dump_cluster_tree Tcl command):"
log "     cd $ORFS_ROOT"
log "     source dev_env.sh"
log "     ./build_openroad.sh --no_init --openroad-args \"-DCMAKE_BUILD_TYPE=DEBUG\" --local"
log ""
log "  2. Quick test (no rebuild needed):"
log "     $FLOW_SCRIPTS/run_vastu.sh floorplan-clusters --clusters test.json --out test.tcl"
log ""
log "  3. Use in ORFS flow (add to design config.mk):"
log "     export MACRO_PLACEMENT_TCL = \$(FLOW_HOME)/scripts/macro_place_vastu.tcl"
log ""
