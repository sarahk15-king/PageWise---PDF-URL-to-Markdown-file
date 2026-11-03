"""Source and output side by side: the PDF page on the left with elements marked in place, the extracted
elements of that page on the right.

  amber  an element that scored below the confidence you choose (default: below the engine's own limits,
         0.8 for tables and 0.6 for everything else)
  blue   an element the AI repaired (Standard quality)

Used by app.py. Needs pdfplumber to draw the page; without it the app shows a short note instead.
"""
import base64
import io
import json
import re

import streamlit as st

TABLE_LIMIT, TEXT_LIMIT = 0.8, 0.6
AMBER, BLUE = (255, 170, 0), (40, 120, 255)


def _norm(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def is_weak(element, limit=None):
    if limit is not None:
        return element.get("confidence", 1) < limit
    return element.get("confidence", 1) < (TABLE_LIMIT if element.get("type") == "table" else TEXT_LIMIT)


def locate(words, text, box=None):
    """Rectangles (x0, top, x1, bottom in PDF points) for an element, one per line of text, or [] when not found.

    A table has its own box. Other elements are matched against the page words: the words are joined and
    compared with the start of the element's text, so a PDF whose words are run together still matches."""
    if box:
        return [tuple(box)]
    target = _norm(text)
    if len(target) < 4:
        return []
    normed = [_norm(w["text"]) for w in words]
    best = []
    for start in range(len(words)):
        if not normed[start] or not target.startswith(normed[start]):
            continue
        joined, used = "", []
        for k in range(start, len(words)):
            if not normed[k]:
                continue
            if not target.startswith(joined + normed[k]):
                break
            joined += normed[k]
            used.append(words[k])
            if len(joined) >= len(target):
                break
        if len(joined) > len(best and "".join(_norm(w["text"]) for w in best)):
            best = used
        if len(joined) >= min(len(target), 60):
            break
    if sum(len(_norm(w["text"])) for w in best) < min(len(target), 12):
        return []
    lines = {}
    for w in best:
        lines.setdefault(round(w["top"] / 3), []).append(w)
    return [(min(w["x0"] for w in ws), min(w["top"] for w in ws), max(w["x1"] for w in ws), max(w["bottom"] for w in ws))
            for ws in lines.values()]


def draw_page(pdf_bytes, page_number, elements, limit=None, dpi=100):
    """The page as a picture with weak and AI-repaired elements marked. Returns (image, marked, weak count)."""
    import pdfplumber
    from PIL import Image, ImageDraw

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[page_number - 1]
        image = page.to_image(resolution=dpi).original.convert("RGBA")
        words = page.extract_words()
    scale = dpi / 72
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    marked, weak = 0, 0
    for e in elements:
        repaired = "ai_repaired" in e.get("flags", [])
        low = is_weak(e, limit)
        weak += low
        if not (low or repaired) or e.get("type") == "image":
            continue
        colour = BLUE if repaired else AMBER
        for x0, top, x1, bottom in locate(words, e.get("content", ""), (e.get("data") or {}).get("box")):
            draw.rectangle([x0 * scale - 2, top * scale - 2, x1 * scale + 2, bottom * scale + 2],
                           fill=colour + (60,), outline=colour + (255,), width=2)
            marked += 1
    return Image.alpha_composite(image, overlay).convert("RGB"), marked, weak


def render(pdf_bytes, json_text, key="source_view"):
    try:
        data = json.loads(json_text)
        elements = data["elements"]
    except (ValueError, KeyError, TypeError):
        st.caption("The extracted elements are not available for this document.")
        return
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        st.caption("Install pdfplumber in the app to see the source page here.")
        return
    pages = sorted({e["page"] for e in elements}) or [1]
    controls = st.columns([1, 2])
    page_number = controls[0].selectbox("Page", pages, key=f"{key}_page")
    limit = controls[1].slider(
        "Mark elements that scored below", 0.5, 1.0, 0.8, 0.01, key=f"{key}_limit",
        help="Amber marks an element below this confidence. Blue marks an element the AI repaired.")
    on_page = [e for e in elements if e["page"] == page_number]
    left, right = st.columns(2)
    with left:
        st.markdown("**Source page**")
        try:
            image, marked, weak = draw_page(pdf_bytes, page_number, on_page, limit)
            # embedded in the page itself: Streamlit's temporary image files can vanish between reruns
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85)
            encoded = base64.b64encode(buffer.getvalue()).decode()
            st.markdown(f'<img src="data:image/jpeg;base64,{encoded}" style="width:100%;border:1px solid #8884">',
                        unsafe_allow_html=True)
            repaired = sum("ai_repaired" in e.get("flags", []) for e in on_page)
            st.caption(f"Amber: {weak} below {limit:.2f}. Blue: {repaired} repaired by AI." if (weak or repaired)
                       else f"No element on this page scores below {limit:.2f}.")
        except Exception as error:
            st.caption(f"The page could not be drawn: {error}")
    with right:
        st.markdown("**Extracted output**")
        for e in on_page:
            repaired = "ai_repaired" in e.get("flags", [])
            badge = "🟦" if repaired else ("🟧" if is_weak(e, limit) else "⬜")
            st.caption(f"{badge} {e['type']} · confidence {e.get('confidence', 0):.2f} · {e.get('source', '')}"
                       + (" · AI repaired" if repaired else ""))
            if e["type"] == "heading":
                st.markdown("#### " + e["content"])
            elif e["type"] == "image":
                st.caption("[image] " + e.get("content", ""))
            elif e["type"] == "table":
                st.markdown(e["content"])
            else:
                st.write(e["content"])
