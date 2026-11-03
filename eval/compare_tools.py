"""Scores each tool's Markdown against the PDF itself, per element type.

Usage:
    python eval/compare_tools.py paper.pdf engine=paper_engine.md markitdown=paper_markitdown.md docling=paper_docling.md

Download each .md from the app (Compare tools tab). No AWS keys are needed.

Per element type:
    text      recall: share of the PDF's text found in the output; precision: share of the output's text
              that is really in the PDF (matched in runs of 5 letters, so spacing does not matter)
    headings  Markdown headings in the output, and how many of them are real (their text is in the PDF)
    tables    tables in the output, next to the tables pdfplumber finds in the PDF
    formulas  formulas written as LaTeX ($...$ or $$...$$)
    images    image links, next to the figures and pictures in the PDF
Prints a table, and writes eval/compare_tools_report.md.
"""
import re
import sys
from collections import Counter
from pathlib import Path

import pdfplumber


def norm(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def grams(text, n=5):
    """Overlapping runs of n letters or digits. Spaces are ignored, so a PDF whose words are glued
    together (or an output that splits them) is still compared fairly."""
    t = norm(text)
    return Counter(t[i:i + n] for i in range(max(len(t) - n + 1, 0)))


def reference(pdf_path):
    words, headings, tables, figures = Counter(), [], 0, 0
    full = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            full.append(norm(page.extract_text() or ""))
            words.update(grams(page.extract_text() or ""))
            try:
                tables += len([t for t in page.find_tables() if len(t.extract() or []) >= 2])
            except Exception:
                pass
            figures += sum(1 for l in page.extract_text_lines() if re.match(r"^(Figure|Fig\.)\s*\d+\s*[:.]", l["text"].strip()))
            sizes = [c["size"] for c in page.chars if c["text"].strip()]
            if not sizes:
                continue
            body = Counter(round(s, 1) for s in sizes).most_common(1)[0][0]
            for line in page.extract_text_lines():
                text = line["text"].strip()
                if not (3 <= len(text) <= 100) or text.endswith((".", ",", ";")) and not re.match(r"^\d+(\.\d+)*\s", text):
                    continue
                chars = line["chars"]
                size = max(c["size"] for c in chars)
                bold = sum("bold" in c["fontname"].lower() for c in chars) > 0.6 * len(chars)
                numbered = re.match(r"^([A-Z]|\d+)(\.\d+)*\.?\s*[A-Z][a-z]", text)
                if re.match(r"^(table|figure|fig\.|arxiv)", text, re.I) or len(text.split()) < 2 and len(text) < 6:
                    continue  # captions and the arXiv stamp are not headings
                if size >= body * 1.12 or (bold and numbered) or (numbered and len(text) < 70 and size >= body):
                    headings.append(norm(text))
    return words, "".join(full), tables, figures


def score(md, ref_words, pdf_text):
    out_words = grams(re.sub(r"\(s3://[^)]*\)|\(images/[^)]*\)", "", md))
    hit = sum(min(n, out_words[w]) for w, n in ref_words.items())
    heads = [norm(re.sub(r"^#+\s*", "", l)) for l in md.splitlines() if re.match(r"^#{1,6}\s", l)]
    real = sum(1 for x in heads if x and x in pdf_text)
    tables = len(re.findall(r"(?m)^\|[ :\-|]+\|\s*$", md))
    return {
        "text recall": 100 * hit / max(sum(ref_words.values()), 1),
        "text precision": 100 * hit / max(sum(out_words.values()), 1),
        "headings": len(heads),
        "headings that are real": f"{real}/{len(heads)}",
        "tables": tables,
        "formulas": len(re.findall(r"\$\$.+?\$\$|(?<!\$)\$[^$\n]{2,}\$(?!\$)", md, flags=re.S)),
        "images": len(re.findall(r"!\[[^\]]*\]\(", md)),
    }


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    ref_words, pdf_text, ref_tables, ref_figs = reference(argv[1])
    results = {}
    for arg in argv[2:]:
        name, _, path = arg.partition("=")
        results[name] = score(Path(path).read_text(encoding="utf-8", errors="ignore"), ref_words, pdf_text)
    rows = list(next(iter(results.values())).keys())
    lines = [f"PDF reference: {sum(ref_words.values()):,} text runs, "
             f"{ref_tables} tables, {ref_figs} captioned figures", "",
             "| Element | " + " | ".join(results) + " |", "|---|" + "---|" * len(results)]
    for r in rows:
        cells = [f"{v[r]:.1f}%" if isinstance(v[r], float) else str(v[r]) for v in results.values()]
        lines.append(f"| {r} | " + " | ".join(cells) + " |")
    report = "\n".join(lines)
    print(report)
    Path(__file__).with_name("compare_tools_report.md").write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
