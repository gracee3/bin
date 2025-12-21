#!/usr/bin/env python3
"""
redline_diff.py

Create a court-friendly redline (HTML, optionally PDF) from two versions of a complaint.

Default: TXT inputs (recommended for LaTeX-generated text).
Optional: PDF inputs (extracted to text via PyMuPDF).

Key features:
- Normalization tuned for pleadings / LaTeX-ish exports
- Anchor-based chunking (Roman numeral headings + numbered paragraphs) to avoid "all deletes then all adds"
- Inline semantic diff (diff-match-patch) so unchanged text is plain; edits are localized
- HTML output with red strikethrough deletions and blue underlined insertions
- Optional HTML -> PDF via WeasyPrint

Dependencies:
  pip install diff-match-patch
Optional:
  pip install weasyprint
  pip install pymupdf

Usage (TXT default):
  python redline_diff.py old.txt new.txt --out_html redline.html --out_pdf redline.pdf

Usage (PDF):
  python redline_diff.py old.pdf new.pdf --pdf --out_html redline.html --out_pdf redline.pdf
"""

from __future__ import annotations

import argparse
import html
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from diff_match_patch import diff_match_patch


# -------------------------
# Input handling
# -------------------------

def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text using PyMuPDF. Works best for text-based PDFs.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise SystemExit("Missing dependency: pymupdf. Install with: pip install pymupdf") from e

    doc = fitz.open(str(pdf_path))
    chunks: List[str] = []
    for i in range(len(doc)):
        page = doc.load_page(i)
        chunks.append(page.get_text("text"))
    doc.close()
    return "\n".join(chunks)


def load_input(path: Path, force_pdf: bool = False) -> str:
    if force_pdf or path.suffix.lower() == ".pdf":
        return extract_text_from_pdf(path)
    return read_text_file(path)


# -------------------------
# Normalization
# -------------------------

@dataclass
class NormalizeOptions:
    # Remove simple "Page X of Y" markers
    drop_page_markers: bool = True
    # Normalize unicode (stabilize characters)
    normalize_unicode: bool = True
    # Normalize TeX-style quotes/dashes
    normalize_tex_punct: bool = True
    # Dehyphenate line-broken words
    dehyphenate: bool = True
    # Collapse extra spaces
    collapse_spaces: bool = True
    # Normalize blank lines to at most one empty line between blocks
    normalize_blank_lines: bool = True


_PAGE_OF_RE = re.compile(r"^\s*Page\s+\d+\s+of\s+\d+\s*$", re.IGNORECASE | re.MULTILINE)


def normalize_latexish_text(s: str, opt: NormalizeOptions) -> str:
    """
    Normalization tuned for LaTeX-generated or pleading-like text exports.
    Keeps structure while reducing non-material differences that break anchoring.
    """
    if opt.normalize_unicode:
        s = unicodedata.normalize("NFKC", s)

    s = s.replace("\r\n", "\n").replace("\r", "\n")

    if opt.drop_page_markers:
        s = _PAGE_OF_RE.sub("", s)

    if opt.normalize_tex_punct:
        # TeX quotes -> plain quotes
        s = s.replace("``", '"').replace("''", '"')
        # Normalize common dash conventions consistently
        # (Pick characters that print well in HTML/PDF; also improves diff stability.)
        s = s.replace("---", "—").replace("--", "–")

    if opt.dehyphenate:
        # "inter-\nnational" -> "international"
        s = re.sub(r"(\w)-\n(\w)", r"\1\2", s)

    if opt.collapse_spaces:
        s = s.replace("\u00A0", " ")
        s = re.sub(r"[ \t]+", " ", s)
        s = re.sub(r"[ \t]+\n", "\n", s)

    if opt.normalize_blank_lines:
        s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)

    return s.strip()


# -------------------------
# Anchor chunking
# -------------------------

ANCHOR_RE = re.compile(
    r"""
    (?m)                               # multiline
    ^\s*[IVXLCDM]+\.\s+.+$             # Roman numeral heading like "I. INTRODUCTION"
    |
    ^\s*\d+\.\s+                       # numbered paragraph like "23. "
    """,
    re.VERBOSE
)


def split_by_anchors(text: str) -> List[Tuple[str, str]]:
    """
    Returns a list of (anchor, chunk_text) where anchor is the first line of the chunk.
    This provides stable alignment even when the body of the paragraph changes.
    """
    matches = list(ANCHOR_RE.finditer(text))
    if not matches:
        return [("DOCUMENT", text)]

    chunks: List[Tuple[str, str]] = []
    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        chunk = text[start:end].strip("\n")
        anchor = chunk.split("\n", 1)[0].strip()
        chunks.append((anchor, chunk))
    return chunks


# -------------------------
# Diff rendering
# -------------------------

def dmp_inline_html(old: str, new: str, timeout_s: float = 2.0) -> str:
    """
    Inline semantic diff: unchanged text plain; deletions red strike; insertions blue underline.
    """
    dmp = diff_match_patch()
    dmp.Diff_Timeout = timeout_s

    diffs = dmp.diff_main(old, new)
    dmp.diff_cleanupSemantic(diffs)
    dmp.diff_cleanupEfficiency(diffs)

    out: List[str] = []
    for op, data in diffs:
        if not data:
            continue
        esc = html.escape(data)
        if op == 0:
            out.append(esc)
        elif op == -1:
            out.append(f'<del class="del">{esc}</del>')
        elif op == 1:
            out.append(f'<ins class="ins">{esc}</ins>')
    return "".join(out)


