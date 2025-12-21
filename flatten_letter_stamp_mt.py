#!/usr/bin/env python3
"""
flatten_letter_stamp_mt.py

Pure-Python, multithreaded PDF flattening pipeline:
  - Renders each PDF page with PyMuPDF (burns in annotations as displayed)
  - Converts to grayscale
  - Fits to US Letter at target DPI (no cropping)
  - Stamps page numbers
  - Assembles into *_flat.pdf

Inputs:  all ./*.pdf in current directory
Outputs: <name>_flat.pdf

Notes:
- Output is image-based (text not selectable/searchable).
- Multithreading accelerates per-page rendering/processing.
"""

from __future__ import annotations

import glob
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Tuple

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont


# ---------------- Configuration (env overrides) ----------------
DPI = int(os.environ.get("DPI", "200"))  # 150 smaller, 200 balanced, 300 higher fidelity
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", str(os.cpu_count() or 4)))
SKIP_EXISTING = os.environ.get("SKIP_EXISTING", "1") == "1"

LETTER_W_IN = 8.5
LETTER_H_IN = 11.0

PN_FONT_SIZE_PX = int(os.environ.get("PN_FONT_SIZE_PX", "36"))
PN_MARGIN_BOTTOM_PX = int(os.environ.get("PN_MARGIN_BOTTOM_PX", "60"))

# Optional font override
FONT_PATH = os.environ.get("FONT_PATH", "").strip()
FONT_CANDIDATES = [
    FONT_PATH,
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def letter_px(dpi: int) -> Tuple[int, int]:
    return int(round(LETTER_W_IN * dpi)), int(round(LETTER_H_IN * dpi))


def load_font(size_px: int) -> ImageFont.ImageFont:
    for p in FONT_CANDIDATES:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, size=size_px)
            except Exception:
                pass
    return ImageFont.load_default()


def fit_to_letter_and_stamp(
    img: Image.Image,
    page_num: int,
    dpi: int,
    font: ImageFont.ImageFont,
    margin_bottom_px: int,
    no_upscale: bool = False,
) -> Image.Image:
    """
    Convert to grayscale, fit within Letter canvas, and stamp page number.
    no_upscale=True avoids enlarging small originals (less blur).
    """
    img = img.convert("L")
    W, H = letter_px(dpi)

    iw, ih = img.size
    scale = min(W / iw, H / ih)
    if no_upscale:
        scale = min(scale, 1.0)

    nw = max(1, int(iw * scale))
    nh = max(1, int(ih * scale))
    resized = img.resize((nw, nh), resample=Image.LANCZOS)

    canvas = Image.new("L", (W, H), color=255)
    x = (W - nw) // 2
    y = (H - nh) // 2
    canvas.paste(resized, (x, y))

    draw = ImageDraw.Draw(canvas)
    label = str(page_num)
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (W - tw) // 2
    ty = max(0, H - margin_bottom_px - th)
    draw.text((tx, ty), label, font=font, fill=0)

    return canvas


@dataclass(frozen=True)
class PageJob:
    pdf_path: str
    page_index: int  # 0-based


def render_page_job(job: PageJob, dpi: int) -> Image.Image:
    """
    Render a single page using PyMuPDF.

    Thread-safety strategy:
    - Each worker opens the document independently, renders one page, then closes.
      This avoids sharing a fitz.Document across threads.
    """
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)

    doc = fitz.open(job.pdf_path)
    try:
        page = doc.load_page(job.page_index)
        pix = page.get_pixmap(matrix=mat, alpha=False)  # alpha off reduces PNG-like issues
        mode = "RGB" if pix.n >= 3 else "L"
        img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        return img
    finally:
        doc.close()


def process_pdf(pdf_path: str) -> None:
    base = os.path.basename(pdf_path)
    stem, _ = os.path.splitext(base)

    if stem.endswith("_flat"):
        print(f"Skipping derivative: {base}")
        return

    out_path = f"{stem}_flat.pdf"
    if SKIP_EXISTING and os.path.exists(out_path):
        print(f"Exists, skipping: {out_path}")
        return

    # Determine page count once (single open)
    doc = fitz.open(pdf_path)
    try:
        n_pages = doc.page_count
    finally:
        doc.close()

    if n_pages == 0:
        print(f"WARNING: {base} has 0 pages; skipping.")
        return

    print(f"Processing: {base} -> {out_path} | pages={n_pages} | dpi={DPI} | workers={MAX_WORKERS}")

    font = load_font(PN_FONT_SIZE_PX)
    no_upscale = os.environ.get("NO_UPSCALE", "0") == "1"

    # Render pages concurrently
    rendered: List[Image.Image] = [None] * n_pages  # type: ignore[assignment]

    jobs = [PageJob(pdf_path=pdf_path, page_index=i) for i in range(n_pages)]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, n_pages)) as ex:
        future_map = {ex.submit(render_page_job, job, DPI): job.page_index for job in jobs}
        for fut in as_completed(future_map):
            i = future_map[fut]
            img = fut.result()
            rendered[i] = img

    # Post-process (fit+stamp) in a second threaded pass (optional, but fast)
    # Keeps rendering threads from doing extra CPU work if you prefer a clean separation.
    processed: List[Image.Image] = [None] * n_pages  # type: ignore[assignment]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, n_pages)) as ex:
        futs = []
        for i, img in enumerate(rendered, start=1):
            futs.append(ex.submit(
                fit_to_letter_and_stamp, img, i, DPI, font, PN_MARGIN_BOTTOM_PX, no_upscale
            ))
        for i, fut in enumerate(as_completed(futs), start=0):
            pass  # just drain to surface exceptions

        # Because as_completed returns out of order, run a simple ordered pass:
        # (processing is fast; this also avoids holding a mapping)
    for i in range(n_pages):
        processed[i] = fit_to_letter_and_stamp(
            rendered[i], i + 1, DPI, font, PN_MARGIN_BOTTOM_PX, no_upscale
        )

    # Assemble to PDF (Pillow)
    first, rest = processed[0], processed[1:]
    first.save(out_path, "PDF", save_all=True, append_images=rest)


def main() -> int:
    pdfs = sorted(glob.glob("./*.pdf"))
    if not pdfs:
        print("No PDFs found in current directory.")
        return 0

    # Optionally process multiple PDFs in parallel as well:
    # For simplicity/safety on memory, default is sequential PDFs, parallel pages.
    parallel_pdfs = os.environ.get("PARALLEL_PDFS", "0") == "1"

    if parallel_pdfs:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(pdfs))) as ex:
            futs = [ex.submit(process_pdf, p) for p in pdfs]
            for fut in as_completed(futs):
                fut.result()
    else:
        for p in pdfs:
            process_pdf(p)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
