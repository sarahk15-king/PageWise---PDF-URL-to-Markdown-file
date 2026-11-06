"""The engine: one entry point for a PDF or a URL.

PDF steps
  1. Triage each page: does it have a text layer? (pdf.extract_pdf_elements)
  2. Text layer: the column reader gives headings, paragraphs, formulas, headers and footers in
     reading order; pdfplumber gives tables; the figure finder gives images.
  3. No text layer (a scan): Azure OCR when keys are set and working, otherwise RapidOCR (free, local),
     otherwise the page is flagged, not guessed.
  4. Key-value pairs are derived from the text, and every element gets a confidence.
URL steps
  A link to a PDF goes through the PDF steps; a web page is read from its HTML structure.
"""
import os
import re
import tempfile
import time
from pathlib import Path

from . import mdparse
from .pdf import extract_pdf_elements
from .schema import Document


def default_image_store(out_dir):
    """Saves each image as a PNG under out_dir/images and returns the relative link."""
    out = Path(out_dir) / "images"
    out.mkdir(parents=True, exist_ok=True)

    def store(name, png):
        (out / f"{name}.png").write_bytes(png)
        return f"images/{name}.png"

    return store


def azure_page_elements(pdf_path, page_no):
    """OCR one scanned page with Azure Document Intelligence when keys are set. Returns elements or None."""
    if not (os.getenv("AZURE_DOCINTEL_ENDPOINT") and os.getenv("AZURE_DOCINTEL_KEY")):
        return None
    try:
        from pypdf import PdfReader, PdfWriter
        from .pdf import _main
        writer = PdfWriter()
        writer.add_page(PdfReader(str(pdf_path)).pages[page_no - 1])
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            writer.write(tmp)
        try:
            markdown = _main().convert_with_azure(tmp.name)
        finally:
            os.unlink(tmp.name)
        return mdparse.blocks_to_elements(markdown, page_no, "azure", first_is_header=False)
    except Exception:
        return None


_rapid = None   # the OCR model loads once, on the first scanned page


def _rapid_engine():
    global _rapid
    if _rapid is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid = RapidOCR()
    return _rapid



def _unglue(text):
    """Some RapidOCR models drop the spaces between words ("Withtherapidexpansion...") and between a word and a
    number or punctuation mark. This puts them back. Runs of 5 or more letters are split into words with the
    wordninja library; a real word, a mixed-case name or acronym (VarP, LLaMA) and a single capitalised name
    (Zhendong) are left alone. Tokens that hold an @ or a / (emails, links) are not touched.
    Without wordninja installed, only the punctuation and number fixes run."""
    try:
        import wordninja
    except ImportError:
        wordninja = None
    known = {}
    if wordninja is not None:
        try:   # the word list sits under a different name in different wordninja versions
            model = getattr(wordninja, "DEFAULT_LANGUAGE_MODEL", None) or getattr(wordninja, "_LANGUAGE_MODEL", None)
            known = model._wordcost
        except Exception:
            known = {}
    small = {"of", "the", "and", "for", "in", "to", "on", "with", "by", "is", "are", "as", "at", "an", "or",
             "from", "that", "this", "a", "we", "our", "it", "its", "be", "can", "not", "all"}
    suffixes = {"iz", "ation", "ization", "ing", "tion", "ness", "ment", "ity"}

    def split_part(part):
        if len(part) < 5 or part.lower() in known or re.search(r"[A-Z]", part[1:]):
            return [part]   # short, a real word, or mixed case like VarP / CosP / LLaMA
        pieces = wordninja.split(part)
        fixed = []
        for x in pieces:   # put back what wordninja cuts off: plurals ("activation"+"s"), suffixes, lone letters
            if fixed and (x in ("s", "S") or x.lower() in suffixes):
                fixed[-1] += x
            elif fixed and len(fixed[-1]) == 1 and fixed[-1].islower() and fixed[-1] != "a":
                fixed[-1] += x
            else:
                fixed.append(x)
        pieces = fixed
        if len(part) < 8 and not any(x.lower() in small for x in pieces):
            return [part]   # a short unknown run is split only when it holds a small word ("suchas")
        if len(pieces) < 2 or any(len(x) < 2 and x not in ("a", "A", "I") and not x.isupper() for x in pieces):
            return [part]
        if len(pieces[0]) == 1 and pieces[0].isupper() and known and all(x.lower() in known for x in pieces[1:]):
            return pieces   # "Msparsitysettings" (from N:M sparsity settings) -> M sparsity settings
        if (re.fullmatch(r"[A-Z][a-z]+", part) and len(part) <= 20 and not any(x.lower() in small for x in pieces)
                and not (known and len(pieces[0]) >= 4 and all(x.lower() in known for x in pieces)
                         and known.get(pieces[0].lower(), 99) <= 11.0)):
            return [part]   # a single capitalised name such as Zhendong stays whole ("Underreview" is split)
        return pieces

    def split_run(match):
        run = match.group(0)
        peel = re.fullmatch(r"([A-Za-z]*[a-z][A-Z]{1,3})([a-z]{3,})", run) or re.fullmatch(r"([A-Z]{2,4})([a-z]{4,})", run)
        if peel and (peel.group(2) in small or (len(peel.group(2)) >= 4 and peel.group(2) in known)):
            parts = [peel.group(1), peel.group(2)]   # "CosPand" -> CosP and, "OPTmodels" -> OPT models
        elif len(run) >= 6:
            parts = re.split(r"(?<=[a-z]{2})(?=[A-Z][a-z]{3,})|(?<=[a-z]{3})(?=[A-Z]{2,}|[A-Z][:\d])"
                             r"|(?<=[A-Z]{2}s)(?=[a-z]{4,})", run)
        else:
            parts = [run]
        out = []
        for part in parts:
            out.extend(split_part(part) if wordninja is not None else [part])
        return " ".join(out)

    def fix_token(tok):
        if "@" in tok or "/" in tok or tok.isspace() or not tok:
            return tok
        tok = re.sub(r"(?<=[a-z]{3})(?=[A-Z][:\d])", " ", tok)               # "leastfourA100", "sparsityandN:M"
        tok = re.sub(r"[A-Za-z]{5,}", split_run, tok)
        tok = re.sub(r"(?<=[A-Za-z\)\]]),(?=[A-Za-z])", ", ", tok)             # "Mi,Zhenglun"
        tok = re.sub(r"(?<=[a-z\]\)])\.(?=[A-Z][a-z]+)", ". ", tok)            # "process.In", "[8,3,9].For"
        tok = re.sub(r"(?<=[a-z]{2})\((?=[A-Z])", " (", tok)                    # "models(LLMs)"
        tok = re.sub(r"\)(?=[a-z]{3,})", ") ", tok)                            # "(LLMs)have"
        tok = re.sub(r"(?<=[A-Za-z]):(?=[a-z])", ": ", tok)                    # "metric:a novel"
        tok = re.sub(r"\b(to|of|in|than|and|by|at|is|on|for)(?=\d)", r"\1 ", tok)   # "to18%"
        tok = re.sub(r"(?<=[a-z]{4})(?=\d)", " ", tok)                         # "exceeding100", "Technology2"
        tok = re.sub(r"(?<=\d)(?=[a-z]{3,})", " ", tok)                        # "2and", "4semi"
        tok = re.sub(r"(\d[A-Z])([a-z]{4,})",                                  # "65Bmodel": the word after B is real
                     lambda m: m.group(1) + " " + m.group(2) if m.group(2) in known or m.group(2) in small else m.group(0), tok)
        tok = re.sub(r"(?<=\d)(?=[A-Z][a-z]{3,})", " ", tok)                   # "2Harvard"
        tok = re.sub(r"%(?=[a-z]{2,})", "% ", tok)                             # "18%decrease"
        return tok

    return "".join(fix_token(t) for t in re.split(r"(\s+)", text))


