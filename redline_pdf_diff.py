#!/usr/bin/env python3
"""
redline_pdf_diff.py

Creates a "redline" comparison from two PDFs:
- Extracts text from each PDF
- Normalizes to reduce PDF artifacts and ignore whitespace-only differences
- Aligns by paragraph (to avoid "everything deleted then everything added")
- Diffs within aligned paragraphs at word level (to localize changes)
- Outputs HTML with red strikethrough deletions and blue underlined additions
- Optionally converts HTML -> PDF (requires WeasyPrint)

Dependencies:
  pip install pymupdf weasyprint

Usage:
  python redline_pdf_diff.py old.pdf new.pdf --out_html redline.html --out_pdf redline.pdf
"""

from __future__ import annotations

import argparse
import html
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Tuple, Optional

# -------------------------
# PDF extraction
# -------------------------

def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text using PyMuPDF. This is generally reliable for text-based PDFs.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise SystemExit("Missing dependency: pymupdf. Install with: pip install pymupdf") from e

    doc = fitz.open(str(pdf_path))
    chunks = []
    for i in range(len(doc)):
        page = doc.load_page(i)
        # "text" is usually fine; for tricky layouts, "blocks" can be used.
        t = page.get_text("text")
        chunks.append(t)
    doc.close()
    return "\n".join(chunks)

# -------------------------
# Normalization
# -------------------------

@dataclass
class NormalizeConfig:
    remove_page_numbers: bool = True
    remove_repeated_headers_footers: bool = True
    dehyphenate_linebreaks: bool = True
    unwrap_hard_linebreaks: bool = True
    normalize_unicode: bool = True
    normalize_quotes_dashes: bool = True
    collapse_whitespace: bool = True
    drop_empty_lines: bool = True


def normalize_text(raw: str, cfg: NormalizeConfig) -> str:
    """
    Normalize extracted PDF text to improve diff quality and ignore non-material artifacts.
    """
    text = raw

    # Normalize unicode to reduce “same glyph, different codepoint” issues.
    if cfg.normalize_unicode:
        text = unicodedata.normalize("NFKC", text)

    # Standardize quotes/dashes (PDFs often vary between hyphen/en-dash/em-dash).
    if cfg.normalize_quotes_dashes:
        # Curly quotes -> straight
        text = text.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
        # Common dash variants -> hyphen or emdash token
        text = text.replace("–", "-").replace("—", "-")

    # Remove form-feed and other odd control chars (except \n and \t).
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)

    # Normalize line endings.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove obvious page numbers like "Page 1 of 10" or standalone "1"
    if cfg.remove_page_numbers:
        lines = text.split("\n")
        new_lines = []
        for ln in lines:
            s = ln.strip()
            # "Page X" / "Page X of Y"
            if re.fullmatch(r"Page\s+\d+(\s+of\s+\d+)?", s, re.IGNORECASE):
                continue
            # standalone digits (often page number)
            if re.fullmatch(r"\d{1,4}", s):
                continue
            new_lines.append(ln)
        text = "\n".join(new_lines)

    # De-hyphenate words split across line breaks: "inter-\nnational" -> "international"
    if cfg.dehyphenate_linebreaks:
        text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)

    # Collapse multiple spaces/tabs (but preserve paragraph breaks for now).
    if cfg.collapse_whitespace:
        # Replace non-breaking spaces
        text = text.replace("\u00A0", " ")
        # Collapse horizontal whitespace runs
        text = re.sub(r"[ \t]+", " ", text)

    # Optionally unwrap hard line breaks that are not paragraph boundaries.
    # Heuristic: treat blank lines as paragraph separators; within a paragraph, turn newlines into spaces.
    if cfg.unwrap_hard_linebreaks:
        # First, standardize multiple blank lines to exactly two newlines.
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        paras = [p for p in re.split(r"\n\s*\n", text)]
        rebuilt = []
        for p in paras:
            # Within paragraph, replace newlines with spaces.
            p2 = re.sub(r"\s*\n\s*", " ", p.strip())
            rebuilt.append(p2)
        text = "\n\n".join([p for p in rebuilt if p or not cfg.drop_empty_lines])

    # Drop purely empty lines if requested.
    if cfg.drop_empty_lines:
        text = "\n".join([ln for ln in text.split("\n") if ln.strip() != ""])

    # Remove repeated headers/footers (best-effort):
    # Find lines that repeat many times and are short-ish; drop them.
    if cfg.remove_repeated_headers_footers:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if len(lines) >= 50:
            freq = {}
            for ln in lines:
                if 3 <= len(ln) <= 80:
                    freq[ln] = freq.get(ln, 0) + 1
            # Consider "repeated" anything appearing on >= 10% of lines, capped to avoid over-removal
            threshold = max(5, int(0.10 * len(lines)))
            repeated = {ln for ln, c in freq.items() if c >= threshold}
            if repeated:
                kept = []
                for ln in text.split("\n"):
                    if ln.strip() in repeated:
                        continue
                    kept.append(ln)
                text = "\n".join(kept)

    # Final cleanup: normalize paragraph spacing again.
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()

    return text

