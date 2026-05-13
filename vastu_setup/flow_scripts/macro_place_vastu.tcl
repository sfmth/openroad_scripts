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

  # --- Step 1: Run RTLMP clustering only, dump to JSON ---
  set cluster_json "$::env(OBJECTS_DIR)/cluster_tree.json"
  file mkdir [file dirname $cluster_json]

  set clustering_args ""
  if { [info exists ::env(RTLMP_MAX_LEVEL)] } {
    append clustering_args " -max_num_level $::env(RTLMP_MAX_LEVEL)"
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
  if { [info exists ::env(VASTU_PLOT)] } {
    append vastu_args " --plot $::env(VASTU_PLOT)"
  }

  puts "Running vastu: $vastu_runner $vastu_args"
  exec bash $vastu_runner {*}$vastu_args

  # --- Step 3: Load placements and regions back into OpenROAD ---
  puts "Loading vastu results from $vastu_out"
  source $vastu_out

  puts "=== Vastu macro placement complete ==="
} else {
  puts "No macros found: Skipping vastu macro placement"
}
