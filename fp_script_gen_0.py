import openroad
from openroad import Design, Tech, Timing
import rcx
import os
import odb


openroad.openroad_version()

odb_file = "results/nangate45/ariane133/base/2_2_floorplan_macro.odb"
lef_files = [#"platforms/nangate45/lef/NangateOpenCellLibrary.macro.lef",
#                "platforms/nangate45/lef/NangateOpenCellLibrary.macro.mod.lef",
 #               "platforms/nangate45/lef/NangateOpenCellLibrary.macro.rect.lef",
                "platforms/nangate45/lef/NangateOpenCellLibrary.tech.lef"]
lib_files = ["platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"]

tech = Tech()
for lef_file in lef_files:
    tech.readLef(lef_file)
for lib_file in lib_files:
    tech.readLiberty(lib_file)

design = Design(tech)

design.readDb(odb_file)

from collections import defaultdict

def escape_tcl(s: str) -> str:
    # Escape for a double-quoted Tcl string
    # Order matters: backslash first.
    s = s.replace('\\', r'\\')
    s = s.replace('"', r'\"')
    s = s.replace('$', r'\$')
    s = s.replace('[', r'\[')
    s = s.replace(']', r'\]')
    return s

# Build category → instances (your category is inst.getLocation()[1])
insts_by_cat = defaultdict(list)

for inst in design.getBlock().getInsts():
    if "FILLER" in inst.getName():  continue
    if "TAP" in inst.getName():     continue
    if "decap" in inst.getMaster().getName():  continue
    if not inst.isPlaced():         continue

    cat = inst.getLocation()[1]     # category (Y coord)
    insts_by_cat[cat].append(inst)

# Count and keep only categories with >100 instances
cats_over_100 = [cat for cat, insts in insts_by_cat.items() if len(insts) > 100]
# Make color assignment stable: sort by category key, then cycle 0..20
cats_over_100.sort()
color_map = {cat: (i % 17) for i, cat in enumerate(cats_over_100)}

# Write Tcl
out_tcl = "highlight_by_category.tcl"
with open(out_tcl, "w") as f:
    f.write("# Auto-generated: highlight instances by category (count > 100)\n")
    for cat in cats_over_100:
        color = color_map[cat]
        # Optional: comment header per category
        f.write(f"# Category {cat} (count={len(insts_by_cat[cat])}) → color {color}\n")
        for inst in insts_by_cat[cat]:
            name_escaped = escape_tcl(inst.getName())
            f.write(f'select -name "{name_escaped}" -type Inst -highlight {color}\n')

print(f"Wrote {out_tcl} with {sum(len(insts_by_cat[c]) for c in cats_over_100)} selections across {len(cats_over_100)} categories.")

