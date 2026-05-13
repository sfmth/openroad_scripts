#!/usr/bin/env bash
# run_vastu.sh — invoke vastu using the self-contained venv + bundled source.
#
# Called by macro_place_vastu.tcl during the ORFS flow.
# Can also be called directly for standalone testing.
# All arguments are passed through to vastu's CLI.
#
# The venv and source are installed by setup.sh at:
#   <orfs>/tools/vastu/venv/    — Python virtual environment with all deps
#   <orfs>/tools/vastu/src/     — vastu source package
#
# Locates the ORFS root by walking up from FLOW_HOME or this script's location.

set -euo pipefail

# Find ORFS root
if [ -n "${FLOW_HOME:-}" ]; then
    ORFS_ROOT="$(cd "$FLOW_HOME/.." && pwd)"
elif [ -n "${ORFS_ROOT:-}" ]; then
    ORFS_ROOT="$(cd "$ORFS_ROOT" && pwd)"
else
    # Assume this script is at <orfs>/flow/scripts/run_vastu.sh
    ORFS_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fi

VASTU_DIR="$ORFS_ROOT/tools/vastu"
VENV_PYTHON="$VASTU_DIR/venv/bin/python"
VASTU_SRC="$VASTU_DIR/src"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "ERROR: vastu venv not found at $VENV_PYTHON" >&2
    echo "Run vastu_setup/setup.sh first." >&2
    exit 1
fi

if [ ! -d "$VASTU_SRC/vastu" ]; then
    echo "ERROR: vastu source not found at $VASTU_SRC/vastu" >&2
    echo "Run vastu_setup/setup.sh first." >&2
    exit 1
fi

exec "$VENV_PYTHON" -c "
import sys
sys.path.insert(0, '$VASTU_SRC')
from vastu.cli import main
sys.exit(main())
" "$@"
