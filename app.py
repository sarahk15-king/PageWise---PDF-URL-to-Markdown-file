import os

import pandas as pd
import requests
import streamlit as st

# The backend. It defaults to the deployed Render service. To run everything on your own computer,
# set the API_URL environment variable to http://localhost:8000 before starting the app.
API_URL = os.getenv("API_URL", "https://ingestion-pipeline-api.onrender.com")
TOOL_NAMES = {"markitdown": "MarkItDown", "pypdf": "pypdf", "pdfplumber": "pdfplumber", "azure": "Azure Document Intelligence", "docling": "Docling", "textract": "Amazon Textract", "engine": "Pagewise engine"}
PDF_TOOLS = ["engine", "markitdown", "pypdf", "pdfplumber", "azure", "docling", "textract"]

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
    .summary-card {border-left: 5px solid #3b6ea5; background: rgba(59,110,165,.10);
                   padding: 1rem 1.2rem; border-radius: 10px; margin: .6rem 0 1.2rem 0;}
    .summary-card h4 {margin: 0 0 .4rem 0;}
    div[data-testid="stMetric"] {background: rgba(128,128,128,.08); padding: .8rem 1rem; border-radius: 12px;}
    .stButton > button[kind="primary"], .stButton > button {border-radius: 10px; font-weight: 600;}
    </style>
    <div class="hero">
      <h1>📄 Pagewise</h1>
      <p>Turn any PDF or web page into clean Markdown, with its tables and images saved and a short summary on top.</p>
      <div class="steps"><span class="step">1 · Upload</span><span class="step">2 · Extract</span><span class="step">3 · Read the summary</span><span class="step">4 · Download</span></div>
    </div>
    """,
    unsafe_allow_html=True,
)


def error_text(response, error):
    detail = ""
    try:
        detail = response.json().get("detail", "")
    except Exception:
        pass
    return detail or str(error)


def add_summary(result):
    """Asks the API for a brief summary of the extracted text. A failure never hides the extraction."""
    try:
        response = requests.post(
            f"{API_URL.rstrip('/')}/summarize",
            data={"markdown": result.get("markdown_text", result.get("markdown_preview", ""))},
            timeout=120,
        )
        response.raise_for_status()
        result["summary"] = response.json()
    except Exception as e:
        try:
            result["summary_error"] = response.json().get("detail", str(e))
        except Exception:
            result["summary_error"] = str(e)
    return result


def show_result(result):
    uploaded = result.get("uploaded", {})

    st.success("Done. Your document is ready.")

    c1, c2, c3 = st.columns(3)
    c1.metric("Characters extracted", f"{result['markdown_length']:,}")
    c2.metric("Tables found", uploaded.get("table_count", 0))
    c3.metric("Images saved", uploaded.get("image_count", 0))

    summary = result.get("summary")
    if summary:
        st.markdown(
            f'<div class="summary-card"><h4>Summary</h4>{summary["summary"]}</div>',
            unsafe_allow_html=True,
        )
        note = f"Written by {summary['model']} in {summary['seconds']}s, cost ${summary['cost_usd']:.4f}."
        if summary.get("truncated"):
            note += " The document was long, so only the first part was summarized."
        st.caption(note)
    elif result.get("summary_error"):
        st.info(f"The summary is not available right now: {result['summary_error']}")

    for warning in result.get("warnings", []):
        st.warning(warning)

    markdown_text = result.get("markdown_text", result["markdown_preview"])
    file_name = result["s3_key"].rsplit("/", 1)[-1]

    tab_md, tab_tables, tab_files = st.tabs(["Markdown", f"Tables ({len(result.get('tables', []))})", "Files in S3"])

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


tab1, tab2 = st.tabs(["📎 Upload a PDF", "🔗 Use a web link"])

with tab1:
    uploaded_file = st.file_uploader("Choose a PDF", type=["pdf"])
    left, right = st.columns([2, 1])
    with left:
        tool = st.selectbox(
            "Conversion tool", PDF_TOOLS, format_func=lambda t: TOOL_NAMES[t],
            help="Pagewise engine is our own tool. The others are there to compare.",
        )
    with right:
        want_summary = st.checkbox("Add a brief summary", value=True, key="pdf_summary")

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
            files = {"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")}
            data = {"tool": tool}
            try:
                response = requests.post(f"{API_URL.rstrip('/')}/process-pdf", files=files, data=data, timeout=600)
                response.raise_for_status()
                result = response.json()
                if want_summary:
                    add_summary(result)
                st.session_state["pdf_result"] = result
            except Exception as e:
                st.session_state.pop("pdf_result", None)
                st.error(f"Error: {error_text(response if 'response' in locals() else None, e)}")

    if "pdf_result" in st.session_state:
        show_result(st.session_state["pdf_result"])

with tab2:
    url = st.text_input("Enter a web page URL", placeholder="https://en.wikipedia.org/wiki/...")
    left, right = st.columns([2, 1])
    with left:
        url_tool = st.selectbox(
            "Conversion tool",
            ["beautifulsoup", "azure", "pypdf", "pdfplumber"],
            format_func=lambda t: {"beautifulsoup": "BeautifulSoup (web pages)", **TOOL_NAMES}[t],
            key="url_tool",
        )
    with right:
        want_url_summary = st.checkbox("Add a brief summary", value=True, key="url_summary")

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
            try:
                response = requests.post(
                    f"{API_URL.rstrip('/')}/process-url", data={"url": url, "tool": url_tool}, timeout=300
                )
                response.raise_for_status()
                result = response.json()
                if want_url_summary:
                    add_summary(result)
                st.session_state["url_result"] = result
            except Exception as e:
                st.session_state.pop("url_result", None)
                st.error(f"Error: {error_text(response if 'response' in locals() else None, e)}")

    if "url_result" in st.session_state:
        show_result(st.session_state["url_result"])

st.divider()
st.caption("Pagewise · PDFs and web pages in, clean Markdown out.")
