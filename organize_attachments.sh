#!/usr/bin/env bash
set -euo pipefail

base="$PWD"
target="${base}/@Attachments"

mkdir -p "$target"
shopt -s nullglob dotglob

for dir in "$base"/Attachments*; do
  [ "$dir" = "$target" ] && continue
  [ -d "$dir" ] || continue

  for src in "$dir"/*; do
    [ -e "$src" ] || continue
    dest="$target/${src##*/}"

    if [ -e "$dest" ]; then
      echo "Skipping duplicate: ${src##*/}"
      continue
    fi

    mv "$src" "$target/"
  done

  rmdir "$dir" || echo "Could not remove directory $dir (maybe not empty)"
done

