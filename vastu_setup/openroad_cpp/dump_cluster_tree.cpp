// SPDX-License-Identifier: BSD-3-Clause
// Vastu integration: dump RTLMP cluster tree to JSON.
//
// This file is meant to be added to tools/OpenROAD/src/mpl/src/.
// It implements the dumpClusterTree() method for HierRTLMP and the
// corresponding MacroPlacer::dumpClusterTree() factory method.
//
// After running RTLMP's multilevel autoclustering (without placement),
// the cluster hierarchy is serialized to a JSON file that vastu can read.

#include "hier_rtlmp.h"

#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "clusterEngine.h"
#include "object.h"
#include "odb/db.h"
#include "utl/Logger.h"

namespace mpl {

// Escape a string for JSON output
static std::string json_escape(const std::string& s)
{
  std::string out;
  out.reserve(s.size() + 8);
  for (char c : s) {
    switch (c) {
      case '"':
        out += "\\\"";
        break;
      case '\\':
        out += "\\\\";
        break;
      case '\n':
        out += "\\n";
        break;
      default:
        out += c;
    }
  }
  return out;
}

static std::string cluster_type_str(ClusterType t)
{
  switch (t) {
    case StdCellCluster:
      return "stdcell";
    case HardMacroCluster:
      return "macro";
    case MixedCluster:
      return "mixed";
    default:
      return "unknown";
  }
}

static void serialize_cluster(const Cluster* cluster,
                              odb::dbBlock* block,
                              int halo_x,
                              int halo_y,
                              const std::string& inst_list_dir,
                              std::ostream& os,
                              bool& first)
{
  if (!first) {
    os << ",\n";
  }
  first = false;

  const float dbu = static_cast<float>(block->getDbUnitsPerMicron());

  os << "    {\n";
  os << "      \"id\": " << cluster->getId() << ",\n";
  os << "      \"name\": \"" << json_escape(cluster->getName()) << "\",\n";
  os << "      \"type\": \"" << cluster_type_str(cluster->getClusterType())
     << "\",\n";

  // Parent
  if (cluster->getParent()) {
    os << "      \"parent\": " << cluster->getParent()->getId() << ",\n";
  } else {
    os << "      \"parent\": null,\n";
  }

  // Children IDs
  os << "      \"children\": [";
  bool first_child = true;
  for (const auto& child : cluster->getChildren()) {
    if (!first_child) os << ", ";
    os << child->getId();
    first_child = false;
  }
  os << "],\n";

  // Metrics
  const auto& metrics = cluster->getMetrics();
  os << "      \"std_cell_area\": " << (metrics.getStdCellArea() / (dbu * dbu))
     << ",\n";
  os << "      \"macro_area\": " << (metrics.getMacroArea() / (dbu * dbu))
     << ",\n";
  os << "      \"num_std_cells\": " << metrics.getNumStdCell() << ",\n";
  os << "      \"num_macros\": " << metrics.getNumMacro() << ",\n";

  // Connections
  os << "      \"connections\": {";
  bool first_conn = true;
  for (const auto& [target_id, weight] : cluster->getConnectionsMap()) {
    if (!first_conn) os << ", ";
    os << "\"" << target_id << "\": " << weight;
    first_conn = false;
  }
  os << "}";

  // Macros (for leaf clusters with macros)
  auto leaf_macros = cluster->getLeafMacros();
  if (!leaf_macros.empty()) {
    os << ",\n      \"macros\": [\n";
    bool first_macro = true;
    for (odb::dbInst* inst : leaf_macros) {
      if (!first_macro) os << ",\n";
      first_macro = false;
      odb::dbMaster* master = inst->getMaster();
      os << "        {\n";
      os << "          \"name\": \"" << json_escape(master->getName())
         << "\",\n";
      os << "          \"inst_name\": \"" << json_escape(inst->getName())
         << "\",\n";
      os << "          \"width\": " << (master->getWidth() / dbu) << ",\n";
      os << "          \"height\": " << (master->getHeight() / dbu) << ",\n";
      os << "          \"fixed\": " << (inst->isFixed() ? "true" : "false");
      if (inst->isFixed()) {
        int x, y;
        inst->getLocation(x, y);
        os << ",\n          \"fixed_x\": " << (x / dbu);
        os << ",\n          \"fixed_y\": " << (y / dbu);
      }
      os << ",\n          \"halo_x\": " << (halo_x / dbu);
      os << ",\n          \"halo_y\": " << (halo_y / dbu);
      os << ",\n          \"orientation\": \""
         << inst->getOrient().getString() << "\"";
      os << "\n        }";
    }
    os << "\n      ]";
  }

  // Std cells of this cluster. RTLMP stores them in TWO places:
  //   - leaf_std_cells_   : orphan std cells directly assigned (glue logic)
  //   - db_modules_       : whole modules whose instances belong to this
  //                         cluster — those cells live recursively under
  //                         dbModule (use getLeafInsts() to flatten).
  // Earlier versions only dumped leaf_std_cells_, which left ~94% of the
  // design (everything inside module-named clusters like `i_frontend`) with
  // no inst-name list — so vastu's tcl_writer seeded 0 cells for them, and
  // the .odb after macro_place had only ~9.6k of 158k std cells placed.
  std::vector<odb::dbInst*> cluster_std_cells = cluster->getLeafStdCells();
  for (odb::dbModule* module : cluster->getDbModules()) {
    for (odb::dbInst* inst : module->getLeafInsts()) {
      if (inst->isBlock()) {
        continue;  // macros handled separately above
      }
      cluster_std_cells.push_back(inst);
    }
  }

  if (!cluster_std_cells.empty()) {
    if (cluster_std_cells.size() <= 10000) {
      os << ",\n      \"leaf_instances\": [";
      bool first_inst = true;
      for (odb::dbInst* inst : cluster_std_cells) {
        if (!first_inst) os << ", ";
        os << "\"" << json_escape(inst->getName()) << "\"";
        first_inst = false;
      }
      os << "]";
    } else {
      // Write to separate file
      std::string filename
          = "cluster_" + std::to_string(cluster->getId()) + "_insts.txt";
      std::string filepath = inst_list_dir + "/" + filename;
      std::ofstream inst_file(filepath);
      for (odb::dbInst* inst : cluster_std_cells) {
        inst_file << inst->getName() << "\n";
      }
      inst_file.close();
      os << ",\n      \"leaf_instances_file\": \"" << json_escape(filename)
         << "\"";
    }
  }

  os << "\n    }";

  // Recurse into children
  for (const auto& child : cluster->getChildren()) {
    serialize_cluster(
        child.get(), block, halo_x, halo_y, inst_list_dir, os, first);
  }
}

void HierRTLMP::dumpClusterTree(const char* output_path)
{
  // setGlobalFence({}) falls back to the core area, populating
  // tree_->global_fence so setFloorplanShape() yields a non-empty shape and
  // movableCellsFitInMacroPlacementArea() can check against the real core.
  // Without this, the area check sees floorplan_shape area == 0 → MPL-0065.
  setGlobalFence(odb::Rect());

  // Run clustering only (steps 1 of run())
  runMultilevelAutoclustering();

  const float dbu
      = static_cast<float>(block_->getDbUnitsPerMicron());

  std::ofstream os(output_path);
  if (!os.is_open()) {
    logger_->error(utl::MPL, 999, "Cannot open {} for writing", output_path);
    return;
  }

  // Determine instance list directory (same dir as output)
  std::string out_str(output_path);
  std::string inst_list_dir = ".";
  auto slash_pos = out_str.rfind('/');
  if (slash_pos != std::string::npos) {
    inst_list_dir = out_str.substr(0, slash_pos);
  }

  os << "{\n";

  // Floorplan info
  const auto& fp = tree_->floorplan_shape;
  const auto& die = tree_->die_area;
  os << "  \"floorplan\": {\n";
  os << "    \"width\": " << (fp.dx() / dbu) << ",\n";
  os << "    \"height\": " << (fp.dy() / dbu) << ",\n";
  os << "    \"die_area\": [" << (die.xMin() / dbu) << ", "
     << (die.yMin() / dbu) << ", " << (die.xMax() / dbu) << ", "
     << (die.yMax() / dbu) << "],\n";
  os << "    \"core_area\": [" << (fp.xMin() / dbu) << ", "
     << (fp.yMin() / dbu) << ", " << (fp.xMax() / dbu) << ", "
     << (fp.yMax() / dbu) << "]\n";
  os << "  },\n";

  // dbu_per_micron
  os << "  \"dbu_per_micron\": " << block_->getDbUnitsPerMicron() << ",\n";

  // Clusters
  os << "  \"clusters\": [\n";
  bool first = true;
  if (tree_->root) {
    int halo_x_dbu = base_halo_.left + base_halo_.right;
    int halo_y_dbu = base_halo_.bottom + base_halo_.top;
    serialize_cluster(
        tree_->root.get(), block_, halo_x_dbu, halo_y_dbu, inst_list_dir, os,
        first);
  }
  os << "\n  ],\n";

  // IO pins
  os << "  \"io_pins\": [\n";
  bool first_pin = true;
  for (odb::dbBTerm* bterm : block_->getBTerms()) {
    int x = 0, y = 0;
    if (bterm->getFirstPinLocation(x, y)) {
      if (!first_pin) os << ",\n";
      first_pin = false;

      // Determine side based on position
      const char* side = "W";
      int dx_min = x - fp.xMin();
      int dx_max = fp.xMax() - x;
      int dy_min = y - fp.yMin();
      int dy_max = fp.yMax() - y;
      int min_dist = dx_min;
      if (dx_max < min_dist) { min_dist = dx_max; side = "E"; }
      if (dy_min < min_dist) { min_dist = dy_min; side = "S"; }
      if (dy_max < min_dist) { side = "N"; }

      const char* dir = "input";
      if (bterm->getIoType() == odb::dbIoType::OUTPUT) dir = "output";
      else if (bterm->getIoType() == odb::dbIoType::INOUT) dir = "inout";

      os << "    {\"name\": \"" << json_escape(bterm->getName())
         << "\", \"x\": " << (x / dbu) << ", \"y\": " << (y / dbu)
         << ", \"side\": \"" << side << "\", \"direction\": \"" << dir
         << "\"}";
    }
  }
  os << "\n  ],\n";

  // Fixed macros (pre-placed)
  os << "  \"fixed_macros\": [\n";
  bool first_fixed = true;
  for (odb::dbInst* inst : block_->getInsts()) {
    if (!inst->isFixed()) continue;
    odb::dbMaster* master = inst->getMaster();
    if (!master->isBlock()) continue;
    if (!first_fixed) os << ",\n";
    first_fixed = false;
    int x, y;
    inst->getLocation(x, y);
    os << "    {\"name\": \"" << json_escape(master->getName())
       << "\", \"inst_name\": \"" << json_escape(inst->getName())
       << "\", \"x\": " << (x / dbu) << ", \"y\": " << (y / dbu)
       << ", \"width\": " << (master->getWidth() / dbu)
       << ", \"height\": " << (master->getHeight() / dbu)
       << ", \"orientation\": \"" << inst->getOrient().getString() << "\"}";
  }
  os << "\n  ]\n";

  os << "}\n";
  os.close();

  logger_->info(utl::MPL, 998, "Cluster tree written to {}", output_path);
}

}  // namespace mpl