# -------------------------
# Paragraph splitting and alignment
# -------------------------

def split_paragraphs(text: str) -> List[str]:
    """
    Paragraphs separated by blank line (double newline).
    """
    if not text.strip():
        return []
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def paragraph_similarity(a: str, b: str) -> float:
    """
    Similarity score to help align paragraphs even when they changed.
    """
    return SequenceMatcher(None, a, b).ratio()


def align_paragraphs(old_paras: List[str], new_paras: List[str]) -> List[Tuple[Optional[str], Optional[str]]]:
    """
    Align paragraphs using SequenceMatcher over paragraph lists.
    Produces pairs (old_para or None, new_para or None).
    """
    sm = SequenceMatcher(None, old_paras, new_paras, autojunk=False)
    aligned: List[Tuple[Optional[str], Optional[str]]] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                aligned.append((old_paras[i1 + k], new_paras[j1 + k]))
        elif tag == "delete":
            for k in range(i1, i2):
                aligned.append((old_paras[k], None))
        elif tag == "insert":
            for k in range(j1, j2):
                aligned.append((None, new_paras[k]))
        elif tag == "replace":
            # Try to pair up replaced paragraphs by best similarity rather than dumping as big blocks.
            olds = old_paras[i1:i2]
            news = new_paras[j1:j2]
            # Greedy pairing: for each old, match closest new if above a threshold.
            used_new = set()
            for o in olds:
                best_j = None
                best_score = 0.0
                for idx, n in enumerate(news):
                    if idx in used_new:
                        continue
                    s = paragraph_similarity(o, n)
                    if s > best_score:
                        best_score = s
                        best_j = idx
                if best_j is not None and best_score >= 0.35:
                    used_new.add(best_j)
                    aligned.append((o, news[best_j]))
                else:
                    aligned.append((o, None))
            # Any remaining new paragraphs are inserts.
            for idx, n in enumerate(news):
                if idx not in used_new:
                    aligned.append((None, n))
        else:
            raise RuntimeError(f"Unhandled opcode: {tag}")

    return aligned

# -------------------------
# Word-level diff within aligned paragraphs
# -------------------------

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)

def tokenize_for_diff(s: str) -> List[str]:
    """
    Tokenize into word and punctuation tokens, ignoring whitespace.
    This makes whitespace-only changes invisible and localizes edits.
    """
    return _TOKEN_RE.findall(s)


def diff_tokens_to_html(old: str, new: str) -> str:
    """
    Produce HTML for a paragraph pair, with <del> and <ins>.
    """
    old_toks = tokenize_for_diff(old)
    new_toks = tokenize_for_diff(new)

    sm = SequenceMatcher(None, old_toks, new_toks, autojunk=False)
    parts: List[str] = []

    def emit_text(tokens: List[str]) -> str:
        # Reconstruct with spacing rules: add a space between two alnum tokens; otherwise no space by default.
        out = []
        prev = ""
        for t in tokens:
            if out:
                if (prev.isalnum() and t.isalnum()):
                    out.append(" ")
                # also space after sentence-ending punctuation when followed by word
                elif prev in [".", "!", "?", ":", ";"] and t.isalnum():
                    out.append(" ")
            out.append(t)
            prev = t
        return "".join(out)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            parts.append(html.escape(emit_text(old_toks[i1:i2])))
        elif tag == "delete":
            deleted = html.escape(emit_text(old_toks[i1:i2]))
            if deleted.strip():
                parts.append(f'<del class="del">{deleted}</del>')
        elif tag == "insert":
            added = html.escape(emit_text(new_toks[j1:j2]))
            if added.strip():
                parts.append(f'<ins class="ins">{added}</ins>')
        elif tag == "replace":
            deleted = html.escape(emit_text(old_toks[i1:i2]))
            added = html.escape(emit_text(new_toks[j1:j2]))
            if deleted.strip():
                parts.append(f'<del class="del">{deleted}</del>')
            if added.strip():
                parts.append(f'<ins class="ins">{added}</ins>')

    # Minor cleanup: collapse spaces around tags
    return "".join(parts).strip()

