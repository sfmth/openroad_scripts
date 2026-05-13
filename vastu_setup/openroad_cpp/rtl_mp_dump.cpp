// SPDX-License-Identifier: BSD-3-Clause
// Vastu integration: MacroPlacer::dumpClusterTree() factory method.
//
// This code should be appended to tools/OpenROAD/src/mpl/src/rtl_mp.cpp
// (or the method added to the existing file via patch).

#include "mpl/rtl_mp.h"
#include "hier_rtlmp.h"

namespace mpl {

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
  hier_rtlmp_->setNumThreads(ord::OpenRoad::openRoad()->getThreadCount());

  hier_rtlmp_->dumpClusterTree(output_path);
}

}  // namespace mpl
