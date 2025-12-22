#!/usr/bin/env python3
"""
redline_txt_diff.py

Create an inline, court-friendly redline HTML from two TXT versions of a complaint.

- Normalizes LaTeX-ish text (drops page markers, drops repeating 5AC footer, normalizes TeX punctuation)
- Anchor-based chunking (Roman numeral headings + numbered paragraphs)
- Aligns chunks in document order to avoid "all red then all blue"
- Inline semantic diff (diff-match-patch): unchanged text plain; deletions red strike; insertions blue underline
- Outputs HTML (no PDF support in this version)

Dependencies:
  pip install diff-match-patch

Usage:
  python redline_txt_diff.py 5ac.txt 6ac.txt --out_html redline.html --keep_txt
"""

from __future__ import annotations

import argparse
import html
import re
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple

from difflib import SequenceMatcher
from diff_match_patch import diff_match_patch


# -------------------------
# Input
# -------------------------

def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# -------------------------
# Normalization (targeted to your 5ac/6ac artifacts)
# -------------------------

# Matches "Page 1 of 77" / "Page 80 of 80" etc
RE_PAGE_OF = re.compile(r"^\s*Page\s+\d+\s+of\s+\d+\s*$", re.IGNORECASE | re.MULTILINE)

# Matches the repeating footer/caption line in 5ac that starts with "Clark v."
# (tolerates hyphen variants and spacing differences)
RE_CLARK_FOOTER = re.compile(
    r"(?im)^\s*Clark\s+v\.\s+District\s+of\s+Columbia.*Complaint\s*$"
)

RE_ECF_HEADER_1LINE = re.compile(
    r"(?im)^\s*Case\s+\S+\s+Document\s+\d+\s+Filed\s+\d{2}/\d{2}/\d{2}\s+Page\s+\d+\s+of\s+\d+\s*$"
)

# Handles the two-line split seen in 14-0.raw.txt (Document line, then Filed/Page line)
RE_ECF_HEADER_2LINE = re.compile(
    r"(?im)^\s*Case\s+\S+\s+Document\s+\d+\s*\n\s*Filed\s+\d{2}/\d{2}/\d{2}\s+Page\s+\d+\s+of\s+\d+\s*$"
)

# Removes odd zero-width chars that sometimes come from PDF text extraction
RE_ZWSP = re.compile(r"[\u200b\u200c\u200d\uFEFF]")


def normalize_latexish_text(s: str) -> str:
    """
    Normalization tuned for LaTeX-ish complaint exports:
    - Remove page markers "Page X of Y"
    - Remove repeating 5AC footer "Clark v. District of Columbia ... Complaint"
    - Normalize TeX quotes/dashes
    - Dehyphenate line-break hyphenation
    - Collapse extraneous whitespace / blank lines
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")

    s = RE_ZWSP.sub("", s)
    s = RE_ECF_HEADER_2LINE.sub("", s)
    s = RE_ECF_HEADER_1LINE.sub("", s)

    # Drop page markers
    s = RE_PAGE_OF.sub("", s)

    # Drop repeating 5ac footer/caption
    s = RE_CLARK_FOOTER.sub("", s)

    # Normalize TeX quotes to straight quotes
    s = s.replace("``", '"').replace("''", '"')

    # Normalize TeX dash conventions consistently
    s = s.replace("---", "—").replace("--", "–")

    # Dehyphenate words split across line breaks
    s = re.sub(r"(\w)-\n(\w)", r"\1\2", s)

    # Collapse horizontal whitespace
    s = s.replace("\u00A0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"[ \t]+\n", "\n", s)

    # Normalize blank lines
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)

    return s.strip()


# -------------------------
# Anchor chunking + alignment (for interleaved red/blue output)
# -------------------------

ANCHOR_RE = re.compile(
    r"""
    (?m)
    ^\s*COUNT\s+[IVXLCDM]+\b.*$          # COUNT I – ...
    |
    ^\s*[IVXLCDM]+\.\s+.+$               # I. INTRODUCTION
    |
    ^\s*\d+\.\s+                         # 23. ...
    |
    ^\s*[A-Z][A-Z &/\-]{3,}\s*$          # INTRODUCTION / JURISDICTION & VENUE / PROCEDURAL POSTURE
    """,
    re.VERBOSE | re.IGNORECASE
)

def split_by_anchors(text: str) -> List[Tuple[str, str]]:
    """
    Returns list of (anchor, chunk_text) where anchor is the first line of the chunk.
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


