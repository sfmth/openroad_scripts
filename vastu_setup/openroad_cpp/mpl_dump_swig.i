// Vastu integration: SWIG binding for dump_cluster_tree.
// Append this to tools/OpenROAD/src/mpl/src/mpl.i inside the %inline %{ ... %} block.

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
