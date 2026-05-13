# Vastu integration: Tcl command for dump_cluster_tree.
# Append this to tools/OpenROAD/src/mpl/src/mpl.tcl

sta::define_cmd_args "dump_cluster_tree" {
  [-max_num_macro max_num_macro]
  [-min_num_macro min_num_macro]
  [-max_num_inst max_num_inst]
  [-min_num_inst min_num_inst]
  [-tolerance tolerance]
  [-max_num_level max_num_level]
  [-coarsening_ratio coarsening_ratio]
  [-large_net_threshold large_net_threshold]
  [-halo_width halo_width]
  [-halo_height halo_height]
  -output output_path
}

proc dump_cluster_tree { args } {
  sta::parse_key_args "dump_cluster_tree" args \
    keys {
      -max_num_macro -min_num_macro
      -max_num_inst -min_num_inst
      -tolerance
      -max_num_level -coarsening_ratio
      -large_net_threshold
      -halo_width -halo_height
      -output
    } \
    flags {}

  if { ![info exists keys(-output)] } {
    utl::error MPL 997 "-output is required for dump_cluster_tree"
  }

  # Defaults matching rtl_macro_placer
  set max_num_macro [expr {[info exists keys(-max_num_macro)] ? $keys(-max_num_macro) : 0}]
  set min_num_macro [expr {[info exists keys(-min_num_macro)] ? $keys(-min_num_macro) : 0}]
  set max_num_inst  [expr {[info exists keys(-max_num_inst)]  ? $keys(-max_num_inst)  : 0}]
  set min_num_inst  [expr {[info exists keys(-min_num_inst)]  ? $keys(-min_num_inst)  : 0}]
  set tolerance     [expr {[info exists keys(-tolerance)]     ? $keys(-tolerance)     : 0.1}]
  set max_num_level [expr {[info exists keys(-max_num_level)] ? $keys(-max_num_level) : 2}]
  set coarsening_ratio [expr {[info exists keys(-coarsening_ratio)] ? $keys(-coarsening_ratio) : 10.0}]
  set large_net_threshold [expr {[info exists keys(-large_net_threshold)] ? $keys(-large_net_threshold) : 50}]

  if { [info exists keys(-halo_width)] && [info exists keys(-halo_height)] } {
    set halo_w $keys(-halo_width)
    set halo_h $keys(-halo_height)
    mpl::set_macro_base_halo_cmd \
      [ord::microns_to_dbu $halo_w] \
      [ord::microns_to_dbu $halo_h] \
      [ord::microns_to_dbu $halo_w] \
      [ord::microns_to_dbu $halo_h]
  }

  mpl::dump_cluster_tree_cmd \
    $max_num_macro \
    $min_num_macro \
    $max_num_inst \
    $min_num_inst \
    $tolerance \
    $max_num_level \
    $coarsening_ratio \
    $large_net_threshold \
    $keys(-output)
}
