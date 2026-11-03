import csv
import io
import json
import os
import re
import zipfile

import pandas as pd
import requests
import streamlit as st

# The backend. It defaults to the deployed Render service. To run everything on your own computer,
# set the API_URL environment variable to http://localhost:8000 before starting the app.
API_URL = os.getenv("API_URL", "https://ingestion-pipeline-api.onrender.com")
TOOL_NAMES = {"markitdown": "MarkItDown", "pypdf": "pypdf", "pdfplumber": "pdfplumber", "azure": "Azure Document Intelligence", "docling": "Docling", "textract": "Amazon Textract", "engine": "Pagewise engine"}
PDF_TOOLS = ["engine", "markitdown", "pypdf", "pdfplumber", "azure", "docling", "textract"]
# The live server is small. This is the size that has been tested there; change the text if you test larger files.
SIZE_NOTE = "On the live app, PDFs up to about 1 MB (around 20 pages) work best. Larger files may not finish."
ON_LIVE_SERVER = "onrender.com" in API_URL
LEVEL_NAMES = {"economy": "Economy", "standard": "Standard"}
# the choices in the Compare tab: the engine at each quality level, then the other tools
COMPARE_CHOICES = ["engine:economy"] + PDF_TOOLS[1:]
COMPARE_NAMES = {**TOOL_NAMES, "engine:economy": "Pagewise engine (economy)", "engine:standard": "Pagewise engine (standard)"}

st.set_page_config(page_title="Pagewise", page_icon="📄", layout="wide")

