#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

# clean.sh
# Unified LLM-ingestion prep:
#   default:   txt -> sanitize/latexify (ASCII-only)
#   --pdf:     pdf -> txt (page markers) -> sanitize/latexify (ASCII-only)
#   --bundle:  txt (typically sanitized) -> corpus bundle
#
# Defaults requested:
#   ./clean.sh                  # all txt, DRY RUN, summary, no report
#   ./clean.sh file1 file2      # specific files
#   ./clean.sh --pdf            # pdf -> txt -> sanitize/latexify
#   ./clean.sh --bundle         # txt -> corpus
#
# Notes:
# - ASCII-only guarantee: after best-effort replacements, any remaining non-ASCII is removed.
# - Keeps existing LaTeX commands intact by NOT escaping backslashes.
# - § -> \S and ¶ -> \P (known LaTeX escapes; do not alter further)

prog="$(basename "$0")"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 127; }; }

usage() {
  cat <<EOF
Unified sanitizer / pdf2llmtext / corpus bundler.

Usage:
  $prog [options]                  # default: sanitize all *.txt in cwd (dry-run)
  $prog [options] file1 [file2...] # sanitize specific files

Modes:
      (default)  txt -> sanitize/latexify (ASCII-only)
  --pdf          pdf -> txt -> sanitize/latexify (ASCII-only)
  --bundle       bundle corpus from text files (typically after sanitize)

Core options (sanitize):
  -w, --write                Apply changes in-place (default: dry-run)
  -n, --dry-run              Do not modify files (default)
  -e, --ext EXT              When no files are specified, include extension EXT (repeatable). Default: txt
  -g, --glob PATTERN         When no files are specified, process files matching glob (overrides --ext)
  -j, --jobs N               Parallel workers for PDF and sanitize (default: 4)
      --max-bytes N          Skip files larger than N bytes (default: 52428800 = 50 MiB)
      --allow-large          Do not skip large files
  -t, --tabstop N            Expand tabs to N spaces (default: 4)
      --keep-tabs            Do not expand tabs
      --no-blank-collapse    Do not collapse excessive blank lines
      --no-trim              Do not trim trailing whitespace
      --report FILE          Write unicode report (disabled by default)
      --no-report            Disable unicode report (default)
  -q, --quiet                Print only one summary line
  -h, --help                 Show help

PDF options (only relevant with --pdf):
      --pdf-outdir DIR       Where raw extracted txt goes (default: txt)
      --pdf-src PATTERN      Glob pattern of input PDFs (default: *.pdf)
      --pdf-mode raw|layout  pdftotext mode (default: raw)
      --pdf-force            Force reconversion even if output is up-to-date
      --pdf-min-chars N      Flag PDFs with output under N chars as OCR candidates (default: 200)

Bundle options (only relevant with --bundle):
      --bundle-out FILE      Output corpus file (default: corpus.txt)
      --bundle-sort MODE     name|mtime|size (default: name)
      --bundle-reverse       Reverse sort
      --bundle-no-index      Do not print file index
      --bundle-no-preamble   Do not print explanatory preamble
      --bundle-no-meta       Disable metadata in file headers
      --bundle-src PATTERN   Glob pattern of input files to bundle (default: *.txt)
                             (Note: output file is always excluded)

Examples:
  ./clean.sh
  ./clean.sh --write
  ./clean.sh file1.txt file2.txt
  ./clean.sh --pdf --write
  ./clean.sh --pdf --write --pdf-outdir txt --glob 'txt/*.txt'
  ./clean.sh --bundle --bundle-src 'sanitized/*.txt' --bundle-out corpus.txt
EOF
}

# ----------------------------
# Defaults
# ----------------------------
JOBS=8

MODE_PDF=0
MODE_BUNDLE=0

WRITE_MODE=0
QUIET=0

GLOB=""
declare -a EXTS=("txt")

MAX_BYTES=52428800
ALLOW_LARGE=0

TABSTOP=4
EXPAND_TABS=1
COLLAPSE_BLANKS=1
TRIM_TRAILING=1

REPORT_ENABLED=0
REPORT_FILE="clean-unicode-report.txt"

