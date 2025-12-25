#!/usr/bin/env bash

# Optional argument: file extension (default: txt)
ext="${1:-txt}"

out="combined.${ext}"

# Safely collect all matching files except the output file
mapfile -d '' files < <(
  find . -maxdepth 1 -type f -name "*.${ext}" ! -name "$out" -print0 | sort -z
)

if [ ${#files[@]} -eq 0 ]; then
  echo "No .${ext} files found."
  exit 1
fi

{
  echo "AGENT NOTES"
  echo "==========="
  echo
  echo "This document is an automatic concatenation of all .${ext} files."
  echo "Each file section is introduced by a header:"
  echo "    ### FILE: filename.${ext}"
  echo
  echo "Horizontal line separators:"
  echo "    ----------------------------------------"
  echo
  echo "These separators mark boundaries between files."
  echo
  echo "INDEX OF FILES"
  echo "=============="
  echo

  i=1
  for f in "${files[@]}"; do
    filename="${f#./}"
    echo "$i. $filename"
    ((i++))
  done

  echo
  echo "----------------------------------------"
  echo

  # Append contents
  for f in "${files[@]}"; do
    filename="${f#./}"
    echo "### FILE: $filename"
    echo "----------------------------------------"
    cat "$f"
    echo
    echo "----------------------------------------"
    echo
  done
} > "$out"

echo "Done. Output written to $out"