st.markdown(
    """
    <style>
    .block-container {max-width: 1100px; padding-top: 2rem;}
    .hero {background: linear-gradient(135deg, #1f3a5f 0%, #3b6ea5 55%, #5aa5c4 100%);
           color: white; padding: 2.2rem 2.4rem; border-radius: 18px; margin-bottom: 1.6rem;}
    .hero h1 {margin: 0; font-size: 2.6rem; letter-spacing: -0.5px; color: white;}
    .hero p {margin: .5rem 0 0 0; font-size: 1.1rem; opacity: .92; color: white;}
    .steps {display: flex; gap: .8rem; margin-top: 1.1rem; flex-wrap: wrap;}
    .step {background: rgba(255,255,255,.16); border-radius: 999px; padding: .3rem .9rem; font-size: .88rem;}
    div[data-testid="stMetric"] {background: rgba(128,128,128,.08); padding: .8rem 1rem; border-radius: 12px;}
    .stButton > button, .stDownloadButton > button {border-radius: 10px; font-weight: 600;}
    </style>
    <div class="hero">
      <h1>📄 Pagewise</h1>
      <p>Turn any PDF or web page into clean Markdown, with its tables and images saved and a side-by-side comparison of tools.</p>
      <div class="steps"><span class="step">1 · Upload</span><span class="step">2 · Extract</span><span class="step">3 · Compare tools</span><span class="step">4 · Download everything</span></div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ---- helpers ---------------------------------------------------------------------------------------

def error_text(response, error):
    detail = ""
    try:
        detail = response.json().get("detail", "")
    except Exception:
        pass
    return detail or str(error)


def split_row(line):
    """Cells of one Markdown table row. A pipe written as \\| stays inside its cell."""
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [c.strip().replace("\\|", "|") for c in cells]


def tables_from_markdown(markdown_text):
    """Every pipe table in the Markdown as a list of rows (the separator row is dropped)."""
    tables, block = [], []
    for line in markdown_text.splitlines() + [""]:
        if line.strip().startswith("|"):
            block.append(line)
            continue
        if len(block) >= 2:
            rows = [split_row(l) for l in block]
            rows = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c) for c in r if c)]
            if rows:
                tables.append(rows)
        block = []
    return tables


def doc_stats(markdown_text):
    lines = markdown_text.splitlines()
    return {
        "Tables": sum(1 for l in lines if re.fullmatch(r"\s*\|[\s:|\-]+\|\s*", l) and "---" in l),
        "Images": len(re.findall(r"!\[[^\]]*\]\(", markdown_text)),
        "Formulas": markdown_text.count("$$") // 2,
    }


def fetch_images(keys):
    """The extracted images as {file name: bytes}, read through the API (the bucket is private)."""
    if not keys:
        return {}
    try:
        response = requests.post(f"{API_URL.rstrip('/')}/image-files", data={"keys": json.dumps(keys)}, timeout=120)
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}
    except Exception:
        return {}


def zip_everything(result, markdown_text, file_name, images=None):
    """One download: the Markdown, the JSON, every table as its own CSV, and the extracted images."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(file_name, markdown_text)
        if result.get("json_text"):
            archive.writestr(file_name.rsplit(".", 1)[0] + ".json", result["json_text"])
        for number, rows in enumerate(tables_from_markdown(markdown_text), start=1):
            out = io.StringIO()
            csv.writer(out).writerows(rows)
            archive.writestr(f"tables/table_{number:02d}.csv", out.getvalue())
        for name, data in (images or {}).items():
            archive.writestr(f"images/{name}", data)
    return buffer.getvalue()


def show_result(result):
    uploaded = result.get("uploaded", {})
    markdown_text = result.get("markdown_text", result["markdown_preview"])
    file_name = result["s3_key"].rsplit("/", 1)[-1]

    st.success("Done. Your document is ready.")

    engine = result.get("engine")
    cards = st.columns(5 if engine else 4)
    cards[0].metric("Characters extracted", f"{result['markdown_length']:,}")
    cards[1].metric("Tables found", uploaded.get("table_count", 0))
    cards[2].metric("Images saved", uploaded.get("image_count", 0))
    cards[3].metric("Time taken", f"{result['seconds']}s" if result.get("seconds") is not None else "-")
    if engine:
        cards[4].metric(
            "Handled without AI", f"{engine['share_without_ai']:.1f}%",
            help=f"{engine['elements'] - engine['weak_before_ai']} of {engine['elements']} elements "
                 "(headings, paragraphs, tables, formulas) were read with enough confidence that they did not "
                 "need AI repair. This is the engine's own confidence score, not a comparison with the PDF.",
        )

    for warning in result.get("warnings", []):
        st.warning(warning)

    image_keys = uploaded.get("image_keys", [])
    cache_key = f"images_{result['s3_key']}"
    if image_keys and cache_key not in st.session_state:
        with st.spinner("Getting the images ready for download..."):
            st.session_state[cache_key] = fetch_images(image_keys)
    images = st.session_state.get(cache_key, {})
    if image_keys and not images:
        st.caption("The images could not be added to the zip right now. They are still saved in S3.")

    st.download_button(
        "⬇ Download everything (.zip)",
        data=zip_everything(result, markdown_text, file_name, images),
        file_name=file_name.rsplit(".", 1)[0] + ".zip",
        mime="application/zip",
        key=f"zip_{result['s3_key']}",
        help="The Markdown, the JSON (Pagewise engine), every table as a CSV file, and the extracted images.",
    )

    names = ["Markdown"] + (["JSON"] if result.get("json_text") else []) + [f"Tables ({len(result.get('tables', []))})", "Files in S3"]
    tabs = st.tabs(names)
    tab_md, tab_tables, tab_files = tabs[0], tabs[-2], tabs[-1]
    if result.get("json_text"):
        with tabs[1]:
            st.download_button(
                "⬇ Download JSON",
                data=result["json_text"],
                file_name=file_name.rsplit(".", 1)[0] + ".json",
                mime="application/json",
                key=f"json_{result['s3_key']}",
            )
            st.caption("Every element with its type, page, reading order, confidence and how it was produced.")
            st.text_area("JSON preview", result["json_text"][:800], height=180)

    with tab_md:
        st.download_button(
            "⬇ Download Markdown",
            data=markdown_text,
            file_name=file_name,
            mime="text/markdown",
            key=f"download_{result['s3_key']}",
        )
        st.text_area("Markdown preview", markdown_text[:800], height=180)
        if len(markdown_text) > 800:
            st.caption("Showing the first 800 characters. Download the file for everything.")

    with tab_tables:
        tables = result.get("tables", [])
        if not tables:
            st.caption("No tables were found in this document.")
        for table in tables:
            with st.expander(f"{table['name']}  ({table['row_count']} rows)"):
                preview = table["preview"]
                width = max(len(row) for row in preview)
                padded = [row + [""] * (width - len(row)) for row in preview]
                st.dataframe(pd.DataFrame(padded), use_container_width=True)
                if table["row_count"] > len(preview):
                    st.caption(f"Showing the first {len(preview)} of {table['row_count']} rows.")
                st.caption(f"Saved as its own CSV file: `{table['key']}`")

    with tab_files:
        st.write(f"**Markdown file:** `{result['s3_url']}`")
        st.json(uploaded)


def run_pdf(file, tool, quality=None):
    """Sends one PDF to the API with one tool. Returns the result, or raises with a readable message.

    A tool written as "engine:standard" means the Pagewise engine at that quality level."""
    if ":" in tool:
        tool, quality = tool.split(":", 1)
    response = None
    try:
        response = requests.post(
            f"{API_URL.rstrip('/')}/process-pdf",
            files={"file": (file.name, file.getvalue(), "application/pdf")},
            data={"tool": tool, "quality": quality or ""},
            timeout=600,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        raise RuntimeError(error_text(response, e))


# ---- tabs ------------------------------------------------------------------------------------------

tab1, tab_compare, tab2 = st.tabs(["📎 Upload a PDF", "⚖️ Compare tools", "🔗 Use a web link"])

with tab1:
    uploaded_file = st.file_uploader("Choose a PDF", type=["pdf"])
    if ON_LIVE_SERVER:
        st.caption(SIZE_NOTE)
    tool = st.selectbox(
        "Conversion tool", PDF_TOOLS, format_func=lambda t: TOOL_NAMES[t],
        help="Pagewise engine is our own tool. The others are there to compare.",
    )
    quality = "economy" if tool == "engine" else None

    if tool == "azure":
        st.info(
            "Azure Document Intelligence is an enterprise service. On the free tier it reads only "
            "the first 2 pages of a PDF and files up to 4 MB."
        )

    if tool == "docling":
        st.info(
            "Docling is slower than the other tools. The first run can take a few minutes while its "
            "service wakes up, and large PDFs take longer."
        )

    if st.button("Process PDF", type="primary", disabled=uploaded_file is None):
        with st.spinner(f"Processing with {TOOL_NAMES[tool]}..."):
            try:
                st.session_state["pdf_result"] = run_pdf(uploaded_file, tool, quality)
            except Exception as e:
                st.session_state.pop("pdf_result", None)
                st.error(f"Error: {e}")

    if "pdf_result" in st.session_state:
        show_result(st.session_state["pdf_result"])

with tab_compare:
    st.write("Run two tools on the same PDF and see what each one found.")
    compare_file = st.file_uploader("Choose a PDF", type=["pdf"], key="compare_file")
    if ON_LIVE_SERVER:
        st.caption(SIZE_NOTE)
    a, b = st.columns(2)
    with a:
        tool_a = st.selectbox("Tool A", COMPARE_CHOICES, index=0, format_func=lambda t: COMPARE_NAMES[t], key="tool_a")
    with b:
        tool_b = st.selectbox("Tool B", COMPARE_CHOICES, index=1, format_func=lambda t: COMPARE_NAMES[t], key="tool_b")
    if tool_a == tool_b:
        st.caption("Pick two different tools to compare.")

    if st.button("Compare", type="primary", disabled=compare_file is None or tool_a == tool_b):
        outcome = {}
        with st.spinner("Running both tools..."):
            for key, name in (("a", tool_a), ("b", tool_b)):
                try:
                    outcome[key] = {"tool": name, "result": run_pdf(compare_file, name)}
                except Exception as e:
                    outcome[key] = {"tool": name, "error": str(e)}
        st.session_state["compare"] = outcome

    if "compare" in st.session_state:
        outcome = st.session_state["compare"]
        rows = {}
        for key in ("a", "b"):
            item = outcome[key]
            label = COMPARE_NAMES[item["tool"]]
            if "error" in item:
                rows[label] = {"Result": f"Failed: {item['error']}"}
                continue
            res = item["result"]
            stats = doc_stats(res.get("markdown_text", ""))
            rows[label] = {"Time (s)": res.get("seconds"), "Characters": res["markdown_length"], **stats}
        st.dataframe(pd.DataFrame(rows).fillna("-"), use_container_width=True)

        left, right = st.columns(2)
        for column, key in ((left, "a"), (right, "b")):
            item = outcome[key]
            with column:
                st.markdown(f"**{COMPARE_NAMES[item['tool']]}**")
                if "error" in item:
                    st.error(item["error"])
                else:
                    text = item["result"].get("markdown_text", "")
                    st.text_area("Preview", text[:1500], height=320, key=f"cmp_prev_{key}")
                    st.download_button(
                        "⬇ Download Markdown", data=text,
                        file_name=item["result"]["s3_key"].rsplit("/", 1)[-1],
                        mime="text/markdown", key=f"cmp_dl_{key}",
                    )

with tab2:
    url = st.text_input("Enter a web page URL", placeholder="https://en.wikipedia.org/wiki/...")
    url_tool = st.selectbox(
        "Conversion tool",
        ["beautifulsoup", "azure", "pypdf", "pdfplumber"],
        format_func=lambda t: {"beautifulsoup": "BeautifulSoup (web pages)", **TOOL_NAMES}[t],
        key="url_tool",
    )

    if url_tool in ("pypdf", "pdfplumber"):
        st.info("pypdf and pdfplumber read PDF files. Use them with a link that points to a PDF.")
    elif url_tool == "azure":
        st.info(
            "Azure Document Intelligence works on web pages and PDF links. On the free tier it reads "
            "only the first 2 pages of a PDF."
        )
    else:
        st.caption("BeautifulSoup reads web pages. For a link to a PDF, choose Azure, pypdf or pdfplumber.")

    if st.button("Process link", type="primary", disabled=not url):
        with st.spinner(f"Processing with {'BeautifulSoup' if url_tool == 'beautifulsoup' else TOOL_NAMES[url_tool]}..."):
            response = None
            try:
                response = requests.post(
                    f"{API_URL.rstrip('/')}/process-url", data={"url": url, "tool": url_tool}, timeout=300
                )
                response.raise_for_status()
                st.session_state["url_result"] = response.json()
            except Exception as e:
                st.session_state.pop("url_result", None)
                st.error(f"Error: {error_text(response, e)}")

    if "url_result" in st.session_state:
        show_result(st.session_state["url_result"])

st.divider()
st.caption("Pagewise · PDFs and web pages in, clean Markdown out.")
