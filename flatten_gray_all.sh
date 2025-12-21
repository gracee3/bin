#!/usr/bin/env bash
# flatten_gray_all.sh
#
# Flatten PDFs (burn in annotations as displayed) by rasterizing each page to grayscale PNG,
# then assembling back into *_flat.pdf. Works reliably even when GS fails on PNG input.
#
# Inputs:  ./*.pdf
# Outputs: ./*_flat.pdf
#
# Defaults are a good balance for exhibits:
#   DPI=200
#   optional GS optimization on the final PDF (more reliable than GS reading PNGs)

set -euo pipefail
shopt -s nullglob

DPI="${DPI:-200}"                    # 150 smaller, 200 balanced, 300 higher quality/larger
OPTIMIZE="${OPTIMIZE:-1}"            # 1 to run GS optimize pass on final PDF, 0 to skip
PDFSETTINGS="${PDFSETTINGS:-/ebook}" # /screen /ebook /printer /prepress

infiles=( ./*.pdf )
if ((${#infiles[@]} == 0)); then
  echo "No PDFs found in current directory."
  exit 0
fi

command -v mutool >/dev/null 2>&1 || { echo "ERROR: mutool not found (install mupdf-tools)." >&2; exit 1; }
python3 - <<'PY' >/dev/null 2>&1 || { echo "ERROR: python3 not available." >&2; exit 1; }
PY

# Pillow is required for the assemble step.
python3 - <<'PY' >/dev/null 2>&1 || { echo "ERROR: Pillow not installed. Install with: python3 -m pip install --user pillow" >&2; exit 1; }
from PIL import Image  # noqa: F401
PY

have_gs=0
command -v gs >/dev/null 2>&1 && have_gs=1

for inpdf in "${infiles[@]}"; do
  base="$(basename "$inpdf")"
  stem="${base%.pdf}"

  if [[ "$stem" == *_flat ]]; then
    echo "Skipping already-flat: $base"
    continue
  fi

  outpdf="./${stem}_flat.pdf"
  if [[ -f "$outpdf" ]]; then
    echo "Exists, skipping: $(basename "$outpdf")"
    continue
  fi

  echo "Flattening: $base -> $(basename "$outpdf") (DPI=${DPI}, grayscale)"

  tmpdir="$(mktemp -d)"
  trap 'rm -rf "$tmpdir"' RETURN

  # 1) Render each page to grayscale PNG (this is what burns in annotations)
  # -N disables ICC workflow; avoids ICC warning and reduces color-management variability
  mutool draw \
    -q \
    -N \
    -r "$DPI" \
    -c gray \
    -F png \
    -o "$tmpdir/page-%04d.png" \
    "$inpdf"

  # 2) Assemble PNGs into a PDF using Pillow (robust; avoids GS PNG parser issues)
  python3 - "$tmpdir" "$outpdf" <<'PY'
import sys, glob
from PIL import Image

tmpdir = sys.argv[1]
outpdf = sys.argv[2]

paths = sorted(glob.glob(f"{tmpdir}/page-*.png"))
if not paths:
    raise SystemExit("No rendered pages found.")

imgs = []
for p in paths:
    im = Image.open(p)
    # Ensure 8-bit grayscale (some PNGs may still be LA or palette)
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGB")
    im = im.convert("L")
    imgs.append(im)

first, rest = imgs[0], imgs[1:]
# resolution is stored as metadata sometimes, but PDF viewers don’t rely on it consistently.
# The DPI choice is already “baked” into pixel dimensions.
first.save(outpdf, "PDF", save_all=True, append_images=rest)
PY

  # 3) Optional: optimize the final PDF with Ghostscript (more reliable than feeding PNGs)
  if [[ "$OPTIMIZE" == "1" && $have_gs -eq 1 ]]; then
    tmpopt="$tmpdir/opt.pdf"
    gs -q -dSAFER -dNOPAUSE -dBATCH \
      -sDEVICE=pdfwrite \
      -dCompatibilityLevel=1.7 \
      -dPDFSETTINGS="$PDFSETTINGS" \
      -sColorConversionStrategy=Gray \
      -dProcessColorModel=/DeviceGray \
      -dDetectDuplicateImages=true \
      -o "$tmpopt" \
      "$outpdf"
    mv "$tmpopt" "$outpdf"
  fi

  rm -rf "$tmpdir"
  trap - RETURN
done

echo "Done. Created *_flat.pdf files."
