#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "Usage: $0 <platform> <design> [run_tag]" >&2
  echo "Example: $0 asap7 cnn base" >&2
  exit 1
fi

platform="$1"
design="$2"
run_tag="${3:-base}"

log_dir="logs/$platform/$design/$run_tag"

if [ ! -d "$log_dir" ]; then
  echo "Log directory not found: $log_dir" >&2
  exit 1
fi

awk '
/Elapsed time:/ {
  line = $0
  sub(/.*Elapsed time:[[:space:]]*/, "", line)
  sub(/\[h:.*/, "", line)

  t = line
  gsub(/^[[:space:]]+/, "", t)
  gsub(/[[:space:]]+$/, "", t)

  n = split(t, a, ":")
  sec_field = a[n]
  sub(/\..*/, "", sec_field)

  if (n == 3) {
    h = a[1]; min = a[2]; s = sec_field
  } else if (n == 2) {
    h = 0; min = a[1]; s = sec_field
  } else if (n == 1) {
    h = 0; min = 0; s = sec_field
  } else {
    next
  }

  step = FILENAME
  sub(/^.*\//, "", step)

  secs = h * 3600 + min * 60 + s
  total += secs
  found = 1

  sh = int(secs / 3600)
  sm = int((secs % 3600) / 60)
  ss = int(secs % 60)

  printf "%s: %02d:%02d:%02d (h:m:s)\n", step, sh, sm, ss
}
END {
  if (!found) {
    print "No Elapsed time lines found" > "/dev/stderr"
    exit 1
  }

  th = int(total / 3600)
  tm = int((total % 3600) / 60)
  ts = int(total % 60)

  printf "Total elapsed time: %02d:%02d:%02d (h:m:s)\n", th, tm, ts
}
' "$log_dir"/*.log