# PDF defaults (mirrors pdf2llmtext.sh behavior)
PDF_OUTDIR="txt"
PDF_FORCE=0
PDF_MIN_CHARS=200
PDF_MODE="raw"
PDF_SRC="*.pdf"

# Bundle defaults (mirrors bundle-llm-corpus.sh behavior)
BUNDLE_OUT="corpus.txt"
BUNDLE_SORT="name"
BUNDLE_REVERSE=0
BUNDLE_NO_INDEX=0
BUNDLE_NO_PREAMBLE=0
BUNDLE_WITH_META=1
BUNDLE_SRC="*.txt"

# ----------------------------
# Parse args
# ----------------------------
declare -a POSITIONAL=()
while (( $# )); do
  case "$1" in
    --pdf) MODE_PDF=1; shift ;;
    --bundle) MODE_BUNDLE=1; shift ;;

    -w|--write) WRITE_MODE=1; shift ;;
    -n|--dry-run) WRITE_MODE=0; shift ;;
    -q|--quiet) QUIET=1; shift ;;
    -h|--help) usage; exit 0 ;;

    -e|--ext)
      [[ $# -ge 2 ]] || { echo "Missing argument for --ext" >&2; exit 2; }
      EXTS+=("${2#.}")
      shift 2
      ;;

    -g|--glob)
      [[ $# -ge 2 ]] || { echo "Missing argument for --glob" >&2; exit 2; }
      GLOB="$2"
      shift 2
      ;;

    -j|--jobs)
      [[ $# -ge 2 ]] || { echo "Missing argument for --jobs" >&2; exit 2; }
      JOBS="$2"
      shift 2
      ;;

    --max-bytes)
      [[ $# -ge 2 ]] || { echo "Missing argument for --max-bytes" >&2; exit 2; }
      MAX_BYTES="$2"
      shift 2
      ;;

    --allow-large) ALLOW_LARGE=1; shift ;;

    -t|--tabstop)
      [[ $# -ge 2 ]] || { echo "Missing argument for --tabstop" >&2; exit 2; }
      TABSTOP="$2"
      shift 2
      ;;

    --keep-tabs|--no-expand-tabs) EXPAND_TABS=0; shift ;;
    --no-blank-collapse) COLLAPSE_BLANKS=0; shift ;;
    --no-trim) TRIM_TRAILING=0; shift ;;

    --report)
      [[ $# -ge 2 ]] || { echo "Missing argument for --report" >&2; exit 2; }
      REPORT_ENABLED=1
      REPORT_FILE="$2"
      shift 2
      ;;

    --no-report)
      REPORT_ENABLED=0
      shift
      ;;

    # PDF flags
    --pdf-outdir)
      [[ $# -ge 2 ]] || { echo "Missing argument for --pdf-outdir" >&2; exit 2; }
      PDF_OUTDIR="$2"; shift 2 ;;
    --pdf-src)
      [[ $# -ge 2 ]] || { echo "Missing argument for --pdf-src" >&2; exit 2; }
      PDF_SRC="$2"; shift 2 ;;
    --pdf-mode)
      [[ $# -ge 2 ]] || { echo "Missing argument for --pdf-mode" >&2; exit 2; }
      PDF_MODE="$2"; shift 2 ;;
    --pdf-force) PDF_FORCE=1; shift ;;
    --pdf-min-chars)
      [[ $# -ge 2 ]] || { echo "Missing argument for --pdf-min-chars" >&2; exit 2; }
      PDF_MIN_CHARS="$2"; shift 2 ;;

    # Bundle flags
    --bundle-out)
      [[ $# -ge 2 ]] || { echo "Missing argument for --bundle-out" >&2; exit 2; }
      BUNDLE_OUT="$2"; shift 2 ;;
    --bundle-sort)
      [[ $# -ge 2 ]] || { echo "Missing argument for --bundle-sort" >&2; exit 2; }
      BUNDLE_SORT="$2"; shift 2 ;;
    --bundle-reverse) BUNDLE_REVERSE=1; shift ;;
    --bundle-no-index) BUNDLE_NO_INDEX=1; shift ;;
    --bundle-no-preamble) BUNDLE_NO_PREAMBLE=1; shift ;;
    --bundle-no-meta) BUNDLE_WITH_META=0; shift ;;
    --bundle-src)
      [[ $# -ge 2 ]] || { echo "Missing argument for --bundle-src" >&2; exit 2; }
      BUNDLE_SRC="$2"; shift 2 ;;

    --) shift; break ;;
    -*)
      echo "Unknown option: $1" >&2
      echo "Run: $prog --help" >&2
      exit 2
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done
while (( $# )); do POSITIONAL+=("$1"); shift; done

# Validate
[[ "$TABSTOP" =~ ^[0-9]+$ ]] || { echo "--tabstop must be an integer" >&2; exit 2; }
[[ "$MAX_BYTES" =~ ^[0-9]+$ ]] || { echo "--max-bytes must be an integer" >&2; exit 2; }
[[ "$PDF_MIN_CHARS" =~ ^[0-9]+$ ]] || { echo "--pdf-min-chars must be an integer" >&2; exit 2; }
[[ "$JOBS" =~ ^[0-9]+$ ]] || { echo "--jobs must be an integer" >&2; exit 2; }
(( JOBS >= 1 )) || { echo "--jobs must be >= 1" >&2; exit 2; }
need xargs
case "$PDF_MODE" in raw|layout) : ;; *) echo "Invalid --pdf-mode: $PDF_MODE (raw|layout)" >&2; exit 2;; esac
: "${PDF_OUTDIR:=txt}"
[[ -n "$PDF_OUTDIR" ]] || { echo "PDF_OUTDIR is empty" >&2; exit 2; }
if (( REPORT_ENABLED )); then
  echo "Warning: --report is currently not implemented in the parallel sanitizer path." >&2
fi

# Tools (baseline)
need perl
need tr
need sed
need grep
need wc
need cmp
need mktemp
need date

# Expand is optional depending on --keep-tabs
if (( EXPAND_TABS )); then need expand; fi

mode_str="DRY-RUN"
(( WRITE_MODE )) && mode_str="WRITE"

# ----------------------------
# Shared: unicode->latex/ascii + ASCII-only guarantee
# ----------------------------
perl_script="$(mktemp)"
cleanup() { rm -f -- "$perl_script"; }
trap cleanup EXIT

cat >"$perl_script" <<'PERL'
use strict;
use warnings;
use utf8;

binmode(STDIN,  ":encoding(UTF-8)");
binmode(STDOUT, ":encoding(UTF-8)");

local $/ = undef;
my $s = <STDIN>;
$s = "" unless defined $s;

# Unicode spaces -> ASCII space
$s =~ s/\x{00A0}/ /g;   # NBSP
$s =~ s/\p{Zs}+/ /g;

# Smart quotes/apostrophes
$s =~ s/\x{201C}/``/g;  # “
$s =~ s/\x{201D}/''/g;  # ”
$s =~ s/\x{2018}/`/g;   # ‘
$s =~ s/\x{2019}/'/g;   # ’
$s =~ s/\x{201A}/'/g;   # ‚
$s =~ s/\x{201E}/''/g;  # „

# Dashes/minus
$s =~ s/\x{2013}/--/g;  # –
$s =~ s/\x{2014}/---/g; # —
$s =~ s/\x{2212}/-/g;   # −

# Ellipsis
$s =~ s/\x{2026}/\\ldots{}/g;  # …

# Section / paragraph (known LaTeX escapes)
$s =~ s/\x{00A7}/\\S/g;  # § -> \S
$s =~ s/\x{00B6}/\\P/g;  # ¶ -> \P

# Common symbols
$s =~ s/\x{00A9}/\\textcopyright{}/g;    # ©
$s =~ s/\x{00AE}/\\textregistered{}/g;   # ®
$s =~ s/\x{2122}/\\texttrademark{}/g;    # ™

# Math-ish
$s =~ s/\x{00D7}/\\times{}/g;  # ×
$s =~ s/\x{00F7}/\\div{}/g;    # ÷
$s =~ s/\x{00B1}/\\pm{}/g;     # ±
$s =~ s/\x{2264}/\\le{}/g;     # ≤
$s =~ s/\x{2265}/\\ge{}/g;     # ≥

# Collapse long blank lines from PDFs before LaTeX escaping:
# - plain underscores: _____
# - LaTeX-escaped underscores might appear after escaping, but we do this early too
$s =~ s/_{5,}/[BLANK]/g;

# Escape LaTeX specials (do NOT touch backslash)
$s =~ s/&/\\&/g;
$s =~ s/%/\\%/g;
$s =~ s/\$/\\\$/g;
$s =~ s/#/\\#/g;
$s =~ s/_/\\_/g;
$s =~ s/\{/\\{/g;
$s =~ s/\}/\\}/g;

# After escaping specials, collapse repeated \_ sequences too
$s =~ s/(?:\\_){5,}/[BLANK]/g;

# HARD GUARANTEE: remove remaining non-ASCII
$s =~ s/[^\x00-\x7F]//g;

print $s;
PERL

# ----------------------------
# Reporting helpers
# ----------------------------
flag_non_ascii() {
  local file="$1"
  local label="$2"

  if perl -CS -ne 'exit 1 if /[^\x00-\x7F]/; END{exit 0}' "$file"; then
    return 0
  fi

  perl -CS -ne '
    my $line = $.;
    my $s = $_;
    while ($s =~ /([^\x00-\x7F])/g) {
      my $ch = $1;
      my $col = pos($s);
      my $cp = ord($ch);
      my $hex = sprintf("U+%04X", $cp);
      my $ctx = $s;
      chomp($ctx);
      $ctx =~ s/\t/\\t/g;
      print "$ENV{LBL}:$line:$col: $hex \"$ch\" $ctx\n";
    }
  ' LBL="$label" "$file"
}

if (( REPORT_ENABLED )); then
  : > "$REPORT_FILE"
  {
    echo "clean.sh unicode report"
    echo "Generated: $(date -Iseconds)"
    echo "Mode: $mode_str"
    echo
    echo "Remaining non-ASCII BEFORE final removal step:"
    echo "Format: filename: line:col: U+XXXX \"char\" context"
    echo
  } >> "$REPORT_FILE"
fi

# ----------------------------
# Sanitization pipeline: steps 1-8 + perl replacements
# ----------------------------
sanitize_and_convert_to_tmp() {
  local in="$1"
  local out="$2"

  local cmd
  cmd=$(
    cat <<'CMD'
LC_ALL=C perl -0777 -pe '
  s/\r\n?/\n/g;                               # CRLF/CR -> LF
  s/\e\[[0-?]*[ -\/]*[@-~]//g;               # ANSI CSI
  s/\e\][^\a]*(?:\a|\e\\)//g;                # ANSI OSC
  s/\x{FEFF}//g;                             # BOM
  s/[\x{200B}\x{200C}\x{200D}]//g;           # zero-width
  s/[\x{202A}-\x{202E}\x{2066}-\x{2069}]//g; # bidi controls
  1 while s/.\x08//g;                         # overstrike (char + BS)
' "$1" \
| LC_ALL=C tr -d '\000-\010\013-\037\177'
CMD
  )

  if (( TRIM_TRAILING )); then
    cmd+=" | sed -E 's/[ \t]+$//'"
  fi
  if (( COLLAPSE_BLANKS )); then
    cmd+=" | LC_ALL=C perl -0777 -pe 's/\n{3,}/\n\n/g; s/\A\n+//; s/\n*\z/\n/;'"
  fi
  if (( EXPAND_TABS )); then
    cmd+=" | expand -t ${TABSTOP}"
  fi

  cmd+=" | perl \"$perl_script\""
  cmd+=" > \"\$2\""

  bash -c "$cmd" -- "$in" "$out"
}
export -f sanitize_and_convert_to_tmp

# ----------------------------
# File selection helper
# ----------------------------
mapfile -t uniq_exts < <(printf "%s\n" "${EXTS[@]}" | sed 's/^\.*//' | sed '/^$/d' | sort -u)
ext_allowed() {
  local f="$1"
  local ext="${f##*.}"
  for e in "${uniq_exts[@]}"; do
    [[ "$ext" == "$e" ]] && return 0
  done
  return 1
}

collect_txt_files() {
  local -n _out_arr="$1"  # nameref
  _out_arr=()

  if (( ${#POSITIONAL[@]} > 0 )); then
    for f in "${POSITIONAL[@]}"; do [[ -f "$f" ]] && _out_arr+=("$f"); done
    return 0
  fi

  if [[ -n "$GLOB" ]]; then
    for f in $GLOB; do [[ -f "$f" ]] && _out_arr+=("$f"); done
    return 0
  fi

  for f in *; do
    [[ -f "$f" ]] || continue
    ext_allowed "$f" && _out_arr+=("$f")
  done
}

# ----------------------------
# PDF conversion
# ----------------------------
pdf_to_txt_worker() {
  local pdf="$1"

  local base="${pdf##*/}"
  base="${base%.pdf}"
  local out="${PDF_OUTDIR}/${base}.txt"

  if (( ! PDF_FORCE )) && [[ -f "$out" && -s "$out" && "$out" -nt "$pdf" ]]; then
    local existing_chars
    existing_chars="$(LC_ALL=C wc -c < "$out" | tr -d ' ')"
    if [[ "$existing_chars" -lt "$PDF_MIN_CHARS" ]]; then
      echo "__SKIP_OCR__:$pdf"
    else
      echo "__SKIP__:$pdf"
    fi
    return 0
  fi

  local pages
  pages="$(pdfinfo "$pdf" 2>/dev/null | awk -F: '/^Pages/ {gsub(/^[ \t]+/,"",$2); print $2; exit}')"
  if [[ -z "${pages:-}" || ! "$pages" =~ ^[0-9]+$ ]]; then
    echo "__FAIL__:$pdf"
    return 0
  fi

  local tmp
  tmp="$(mktemp)"
  : > "$tmp"

  local -a pdftotext_args_common=(-enc UTF-8 -eol unix -nopgbrk)
  local -a pdftotext_args_mode=()
  case "$PDF_MODE" in
    raw)    pdftotext_args_mode=(-raw) ;;
    layout) pdftotext_args_mode=(-layout) ;;
  esac

  for ((p=1; p<=pages; p++)); do
    {
      printf "\n\n===== FILE: %s | PAGE: %d/%d =====\n\n" "$pdf" "$p" "$pages"
      pdftotext "${pdftotext_args_common[@]}" "${pdftotext_args_mode[@]}" -f "$p" -l "$p" -- "$pdf" - 2>/dev/null || true
    } >> "$tmp"
  done

  local chars
  chars="$(LC_ALL=C wc -c < "$tmp" | tr -d ' ')"

  mv -- "$tmp" "$out"

  if [[ "$chars" -lt "$PDF_MIN_CHARS" ]]; then
    echo "__OCR__:$pdf"
  else
    echo "__OK__:$pdf"
  fi
}
export -f pdf_to_txt_worker

pdf_to_txt() {
  need pdftotext
  need pdfinfo
  mkdir -p -- "$PDF_OUTDIR"

  local converted=0 skipped_uptodate=0 flagged_ocr=0 failed=0
  local -a flagged_files=()

  # Build PDF list (null-delimited for safety).
  # If the pattern includes '/', use -wholename (because -name matches basenames only).
  if [[ "$PDF_SRC" == *"/"* ]]; then
    local wholename="$PDF_SRC"
    [[ "$wholename" == ./* ]] || wholename="./$wholename"
    mapfile -d '' pdfs < <(find . -type f -wholename "$wholename" -print0)
  else
    mapfile -d '' pdfs < <(find . -maxdepth 1 -type f -name "$PDF_SRC" -print0)
  fi

  if (( ${#pdfs[@]} == 0 )); then
    echo "No PDFs found."
    return 0
  fi

  if (( ! WRITE_MODE )); then
    for pdf in "${pdfs[@]}"; do
      base="${pdf##*/}"; base="${base%.pdf}"
      echo "[DRY-RUN] would convert: $pdf -> ${PDF_OUTDIR}/${base}.txt"
    done
    return 0
  fi

  export PDF_OUTDIR PDF_FORCE PDF_MIN_CHARS PDF_MODE

  # Run in parallel, collect statuses
  mapfile -t results < <(
    printf "%s\0" "${pdfs[@]}" | xargs -0 -r -n 1 -P "$JOBS" bash -c 'pdf_to_txt_worker "$1"' _
  )

  for r in "${results[@]}"; do
    case "$r" in
      __OK__:* )   converted=$((converted + 1)) ;;
      __OCR__:* )  converted=$((converted + 1)); flagged_ocr=$((flagged_ocr + 1)); flagged_files+=("${r#__OCR__:}") ;;
      __SKIP__:* ) skipped_uptodate=$((skipped_uptodate + 1)) ;;
      __SKIP_OCR__:* ) skipped_uptodate=$((skipped_uptodate + 1)); flagged_ocr=$((flagged_ocr + 1)); flagged_files+=("${r#__SKIP_OCR__:}") ;;
      __FAIL__:* ) failed=$((failed + 1)) ;;
    esac
  done

  echo "PDF2TXT Mode: ${PDF_MODE}"
  echo "PDF2TXT Output dir: ${PDF_OUTDIR}"
  echo "PDF2TXT Jobs: ${JOBS}"
  echo "PDF2TXT Converted: ${converted}"
  echo "PDF2TXT Skipped (up-to-date): ${skipped_uptodate}"
  echo "PDF2TXT Flagged (likely needs OCR): ${flagged_ocr}"
  echo "PDF2TXT Failed: ${failed}"
  if (( flagged_ocr > 0 )); then
    echo "PDF2TXT OCR candidates: $(IFS=,; echo "${flagged_files[*]}")"
    echo "Tip: if scanned PDFs, run OCR first (e.g., ocrmypdf) then reconvert."
  fi
}


# ----------------------------
# Bundling (bundle-llm-corpus behavior)
# ----------------------------
bundle_corpus() {
  need find
  need sort
  need stat
  need wc
  need date

  local glob="$BUNDLE_SRC"
  local out="$BUNDLE_OUT"
  local sort_mode="$BUNDLE_SORT"
  local reverse="$BUNDLE_REVERSE"
  local no_index="$BUNDLE_NO_INDEX"
  local no_preamble="$BUNDLE_NO_PREAMBLE"
  local with_meta="$BUNDLE_WITH_META"

  local out_base
  out_base="$(basename -- "$out")"

  # Collect files.
  # If the pattern includes '/', use -wholename (because -name matches basenames only).
  if [[ "$glob" == *"/"* ]]; then
    local wholename="$glob"
    [[ "$wholename" == ./* ]] || wholename="./$wholename"

    mapfile -d '' files < <(
      find . -type f \
        -wholename "$wholename" \
        ! -name "$out_base" \
        -print0
    )
  else
    mapfile -d '' files < <(
      find . -maxdepth 1 -type f \
        -name "$glob" \
        ! -name "$out_base" \
        -print0
    )
  fi

  (( ${#files[@]} > 0 )) || { echo "No files matched for bundle: ${glob}" >&2; exit 1; }

  # Sort
  sorted_files=()
  case "$sort_mode" in
    name)
      mapfile -d '' sorted_files < <(printf '%s\0' "${files[@]}" | sort -z)
      ;;
    mtime)
      mapfile -t sorted_files < <(
        for f in "${files[@]}"; do
          printf "%012d\t%s\n" "$(stat -c %Y "$f")" "$f"
        done | sort -k1,1n | cut -f2-
      )
      ;;
    size)
      mapfile -t sorted_files < <(
        for f in "${files[@]}"; do
          printf "%012d\t%s\n" "$(stat -c %s "$f")" "$f"
        done | sort -k1,1n | cut -f2-
      )
      ;;
    *)
      echo "Invalid --bundle-sort: $sort_mode (name|mtime|size)" >&2
      exit 2
      ;;
  esac
  (( reverse )) && sorted_files=($(printf "%s\n" "${sorted_files[@]}" | tac))

  strip() { echo "${1#./}"; }
  meta_line() {
    local f="$1"
    printf "mtime=%s bytes=%s" \
      "$(date -d "@$(stat -c %Y "$f")" '+%Y-%m-%d %H:%M:%S')" \
      "$(stat -c %s "$f")"
  }

  if (( ! WRITE_MODE )); then
    echo "[DRY-RUN] would write bundle: $out"
    echo "[DRY-RUN] would include $(printf "%s\n" "${sorted_files[@]}" | wc -l | tr -d ' ') file(s) matching: $glob"
    return 0
  fi

  {
    if (( ! no_preamble )); then
      cat <<EOF
AGENT NOTES
===========

This corpus is an automatic concatenation of sanitized text files.

Each file is wrapped with explicit boundary markers to prevent
cross-file semantic bleed during LLM ingestion.

EOF
    fi

    if (( ! no_index )); then
      echo "INDEX OF FILES"
      echo "=============="
      echo
      i=1
      for f in "${sorted_files[@]}"; do
        fn="$(strip "$f")"
        if (( with_meta )); then
          echo "${i}. ${fn} ($(meta_line "$f"))"
        else
          echo "${i}. ${fn}"
        fi
        ((i++))
      done
      echo
      echo "----------------------------------------------------------------"
      echo
    fi

    for f in "${sorted_files[@]}"; do
      fn="$(strip "$f")"
      if (( with_meta )); then
        echo ">>>>> BEGIN FILE: ${fn} [$(meta_line "$f")]"
      else
        echo ">>>>> BEGIN FILE: ${fn}"
      fi
      echo
      cat -- "$f"
      [[ "$(tail -c 1 "$f" 2>/dev/null)" == $'\n' ]] || echo
      echo
      echo "<<<<< END FILE: ${fn}"
      echo
      echo "----------------------------------------------------------------"
      echo
    done
  } > "$out"

  echo "Done. Bundle written to $out"
}

# ----------------------------
# Main sanitize runner
# ----------------------------

sanitize_one_file() {
  local f="${1:-}"

  [[ -n "$f" ]] || return 0
  [[ -f "$f" ]] || return 0
  
  # Text/binary heuristic
  if ! grep -Iq . "$f"; then
    printf "__BIN__\t%s\n" "$f"
    return 0
  fi

  local size_bytes
  size_bytes=$(LC_ALL=C wc -c < "$f" | tr -d ' ')
  if (( ! ALLOW_LARGE )) && [[ "$size_bytes" -gt "$MAX_BYTES" ]]; then
    printf "__LARGE__\t%s\n" "$f"
    return 0
  fi

  local tmp
  tmp="$(mktemp)"
  sanitize_and_convert_to_tmp "$f" "$tmp"

  local after_bytes
  after_bytes=$(LC_ALL=C wc -c < "$tmp" | tr -d ' ')

  if cmp -s -- "$f" "$tmp"; then
    rm -f -- "$tmp"
    printf "__UNCHANGED__\t%s\t%s\t%s\n" "$f" "$size_bytes" "$after_bytes"

    return 0
  fi

  if (( WRITE_MODE )); then
    mv -- "$tmp" "$f"
  else
    rm -f -- "$tmp"
  fi

  printf "__CHANGED__\t%s\t%s\t%s\n" "$f" "$size_bytes" "$after_bytes"

}
export -f sanitize_one_file


sanitize_files() {
  local -a files=()
  collect_txt_files files

  local scanned=0 skipped_binary=0 skipped_large=0 unchanged=0 changed=0
  local net_delta_total=0 bytes_removed_total=0 bytes_added_total=0
  local -a modified_files=()
  local -a flagged_files=()

  # If unicode reporting is enabled, force single-thread sanitize to keep the report deterministic.
  local sanitize_jobs="$JOBS"
  if (( REPORT_ENABLED && JOBS > 1 )); then
    echo "Note: unicode reporting enabled; forcing sanitize to single-threaded for deterministic report." >&2
    sanitize_jobs=1
  fi

  export ALLOW_LARGE MAX_BYTES WRITE_MODE TRIM_TRAILING COLLAPSE_BLANKS EXPAND_TABS TABSTOP perl_script

  # Run one file per worker
  mapfile -t results < <(
    printf "%s\0" "${files[@]}" | xargs -0 -r -n 1 -P "$sanitize_jobs" bash -c 'sanitize_one_file "$1"' _
  )

  scanned=${#files[@]}

  for r in "${results[@]}"; do
    case "$r" in
      __BIN__* )   skipped_binary=$((skipped_binary + 1)) ;;
      __LARGE__* ) skipped_large=$((skipped_large + 1)) ;;
      __UNCHANGED__* )
        unchanged=$((unchanged + 1))
        IFS=$'\t' read -r tag file before after <<<"$r"
        delta=$((after - before))
        net_delta_total=$((net_delta_total + delta))
        if (( after <= before )); then
          bytes_removed_total=$((bytes_removed_total + (before - after)))
        else
          bytes_added_total=$((bytes_added_total + (after - before)))
        fi
        ;;
      __CHANGED__* )
        changed=$((changed + 1))
        IFS=$'\t' read -r tag file before after <<<"$r"
        modified_files+=("$file")
        delta=$((after - before))
        net_delta_total=$((net_delta_total + delta))
        if (( after <= before )); then
          bytes_removed_total=$((bytes_removed_total + (before - after)))
        else
          bytes_added_total=$((bytes_added_total + (after - before)))
        fi
        ;;
    esac
  done

  if (( QUIET )); then
    files_str="(none)"; (( changed > 0 )) && files_str="$(IFS=,; echo "${modified_files[*]}")"
    flagged_str="(none)"; (( ${#flagged_files[@]} > 0 )) && flagged_str="$(IFS=,; echo "${flagged_files[*]}")"
    report_str="(disabled)"; (( REPORT_ENABLED )) && report_str="$REPORT_FILE"
    echo "mode=${mode_str} scanned=${scanned} changed=${changed} unchanged=${unchanged} skipped_binary=${skipped_binary} skipped_large=${skipped_large} net_delta=${net_delta_total} removed=${bytes_removed_total} added=${bytes_added_total} files=${files_str} unicode_flagged=${flagged_str} report=${report_str}"
    return 0
  fi

  echo "Mode: ${mode_str}"
  if (( ${#POSITIONAL[@]} > 0 )); then
    echo "Scanned: ${scanned} file(s) (explicit files)"
  elif [[ -n "$GLOB" ]]; then
    echo "Scanned: ${scanned} file(s) (pattern: ${GLOB})"
  else
    echo "Scanned: ${scanned} file(s) (extensions: $(IFS=,; echo "${uniq_exts[*]}"))"
  fi
  echo "Skipped: ${skipped_binary} binary-ish, ${skipped_large} over-size (max=${MAX_BYTES} bytes; allow_large=${ALLOW_LARGE})"
  echo "Result: ${changed} changed, ${unchanged} unchanged"
  echo "Byte delta (net): ${net_delta_total} (after-before)"
  echo "Bytes removed: ${bytes_removed_total}; bytes added: ${bytes_added_total}"
  if (( changed > 0 )); then
    echo "Files modified: $(IFS=,; echo "${modified_files[*]}")"
  else
    echo "Files modified: (none)"
  fi
  if (( REPORT_ENABLED )); then
    if (( ${#flagged_files[@]} > 0 )); then
      echo "Unicode detected pre-removal in: $(IFS=,; echo "${flagged_files[*]}")"
      echo "See report: ${REPORT_FILE}"
    else
      echo "No unicode detected pre-removal."
      echo "Report: ${REPORT_FILE}"
    fi
  fi
}

# ----------------------------
# Orchestration
# ----------------------------

# If --pdf: convert PDFs first. Then sanitize extracted txt.
if (( MODE_PDF )); then
  pdf_to_txt
  # If user didn't pass --glob/positional, default to sanitizing PDF_OUTDIR/*.txt
  if (( ${#POSITIONAL[@]} == 0 )) && [[ -z "$GLOB" ]]; then
    GLOB="${PDF_OUTDIR}/*.txt"
  fi
fi

# If bundling after PDF conversion and bundle source was not explicitly set,
# default bundle source to the PDF output directory as well.
if (( MODE_PDF && MODE_BUNDLE )); then
  if [[ "$BUNDLE_SRC" == "*.txt" ]]; then
    BUNDLE_SRC="${PDF_OUTDIR}/*.txt"
  fi
fi

# If not bundle-only, run sanitize (default behavior)
# You can still run sanitize then bundle by specifying both --bundle and normal sanitize options.
if (( ! MODE_BUNDLE )) || (( MODE_BUNDLE )); then
  # Sanitize runs unless the user explicitly wants only bundling and passes no sanitize targets.
  # Heuristic: if --bundle is set and user provided --bundle-src but also provided no sanitize inputs
  # and not --pdf, we still allow sanitize to run on default *.txt (consistent with ./clean.sh).
  # This keeps the behavior predictable: default does sanitize.
  sanitize_files
fi

# If --bundle: build corpus (typically on already-sanitized files)
if (( MODE_BUNDLE )); then
  bundle_corpus
fi