def align_chunks(
    old_chunks: List[Tuple[str, str]],
    new_chunks: List[Tuple[str, str]],
    similarity_threshold: float = 0.35,
) -> List[Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]]]]:
    """
    Align chunks in document order to produce interleaved (old?, new?) pairs.
    This avoids dumping all deletes then all inserts.
    """
    old_anchors = [a for a, _ in old_chunks]
    new_anchors = [a for a, _ in new_chunks]

    sm = SequenceMatcher(None, old_anchors, new_anchors, autojunk=False)
    aligned: List[Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]]]] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                aligned.append((old_chunks[i1 + k], new_chunks[j1 + k]))

        elif tag == "delete":
            for k in range(i1, i2):
                aligned.append((old_chunks[k], None))

        elif tag == "insert":
            for k in range(j1, j2):
                aligned.append((None, new_chunks[k]))

        elif tag == "replace":
            olds = old_chunks[i1:i2]
            news = new_chunks[j1:j2]

            used_new = set()
            for o in olds:
                best_j = None
                best_score = 0.0
                for idx, n in enumerate(news):
                    if idx in used_new:
                        continue
                    score = SequenceMatcher(None, o[1], n[1], autojunk=False).ratio()
                    if score > best_score:
                        best_score = score
                        best_j = idx

                if best_j is not None and best_score >= similarity_threshold:
                    used_new.add(best_j)
                    aligned.append((o, news[best_j]))
                else:
                    aligned.append((o, None))

            for idx, n in enumerate(news):
                if idx not in used_new:
                    aligned.append((None, n))

        else:
            raise RuntimeError(f"Unhandled opcode: {tag}")

    return aligned


# -------------------------
# Diff rendering
# -------------------------

def dmp_inline_html(old: str, new: str, timeout_s: float = 2.0) -> str:
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
    return s.replace("\n\n", "</p><p>").replace("\n", "<br>")


def wrap_p(inner: str) -> str:
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
    background: rgba(176, 0, 32, 0.12);
  }}
  ins.ins {{
    color: #0b57d0;
    text-decoration: underline;
    background: rgba(11, 87, 208, 0.12);
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


def build_redline_html(old_text: str, new_text: str, old_name: str, new_name: str, timeout_s: float = 2.0) -> str:
    old_chunks = split_by_anchors(old_text)
    new_chunks = split_by_anchors(new_text)
    pairs = align_chunks(old_chunks, new_chunks, similarity_threshold=0.35)

    out_parts: List[str] = []

    for old_item, new_item in pairs:
        if old_item is not None and new_item is not None:
            merged = dmp_inline_html(old_item[1], new_item[1], timeout_s=timeout_s)
            out_parts.append(wrap_p(nl_to_html(merged)))

        elif old_item is not None and new_item is None:
            deleted = f'<del class="del">{html.escape(old_item[1])}</del>'
            out_parts.append(f'<div class="blockdel">{wrap_p(nl_to_html(deleted))}</div>')

        elif old_item is None and new_item is not None:
            added = f'<ins class="ins">{html.escape(new_item[1])}</ins>'
            out_parts.append(f'<div class="blockins">{wrap_p(nl_to_html(added))}</div>')

        out_parts.append('<div class="sep"></div>')

    body = "\n".join(out_parts)
    return HTML_TEMPLATE.format(
        old_name=html.escape(old_name),
        new_name=html.escape(new_name),
        body=body,
    )


# -------------------------
# Main
# -------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Create a redline HTML from two TXT files.")
    ap.add_argument("old", type=Path)
    ap.add_argument("new", type=Path)
    ap.add_argument("--out_html", type=Path, default=Path("redline.html"))
    ap.add_argument("--keep_txt", action="store_true", help="Write normalized text files for inspection.")
    ap.add_argument("--timeout", type=float, default=2.0, help="Diff timeout seconds for diff-match-patch.")
    args = ap.parse_args()

    old_raw = read_text_file(args.old)
    new_raw = read_text_file(args.new)

    old_norm = normalize_latexish_text(old_raw)
    new_norm = normalize_latexish_text(new_raw)

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
    print(f"Wrote HTML: {args.out_html}")


if __name__ == "__main__":
    main()
