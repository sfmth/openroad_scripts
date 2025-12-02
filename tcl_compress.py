
#!/usr/bin/env python3
"""
tcl_highlight_editor.py
—————————
Utility classes for reading, manipulating, and saving
Tcl “select -name … -highlight …” scripts that are arranged
in per-category blocks.

Usage example (run from a separate script or an interactive shell):

    from tcl_highlight_editor import TclScript
    tcl = TclScript.read("input.tcl")

    # List all category IDs and how many commands each holds
    print({cid: len(cat.cmds) for cid, cat in tcl.categories.items()})

    # Example 1 ─ simple replacement
    tcl.substitute(r"/i_mult\.i_multiplier/_", "/mult.u_/")

    # Example 2 ─ write your own transformation function
    import re
    def starify(name: str) -> str:
        # turn …/_12345_ into …/_12*_  (hide middle digits)
        return re.sub(r"_(\d{2})\d{1,3}_(?!\d)", r"_\1*_", name)
    tcl.apply(starify, category_filter={510057})

    tcl.write("output.tcl")
"""

from difflib import SequenceMatcher
import itertools
from collections import Counter
import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Callable, Iterable, Optional


# ──────────────── low-level parse helpers ──────────────── #

SEL_RE = re.compile(
    r'^select\s+-name\s+"(?P<name>[^"]+)"\s+'
    r'-type\s+(?P<type>\S+)\s+'
    r'-highlight\s+(?P<hlight>\d+)\s*$'
)
CAT_RE = re.compile(
    r'^#\s*Category\s+(?P<cat_id>\d+)\s*\(count=(?P<count>\d+)\)\s*→\s*color\s+(?P<color>\d+)\s*$'
)

@dataclass
class SelectCmd:
    name: str
    type_: str
    highlight: int
    raw_line: str                     # original text, for rewrites

    def rebuild(self) -> str:
        """Return the Tcl line reflecting current fields."""
        escaped = self.name.replace('"', r'\"')
        return f'select -name "{escaped}" -type {self.type_} -highlight {self.highlight}'

@dataclass
class CategoryBlock:
    cat_id: int
    count_hint: int
    color: int
    header_line: str                  # original comment line
    cmds: List[SelectCmd] = field(default_factory=list)


# ──────────────── main document object ──────────────── #

