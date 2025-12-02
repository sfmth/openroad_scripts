
#!/usr/bin/env python3
"""
compress_tcl_selects.py

Compress a huge Tcl "select -name" list by:
  A) Using unique hierarchy prefixes (cut ONLY at '.' or '/') per category → "prefix*"
  B) For the remainder, merging trailing numeric runs safely across categories → "...<digits_prefix>*<fixed_suffix>"

Safety: A pattern is emitted only if, when applied to ALL names from ALL categories in the input,
it matches names from exactly one category (the category we’re writing for).

Usage:
    python3 compress_tcl_selects.py in.tcl out.tcl
"""

import re
import sys
import argparse
from collections import defaultdict

SEPS = {'.', '/'}
SEL_RE = re.compile(r'''^\s*select\s+-name\s+"([^"]+)"\s+-type\s+Inst\s+-highlight\s+(\d+)\s*$''')
CAT_RE = re.compile(r'''^\s*#\s*Category\s+(\d+).*\bcolor\s+(\d+)''')
# split into: head (up to before last digit-run), digits (last run), suffix (non-digits after)
TAIL_NUM_RE = re.compile(r'^(.*?)(\d+)(\D*)$')

def parse_file(path):
    """
    Returns:
      order: [category_id as str in encountered order]
      cats:  {cat_id: {"highlight": int, "names": [str, ...]}}
      header_lines: initial comment lines (kept, optional)
    """
    cats = {}
    order = []
    cur_cat = None
    cur_high = None
    header_lines = []

    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            mcat = CAT_RE.match(line)
            if mcat:
                cur_cat = mcat.group(1)
                cur_high = int(mcat.group(2))
                if cur_cat not in cats:
                    cats[cur_cat] = {"highlight": cur_high, "names": []}
                    order.append(cur_cat)
                else:
                    # If repeated headers appear, keep first highlight
                    cats[cur_cat]["highlight"] = cats[cur_cat].get("highlight", cur_high)
                continue

            msel = SEL_RE.match(line)
            if msel and cur_cat is not None:
                name = msel.group(1)
                cats[cur_cat]["names"].append(name)
            else:
                # Preserve very first comment lines only for reference
                if not order and (line.lstrip().startswith('#') or not line.strip()):
                    header_lines.append(line.rstrip('\n'))

    return order, cats, header_lines

def sep_prefixes(name):
    """All prefixes ending exactly at '.' or '/' (short → long)."""
    out = []
    for i, ch in enumerate(name):
        if ch in SEPS:
            out.append(name[:i+1])
    return out

def build_sep_prefix_index(cats):
    """
    Map every hierarchy prefix (ending at '.' or '/') -> set of categories using that prefix.
    """
    pref_to_cats = defaultdict(set)
    for cat, d in cats.items():
        for nm in d["names"]:
            for p in sep_prefixes(nm):
                pref_to_cats[p].add(cat)
    return pref_to_cats

def compute_unique_prefixes_for_category(cat, names, pref_to_cats):
    """
    For each name, find the SHORTEST prefix-at-separator unique to `cat`.
    Then reduce the set (drop any prefix covered by a shorter already chosen).
    Returns (prefixes:set[str], covered_names:set[str])
    """
    candidates = []  # (prefix, len)
    for nm in names:
        uniq = None
        for p in sep_prefixes(nm):
            if pref_to_cats[p] == {cat}:
                uniq = p
                break  # shortest unique
        if uniq:
            candidates.append(uniq)

    # dedup and minimize coverage: keep shortest that are not subsumed by another kept prefix
    candidates = sorted(set(candidates), key=len)
    kept = []
    for p in candidates:
        if not any(p.startswith(k) for k in kept):
            kept.append(p)

    # compute coverage
    covered = set()
    for nm in names:
        if any(nm.startswith(k) for k in kept):
            covered.add(nm)

    return set(kept), covered

def build_trailing_number_index(all_names_by_cat):
    """
    Build index for trailing-number grouping:
      key = (head, suffix) where name == head + digits + suffix
      val = list of (cat, full_name, digits_str)
    """
    idx = defaultdict(list)
    for cat, names in all_names_by_cat.items():
        for nm in names:
            m = TAIL_NUM_RE.match(nm)
            if not m:
                continue
            head, digits, suffix = m.groups()
            idx[(head, suffix)].append((cat, nm, digits))
    return idx

def prefixes_of_numbers_set(nums):
    """Return a set of ALL possible leading prefixes of each numeric string in nums."""
    s = set()
    for n in nums:
        for L in range(1, len(n)+1):
            s.add(n[:L])
    return s