# -------------------------
# HTML + (optional) PDF output
# -------------------------

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
    color: #b00020;              /* red */
    text-decoration: line-through;
  }}
  ins.ins {{
    color: #0b57d0;              /* blue */
    text-decoration: underline;
  }}
  .blockdel {{
    border-left: 3px solid #b00020;
    padding-left: 10px;
  }}
  .blockins {{
    border-left: 3px solid #0b57d0;
    padding-left: 10px;
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

def build_redline_html(old_text: str, new_text: str, old_name: str, new_name: str) -> str:
    old_paras = split_paragraphs(old_text)
    new_paras = split_paragraphs(new_text)

    aligned = align_paragraphs(old_paras, new_paras)

    out_parts: List[str] = []
    for old_p, new_p in aligned:
        if old_p is not None and new_p is not None:
            # Word-level diff within aligned paragraphs
            para_html = diff_tokens_to_html(old_p, new_p)
            if para_html:
                out_parts.append(f"<p>{para_html}</p>")
        elif old_p is not None and new_p is None:
            # Entire paragraph deleted
            out_parts.append(f'<p class="blockdel"><del class="del">{html.escape(old_p)}</del></p>')
        elif old_p is None and new_p is not None:
            # Entire paragraph added
            out_parts.append(f'<p class="blockins"><ins class="ins">{html.escape(new_p)}</ins></p>')

        # Optional separator between paragraphs to enhance readability
        out_parts.append('<div class="sep"></div>')

    body = "\n".join(out_parts)
    return HTML_TEMPLATE.format(old_name=html.escape(old_name), new_name=html.escape(new_name), body=body)


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
    ap = argparse.ArgumentParser(description="Create a redline HTML/PDF from two PDF files.")
    ap.add_argument("old_pdf", type=Path)
    ap.add_argument("new_pdf", type=Path)
    ap.add_argument("--out_html", type=Path, default=Path("redline.html"))
    ap.add_argument("--out_pdf", type=Path, default=None)
    ap.add_argument("--keep_txt", action="store_true", help="Save normalized txt alongside output for inspection.")
    args = ap.parse_args()

    old_raw = extract_text_from_pdf(args.old_pdf)
    new_raw = extract_text_from_pdf(args.new_pdf)

    cfg = NormalizeConfig(
        remove_page_numbers=True,
        remove_repeated_headers_footers=True,
        dehyphenate_linebreaks=True,
        unwrap_hard_linebreaks=True,
        normalize_unicode=True,
        normalize_quotes_dashes=True,
        collapse_whitespace=True,
        drop_empty_lines=True,
    )

    old_norm = normalize_text(old_raw, cfg)
    new_norm = normalize_text(new_raw, cfg)

    if args.keep_txt:
        args.out_html.with_suffix(".old.normalized.txt").write_text(old_norm, encoding="utf-8")
        args.out_html.with_suffix(".new.normalized.txt").write_text(new_norm, encoding="utf-8")

    html_str = build_redline_html(
        old_text=old_norm,
        new_text=new_norm,
        old_name=args.old_pdf.name,
        new_name=args.new_pdf.name,
    )

    args.out_html.write_text(html_str, encoding="utf-8")

    if args.out_pdf is not None:
        html_to_pdf(html_str, args.out_pdf)

    print(f"Wrote HTML: {args.out_html}")
    if args.out_pdf is not None:
        print(f"Wrote PDF:  {args.out_pdf}")


if __name__ == "__main__":
    main()