def nl_to_html(s: str) -> str:
    """Convert newlines to readable HTML while preserving paragraphs."""
    return s.replace("\n\n", "</p><p>").replace("\n", "<br>")

def wrap_p(inner: str) -> str:
    """Wrap content in a paragraph block."""
    return f"<p>{inner}</p>"



HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Redline Comparison</title>
<style>
  body {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11pt;
    line-height: 1.25;
    margin: 1in;
  }}
  h1 {{
    font-size: 14pt;
    margin: 0 0 0.25in 0;
  }}
  .meta {{
    font-size: 9.5pt;
    color: #444;
    margin-bottom: 0.25in;
  }}
  p {{
    margin: 0 0 0.12in 0;
  }}
  del.del {{
    color: #b00020;
    text-decoration: line-through;
  }}
  ins.ins {{
    color: #0b57d0;
    text-decoration: underline;
  }}
  .blockdel {{
    border-left: 3px solid #b00020; padding-left: 10px;
  }}
  .blockins {{
    border-left: 3px solid #0b57d0; padding-left: 10px;
  }}
  .sep {{
    margin: 0.18in 0;
    border-top: 1px solid #ddd;
  }}
</style>
</head>
<body>
  <h1>Redline Comparison</h1>
  <div class="meta">
    <div><strong>Old:</strong> {old_name}</div>
    <div><strong>New:</strong> {new_name}</div>
  </div>
  {body}
</body>
</html>
"""


def build_redline_html(old_text: str, new_text: str, old_name: str, new_name: str, timeout_s: float = 2.0) -> str:
    """
    Anchor-align by heading/paragraph marker, then do inline semantic diff per chunk.
    Unchanged text is plain; deletions are red strikethrough; insertions are blue underline.
    """
    old_chunks = split_by_anchors(old_text)
    new_chunks = split_by_anchors(new_text)

    # Map new chunks by anchor; keep first occurrence.
    new_map: Dict[str, str] = {}
    for a, c in new_chunks:
        new_map.setdefault(a, c)

    used_new: Set[str] = set()
    out_parts: List[str] = []

    for a_old, c_old in old_chunks:
        c_new = new_map.get(a_old)
        if c_new is None:
            # Entire chunk deleted
            deleted_html = f'<del class="del">{html.escape(c_old)}</del>'
            out_parts.append(f'<div class="blockdel">{wrap_p(nl_to_html(deleted_html))}</div>')
        else:
            used_new.add(a_old)
            merged = dmp_inline_html(c_old, c_new, timeout_s=timeout_s)
            out_parts.append(wrap_p(nl_to_html(merged)))

        out_parts.append('<div class="sep"></div>')

    # Any chunks present only in new are inserts
    for a_new, c_new in new_chunks:
        if a_new not in used_new:
            added_html = f'<ins class="ins">{html.escape(c_new)}</ins>'
            out_parts.append(f'<div class="blockins">{wrap_p(nl_to_html(added_html))}</div>')
            out_parts.append('<div class="sep"></div>')

    body = "\n".join(out_parts)
    return HTML_TEMPLATE.format(
        old_name=html.escape(old_name),
        new_name=html.escape(new_name),
        body=body,
    )


# -------------------------
# Optional HTML -> PDF
# -------------------------

def html_to_pdf(html_str: str, out_pdf: Path) -> None:
    try:
        from weasyprint import HTML  # type: ignore
    except ImportError as e:
        raise SystemExit("Missing dependency: weasyprint. Install with: pip install weasyprint") from e

    HTML(string=html_str).write_pdf(str(out_pdf))


# -------------------------
# Main
# -------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Create a redline HTML/PDF from two TXT (default) or PDF inputs.")
    ap.add_argument("old", type=Path, help="Old version (.txt default; .pdf if --pdf or file ends with .pdf)")
    ap.add_argument("new", type=Path, help="New version (.txt default; .pdf if --pdf or file ends with .pdf)")
    ap.add_argument("--pdf", action="store_true", help="Force treat inputs as PDFs (extract text).")
    ap.add_argument("--out_html", type=Path, default=Path("redline.html"))
    ap.add_argument("--out_pdf", type=Path, default=None)
    ap.add_argument("--keep_txt", action="store_true", help="Write normalized text files for inspection.")
    ap.add_argument("--timeout", type=float, default=2.0, help="Diff timeout seconds for diff-match-patch.")
    args = ap.parse_args()

    old_raw = load_input(args.old, force_pdf=args.pdf)
    new_raw = load_input(args.new, force_pdf=args.pdf)

    norm_opt = NormalizeOptions()
    old_norm = normalize_latexish_text(old_raw, norm_opt)
    new_norm = normalize_latexish_text(new_raw, norm_opt)

    if args.keep_txt:
        args.out_html.with_suffix(".old.normalized.txt").write_text(old_norm, encoding="utf-8")
        args.out_html.with_suffix(".new.normalized.txt").write_text(new_norm, encoding="utf-8")

    html_str = build_redline_html(
        old_text=old_norm,
        new_text=new_norm,
        old_name=args.old.name,
        new_name=args.new.name,
        timeout_s=args.timeout,
    )

    args.out_html.write_text(html_str, encoding="utf-8")

    if args.out_pdf is not None:
        html_to_pdf(html_str, args.out_pdf)

    print(f"Wrote HTML: {args.out_html}")
    if args.out_pdf is not None:
        print(f"Wrote PDF:  {args.out_pdf}")


if __name__ == "__main__":
    main()