def make_digit_merges_for_category(cat, names_left, trailing_idx):
    """
    For each (head, suffix) group, compute minimal digit prefixes that exclude other categories.
    Returns:
      patterns: set of pattern strings like head + dprefix + '*' + suffix
      covered_names: set of names these patterns cover
    """
    # Map from (head, suffix) to our list of (name, digits)
    groups = defaultdict(list)
    for nm in names_left:
        m = TAIL_NUM_RE.match(nm)
        if not m:
            continue
        head, digits, suffix = m.groups()
        groups[(head, suffix)].append((nm, digits))

    patterns = set()
    covered = set()

    for key, our_list in groups.items():
        head, suffix = key
        # All entries across all categories for this (head, suffix)
        all_entries = trailing_idx.get(key, [])
        # Other categories' numbers for conflict checks
        other_nums = [d for (c2, _nm2, d) in all_entries if c2 != cat]
        other_pref = prefixes_of_numbers_set(other_nums)

        # Determine per-number minimal safe prefix
        dprefix_to_names = defaultdict(list)
        for nm, d in our_list:
            # Find shortest prefix of d that is NOT used by other categories
            L = 1
            while L <= len(d) and d[:L] in other_pref:
                L += 1
            # If even full length conflicts (means exact same number appears in other cat),
            # then no safe merge; we will leave this as an explicit name.
            if L > len(d):
                continue
            dprefix_to_names[d[:L]].append(nm)

        # Emit patterns only for dprefixes that cover >=2 names
        for dp, nmlist in dprefix_to_names.items():
            if len(nmlist) >= 2:
                patterns.add(f'{head}{dp}*{suffix}')
                covered.update(nmlist)

    return patterns, covered

def write_output(path, header_lines, order, cats, unique_by_cat, digit_patterns_by_cat, leftovers_by_cat):
    total_in = sum(len(cats[c]["names"]) for c in order)
    total_out = 0

    with open(path, 'w', encoding='utf-8') as out:
        out.write('# Auto-compressed Tcl selects (hierarchy + numeric merges)\n')
        out.write(f'# Original lines: {total_in}\n')
        out.write(f'# Generated by compress_tcl_selects.py\n')
        if header_lines:
            for h in header_lines:
                out.write(f'# {h}\n')
        out.write('\n')

        for cat in order:
            hlt = cats[cat]["highlight"]
            uprefs = sorted(unique_by_cat[cat], key=len)
            dpatts = sorted(digit_patterns_by_cat[cat])
            leftovers = sorted(leftovers_by_cat[cat])

            out.write(f'# Category {cat} → color {hlt}\n')

            for p in uprefs:
                out.write(f'select -name "{p}*" -type Inst -highlight {hlt}\n')
            for patt in dpatts:
                out.write(f'select -name "{patt}" -type Inst -highlight {hlt}\n')
            for nm in leftovers:
                out.write(f'select -name "{nm}" -type Inst -highlight {hlt}\n')

            total_out += len(uprefs) + len(dpatts) + len(leftovers)

    print(f'Compressed from {total_in} lines to {total_out} lines ({100.0*total_out/max(1,total_in):.2f}% of original).')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input')
    ap.add_argument('output')
    args = ap.parse_args()

    order, cats, header_lines = parse_file(args.input)
    if not order:
        print("No categories found.", file=sys.stderr)
        sys.exit(1)

    # 1) Build convenience views
    all_names_by_cat = {c: set(d["names"]) for c, d in cats.items()}
    all_names = set().union(*all_names_by_cat.values())

    # 2) Unique hierarchy prefixes (A)
    pref_to_cats = build_sep_prefix_index(cats)
    unique_by_cat = {}
    covered_by_unique = {}
    for cat in order:
        up, cov = compute_unique_prefixes_for_category(cat, cats[cat]["names"], pref_to_cats)
        unique_by_cat[cat] = up
        covered_by_unique[cat] = cov

    # 3) Numeric merges for the remainder (B)
    trailing_idx = build_trailing_number_index(all_names_by_cat)
    digit_patterns_by_cat = {}
    covered_by_digits = {}
    leftovers_by_cat = {}

    for cat in order:
        names = set(cats[cat]["names"])
        names_left = names - covered_by_unique[cat]

        dpatterns, dcovered = make_digit_merges_for_category(
            cat, names_left, trailing_idx
        )
        digit_patterns_by_cat[cat] = dpatterns
        covered_by_digits[cat] = dcovered

        # 4) Leftovers = not covered by unique prefixes nor digit merges
        leftovers_by_cat[cat] = sorted(names_left - dcovered)

    # 5) Final safety check: ensure no digit pattern crosses categories (defensive)
    #    (They shouldn’t, but we verify by matching against the dataset.)
    #    We only use patterns of form "<prefix>*<suffix>" where <prefix> is the whole head+dprefix.
    import fnmatch
    for cat in order:
        safe_patts = set()
        for patt in digit_patterns_by_cat[cat]:
            cats_hit = set()
            for other_cat, names in all_names_by_cat.items():
                for nm in names:
                    if fnmatch.fnmatchcase(nm, patt):
                        cats_hit.add(other_cat)
                        if len(cats_hit) > 1:
                            break
                if len(cats_hit) > 1:
                    break
            if cats_hit == {cat}:
                safe_patts.add(patt)
        digit_patterns_by_cat[cat] = safe_patts  # drop any unsafe (should be none)

    # 6) Write output
    write_output(args.output, header_lines, order, cats, unique_by_cat, digit_patterns_by_cat, leftovers_by_cat)

if __name__ == '__main__':
    main()