class TclScript:
    """Represents a whole highlight script grouped by Category."""

    def __init__(self) -> None:
        self.pre_comments: List[str] = []          # anything before first category
        self.categories: Dict[int, CategoryBlock] = {}
        self.trailer: List[str] = []               # anything after last category

    # ---------- I/O ---------- #

    @classmethod
    def read(cls, path: str | Path) -> "TclScript":
        obj = cls()
        current_cat: Optional[CategoryBlock] = None

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                ln = line.rstrip("\n")
                cat_m = CAT_RE.match(ln)
                sel_m = SEL_RE.match(ln)

                if cat_m:
                    current_cat = CategoryBlock(
                        cat_id=int(cat_m["cat_id"]),
                        count_hint=int(cat_m["count"]),
                        color=int(cat_m["color"]),
                        header_line=ln,
                    )
                    obj.categories[current_cat.cat_id] = current_cat
                elif sel_m and current_cat:
                    cmd = SelectCmd(
                        name=sel_m["name"],
                        type_=sel_m["type"],
                        highlight=int(sel_m["hlight"]),
                        raw_line=ln,
                    )
                    current_cat.cmds.append(cmd)
                else:
                    # line is a comment or blank.  Decide where it belongs:
                    if current_cat is None:
                        obj.pre_comments.append(ln)
                    else:
                        obj.trailer.append(ln)
        return obj

    def write(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for ln in self.pre_comments:
                print(ln, file=f)

            for cat in self.categories.values():
                print(cat.header_line, file=f)
                for cmd in cat.cmds:
                    print(cmd.rebuild(), file=f)

            for ln in self.trailer:
                print(ln, file=f)

    # ---------- transformation helpers ---------- #

    def _iter_cmds(
        self,
        category_filter: Optional[Iterable[int]] = None
    ) -> Iterable[SelectCmd]:
        if category_filter is None:
            for cat in self.categories.values():
                yield from cat.cmds
        else:
            for cid in category_filter:
                if cid in self.categories:
                    yield from self.categories[cid].cmds

    def substitute(
        self,
        pattern: str,
        repl: str,
        *,
        category_filter: Optional[Iterable[int]] = None,
        count: int = 0,
        flags: int = 0,
    ) -> int:
        """
        Regex replace in -name strings.  Returns # of substitutions made.
        """
        prog = re.compile(pattern, flags)
        n_changes = 0
        for cmd in self._iter_cmds(category_filter):
            new_name, n = prog.subn(repl, cmd.name, count=count)
            if n:
                cmd.name = new_name
                n_changes += n
        return n_changes

    def apply(
        self,
        fn: Callable[[str], str],
        *,
        category_filter: Optional[Iterable[int]] = None,
    ) -> None:
        """
        Apply arbitrary `fn(name)->new_name` to each command’s name.
        """
        for cmd in self._iter_cmds(category_filter):
            cmd.name = fn(cmd.name)

    # ---------- convenience ---------- #

    def find(self, pattern: str, *, flags: int = 0,
             category_filter: Optional[Iterable[int]] = None) -> List[str]:
        """Return list of names that match regex."""
        prog = re.compile(pattern, flags)
        return [
            cmd.name
            for cmd in self._iter_cmds(category_filter)
            if prog.search(cmd.name)
        ]


# ── quick-n-dirty CLI (optional) ── #




import re, itertools
from collections import Counter

SEG_SPLIT = re.compile(r'[/.]+')          # delimiter class

def shared_leading_segments(names,
                            min_len: int = 1,
                            min_occurs: int = 2,
                            sample: int | None = None):
    """
    Count *prefixes* (built from whole segments) that:
      • start at index 0 of each string
      • end right *after* a '/' (or '.') delimiter
      • appear in ≥ `min_occurs` different names
      • have length ≥ `min_len`
    Returns {prefix : hit_count}.
    """
    if sample:
        names = names[:sample]

    hits = Counter()

    for n in names:
        segs = SEG_SPLIT.split(n)          # break into path tokens
        seen = set()                       # avoid double-counting in same name
        prefix = ''
        for tok in segs[:-1]:              # ignore trailing '' after final '/'
            prefix += tok + '/'            # always end with delimiter
            if len(prefix) >= min_len and prefix not in seen:
                hits[prefix] += 1
                seen.add(prefix)

    # keep only prefixes that occur in ≥ min_occurs distinct names
    return {p: c for p, c in hits.items() if c >= min_occurs}


if __name__ == "__main__":
    import argparse, sys, textwrap

    parser = argparse.ArgumentParser(
        description="Batch substitute in Tcl highlight scripts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python tcl_highlight_editor.py in.tcl out.tcl -p "_\\d{5}_" "_****_"
              python tcl_highlight_editor.py in.tcl out.tcl -p "/i_mult" "/mult" -c 510057 510058
        """),
    )
    parser.add_argument("infile")
    parser.add_argument("outfile")
    parser.add_argument("-p", "--pattern", nargs=2, metavar=("REGEX", "REPL"),
                        help="regex substitution to apply")
    parser.add_argument("-c", "--categories", nargs="*", type=int,
                        help="restrict operation to these category IDs")

    args = parser.parse_args()

    tcl = TclScript.read(args.infile)
    
    for cid, cat in tcl.categories.items():
        names = [cmd.name for cmd in cat.cmds]

        substrs = shared_leading_segments([cmd.name for cmd in cat.cmds], min_len=4, min_occurs=2)
        if substrs:
            print(f"\nCategory {cid} top shared fragments:")
            for frag, cnt in sorted(substrs.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))[:10]:
                print(f"   '{frag}'  ({cnt} occurrences)")


        #prefix = os.path.commonprefix(names)
        ## common suffix = commonprefix of the reversed strings
        #print("\nCategory " + str(cid))
        #print(prefix)
        
        #print(cid, [cmd.name for cmd in cat.cmds])

    if args.pattern:
        pat, repl = args.pattern
        n_subs = tcl.substitute(
            pat, repl, category_filter=args.categories
        )
        print(f"Made {n_subs} substitution(s).", file=sys.stderr)

    tcl.write(args.outfile)