def _row_order(lines, body_h):
    """Lines in reading order: top to bottom by their middle, and left to right only inside one row.
    Sorting on the top edge alone can swap two neighbouring lines when their boxes are a little uneven."""
    rows = []
    for l in sorted(lines, key=lambda l: (l["y0"] + l["y1"]) / 2):
        centre = (l["y0"] + l["y1"]) / 2
        if rows and abs(centre - rows[-1]["centre"]) <= body_h * 0.5:
            rows[-1]["items"].append(l)
            rows[-1]["centre"] = sum((i["y0"] + i["y1"]) / 2 for i in rows[-1]["items"]) / len(rows[-1]["items"])
        else:
            rows.append({"centre": centre, "items": [l]})
    return [l for row in rows for l in sorted(row["items"], key=lambda l: l["x0"])]


def rapidocr_markdown(pdf_path, page_no, scale=2.0):
    """Read one scanned page with RapidOCR (free, runs inside the app, no cloud keys).

    Returns (markdown, average_score) or None when the library is missing or the page has no text.
    Reading order: one column, or left column then right column when the page clearly has two.
    A line much taller than the rest, short and not ending like a sentence, becomes a heading.
    RapidOCR reads text only, so a table comes out as lines of text, not a table.
    """
    try:
        import numpy as np
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            page = pdf[page_no - 1]
            image = np.array(page.render(scale=scale).to_pil().convert("RGB"))[:, :, ::-1]
            page.close()
        finally:
            pdf.close()   # release the file, or Windows cannot delete the temporary upload afterwards
        found, _ = _rapid_engine()(image)
    except Exception:
        return None
    if not found:
        return None

    lines = []
    for box, text, score in found:
        text = _unglue(str(text).strip())
        text = re.sub(r"^(\d{1,2}(?:\.\d{1,2})*)\.?(?=[A-Z][a-z])", r"\1 ", text)   # "1Introduction" -> "1 Introduction"
        if not text:
            continue
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        if (max(ys) - min(ys)) > 2 * (max(xs) - min(xs)) and len(text) > 3:
            continue  # sideways text in the margin (such as the arXiv stamp) is not part of the page text
        lines.append({"text": text, "score": float(score), "x0": min(xs), "x1": max(xs),
                      "y0": min(ys), "y1": max(ys)})
    if not lines:
        return None

    width = max(l["x1"] for l in lines)
    heights = sorted(l["y1"] - l["y0"] for l in lines)
    body_h = heights[len(heights) // 2] or 1.0

    left = [l for l in lines if l["x1"] < width * 0.52]
    right = [l for l in lines if l["x0"] > width * 0.48]
    across = [l for l in lines if l not in left and l not in right]
    two_columns = len(left) >= 6 and len(right) >= 6 and len(across) <= 0.15 * len(lines)
    if two_columns:
        groups = [_row_order(left, body_h), _row_order(right, body_h)]
        top = sorted(across, key=lambda l: l["y0"])
        if top and top[0]["y0"] < min(l["y0"] for l in left):
            groups[0] = top + groups[0]
    else:
        groups = [_row_order(lines, body_h)]

    blocks = []
    for group in groups:
        para, prev = [], None
        for l in group:
            tall = (l["y1"] - l["y0"]) >= body_h * 1.3
            short = len(l["text"].split()) <= 12 and not l["text"].rstrip().endswith((".", ",", ";", ":"))
            numbered = bool(re.match(r"^\d{1,2}(?:\.\d{1,2})*\s[A-Z][a-z]", l["text"])) and short and len(l["text"].split()) <= 8 \
                and not re.search(r"University|Institute|Laborator|School|Department|College|Corporation|Research", l["text"])   # an affiliation, not a section
            if (tall and short) or numbered:
                if para:
                    blocks.append(" ".join(para))
                    para = []
                blocks.append("## " + l["text"])
                prev = None
                continue
            new_paragraph = prev is not None and (l["y0"] - prev["y1"]) > body_h * 0.9
            if new_paragraph and para:
                blocks.append(" ".join(para))
                para = []
            para.append(l["text"])
            prev = l
        if para:
            blocks.append(" ".join(para))
    markdown = "\n\n".join(blocks)
    average = sum(l["score"] for l in lines) / len(lines)
    return markdown, average


def rapidocr_page_elements(pdf_path, page_no):
    """OCR one scanned page with RapidOCR. Returns elements or None (library missing, or nothing read)."""
    result = rapidocr_markdown(pdf_path, page_no)
    if not result:
        return None
    markdown, average = result
    try:
        elements = mdparse.blocks_to_elements(markdown, page_no, "rapidocr", first_is_header=False)
    except Exception:
        return None
    for e in elements:
        # OCR is less certain than a text layer: cap the confidence at what the engine measured on the page
        e.confidence = round(min(float(e.confidence or 1.0), average), 3)
        e.data["ocr_engine"] = "rapidocr"
    return elements


def extract_pdf(path, image_store=None, out_dir=None, max_pages=60, quality="economy"):
    """PDF in, Document out (Document.to_markdown() and Document.to_json()).

    quality="economy": Python functions only (plus OCR for scanned pages: Azure, else RapidOCR).
    quality="standard": the same, then Amazon Nova repairs the weak tables and weak text (see engine/ai.py).
    """
    started = time.time()
    doc = Document(source=str(path), level="engine")
    if image_store is None:
        image_store = default_image_store(out_dir or tempfile.mkdtemp(prefix="engine_"))

    elements, pages, scanned = extract_pdf_elements(path, image_store, doc.warnings, max_pages=max_pages)
    doc.pages = pages

    ocr_pages = 0
    if scanned:
        rebuilt = []
        for e in elements:
            if not e.data.get("scanned"):
                rebuilt.append(e)
                continue
            new = azure_page_elements(path, e.page)
            if not new:
                new = rapidocr_page_elements(path, e.page)   # free, local; used when Azure is not set up or fails
            if new:
                ocr_pages += 1
                for n in new:
                    n.flags.append("ocr")
                rebuilt += new
            else:
                doc.warnings.append(f"Page {e.page} has no text layer and no OCR is set up, so it was not read.")
                rebuilt.append(e)
        elements = rebuilt

    doc.elements = mdparse.add_key_values([e for e in elements if e.type != "key_value"])
    mdparse.finish_flags(doc.elements)
    doc.number()
    if quality == "standard":
        from . import ai
        ai.repair_document(doc, path)
    doc.timings["total_s"] = round(time.time() - started, 2)
    doc.cost["ocr_pages"] = ocr_pages
    return doc


def extract_url(url, image_store=None, out_dir=None):
    """URL in, Document out. A link to a PDF is downloaded and handled as a PDF."""
    from . import web
    started = time.time()
    body, content_type, final_url = web.fetch(url)
    if web.is_pdf(body, content_type, final_url):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(body)
        try:
            doc = extract_pdf(tmp.name, image_store, out_dir)
        finally:
            os.unlink(tmp.name)
        doc.source = url
        return doc
    doc = Document(source=url, level="engine", pages=1)
    doc.elements = web.html_elements(body.decode("utf-8", "replace"), final_url)
    mdparse.finish_flags(doc.elements)
    doc.number()
    doc.timings["total_s"] = round(time.time() - started, 2)
    return doc
