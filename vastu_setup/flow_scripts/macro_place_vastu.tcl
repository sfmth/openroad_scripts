# macro_place_vastu.tcl — alternative macro placement using vastu
#
# Activated by setting in config.mk:
#   export MACRO_PLACEMENT_TCL = $(FLOW_HOME)/scripts/macro_place_vastu.tcl
#
# Flow: RTLMP clustering -> JSON -> vastu SP+SA -> Tcl placement commands
#
# Vastu is installed at tools/vastu/ by setup.sh. The run_vastu.sh wrapper
# sets PYTHONPATH automatically.

if { [find_macros] != "" } {
  puts "=== Vastu macro placement ==="

  # Tell macro_place_util.tcl to skip rtl_macro_placer after we run.
  # Vastu fully replaces the SA placement; the result is handed off to GPL.
  set ::env(MACRO_PLACEMENT_TCL_FULL) 1

  # --- Step 1: Run RTLMP clustering only, dump to JSON ---
  set cluster_json "$::env(OBJECTS_DIR)/cluster_tree.json"
  file mkdir [file dirname $cluster_json]

  set clustering_args ""
  # Vastu thrives on a deep cluster tree (it solves recursively per level).
  # Designs typically set RTLMP_MAX_LEVEL=1 because the stock SA placer
  # works best flat; for vastu we want a real hierarchy.
  # Precedence: VASTU_MAX_LEVEL > RTLMP_MAX_LEVEL > default 3.
  if { [info exists ::env(VASTU_MAX_LEVEL)] } {
    append clustering_args " -max_num_level $::env(VASTU_MAX_LEVEL)"
  } elseif { [info exists ::env(RTLMP_MAX_LEVEL)] } {
    # If the design pins RTLMP_MAX_LEVEL to a small value (typical: 1) bump
    # the floor to 3 so vastu sees a usable hierarchy. Larger values pass
    # through unchanged.
    set lvl $::env(RTLMP_MAX_LEVEL)
    if { $lvl < 3 } { set lvl 3 }
    append clustering_args " -max_num_level $lvl"
  } else {
    append clustering_args " -max_num_level 3"
  }
  if { [info exists ::env(RTLMP_MAX_INST)] } {
    append clustering_args " -max_num_inst $::env(RTLMP_MAX_INST)"
  }
  if { [info exists ::env(RTLMP_MIN_INST)] } {
    append clustering_args " -min_num_inst $::env(RTLMP_MIN_INST)"
  }
  if { [info exists ::env(RTLMP_MAX_MACRO)] } {
    append clustering_args " -max_num_macro $::env(RTLMP_MAX_MACRO)"
  }
  if { [info exists ::env(RTLMP_MIN_MACRO)] } {
    append clustering_args " -min_num_macro $::env(RTLMP_MIN_MACRO)"
  }

  if { [info exists ::env(MACRO_PLACE_HALO)] } {
    lassign $::env(MACRO_PLACE_HALO) halo_x halo_y
    append clustering_args " -halo_width $halo_x -halo_height $halo_y"
  }

  append clustering_args " -output $cluster_json"

  puts "Running RTLMP clustering: dump_cluster_tree $clustering_args"
  log_cmd dump_cluster_tree {*}$clustering_args

  # --- Step 2: Run vastu on the cluster tree ---
  set vastu_out "$::env(OBJECTS_DIR)/vastu_placement.tcl"

  # run_vastu.sh is installed alongside this script by setup.sh.
  set vastu_runner "$::env(FLOW_HOME)/scripts/run_vastu.sh"
  set vastu_args "floorplan-clusters --clusters $cluster_json --out $vastu_out"

  if { [info exists ::env(VASTU_SEED)] } {
    append vastu_args " --seed $::env(VASTU_SEED)"
  } else {
    append vastu_args " --seed 42"
  }
  if { [info exists ::env(VASTU_WL_WEIGHT)] } {
    append vastu_args " --wirelength-weight $::env(VASTU_WL_WEIGHT)"
  }
  if { [info exists ::env(VASTU_OUTLINE_PENALTY)] } {
    append vastu_args " --outline-penalty $::env(VASTU_OUTLINE_PENALTY)"
  }
  if { [info exists ::env(VASTU_UTILIZATION)] } {
    append vastu_args " --target-utilization $::env(VASTU_UTILIZATION)"
  }
  if { [info exists ::env(VASTU_MAX_TEMP_STEPS)] } {
    append vastu_args " --max-temp-steps $::env(VASTU_MAX_TEMP_STEPS)"
  }
  if { [info exists ::env(VASTU_MOVES_PER_TEMP)] } {
    append vastu_args " --moves-per-temp $::env(VASTU_MOVES_PER_TEMP)"
  }
  # Always dump a visual of the placement to the design's reports dir so it
  # ships with the rest of the per-stage artifacts. VASTU_PLOT env overrides.
  if { [info exists ::env(VASTU_PLOT)] } {
    set vastu_plot $::env(VASTU_PLOT)
  } elseif { [info exists ::env(REPORTS_DIR)] } {
    set vastu_plot "$::env(REPORTS_DIR)/vastu_floorplan.png"
  } else {
    set vastu_plot "$::env(OBJECTS_DIR)/vastu_floorplan.png"
  }
  file mkdir [file dirname $vastu_plot]
  append vastu_args " --plot $vastu_plot"

  puts "Running vastu: $vastu_runner $vastu_args"
  # -ignorestderr: matplotlib + setuptools may emit harmless UserWarnings on
  # stderr; Tcl's exec would otherwise raise even when the subprocess exit is 0.
  exec -ignorestderr bash $vastu_runner {*}$vastu_args 2>@stderr

  # --- Step 3: Load placements and regions back into OpenROAD ---
  puts "Loading vastu results from $vastu_out"
  source $vastu_out

  puts "=== Vastu macro placement complete ==="
} else {
  puts "No macros found: Skipping vastu macro placement"
}
