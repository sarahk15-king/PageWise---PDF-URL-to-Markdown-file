import csv
import io
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
    .stButton > button, .stDownloadButton > button {border-radius: 10px; font-weight: 600;}
    </style>
    <div class="hero">
      <h1>📄 Pagewise</h1>
      <p>Turn any PDF or web page into clean Markdown, with its tables and images saved and a check on how sure we are of every page.</p>
      <div class="steps"><span class="step">1 · Upload</span><span class="step">2 · Extract</span><span class="step">3 · Check the quality</span><span class="step">4 · Download everything</span></div>
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
        "Words": len(re.findall(r"\w+", markdown_text)),
        "Headings": sum(1 for l in lines if re.match(r"#{1,6}\s", l)),
        "Tables": sum(1 for l in lines if re.fullmatch(r"\s*\|[\s:|\-]+\|\s*", l) and "---" in l),
        "Formulas": markdown_text.count("$$") // 2,
        "Images": len(re.findall(r"!\[[^\]]*\]\(", markdown_text)),
    }


def quality_text(report):
    lines = [f"Average confidence: {report['average_confidence']}", f"Pages: {report['pages']}",
             f"Elements read: {report['elements']}", f"Weak elements: {report['weak_count']}", ""]
    for w in report["weak"]:
        lines.append(f"Page {w['page']}, {w['type']}, {w['confidence']}: {w['preview']}")
    return "\n".join(lines)


def zip_everything(result, markdown_text, file_name):
    """One download: the Markdown, every table as its own CSV, and the quality report when there is one."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(file_name, markdown_text)
        for number, rows in enumerate(tables_from_markdown(markdown_text), start=1):
            out = io.StringIO()
            csv.writer(out).writerows(rows)
            archive.writestr(f"tables/table_{number:02d}.csv", out.getvalue())
        if result.get("quality_report"):
            archive.writestr("quality_report.txt", quality_text(result["quality_report"]))
    return buffer.getvalue()


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


def show_quality(report):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Average confidence", f"{report['average_confidence']:.0%}")
    c2.metric("Pages", report["pages"])
    c3.metric("Elements read", report["elements"])
    c4.metric("Need a look", report["weak_count"])

    st.caption(
        "Pagewise scores every heading, paragraph, table and formula it reads. "
        "A low score does not mean the content is wrong, only that the engine is less sure."
    )
    if report.get("by_page"):
        pages = pd.DataFrame(report["by_page"]).rename(columns={"page": "Page", "average": "Average confidence"})
        st.markdown("**Confidence by page**")
        st.bar_chart(pages.set_index("Page")["Average confidence"], height=220)
    if report["weak"]:
        st.markdown("**Least sure about**")
        weak = pd.DataFrame(report["weak"]).rename(
            columns={"page": "Page", "type": "Type", "confidence": "Confidence", "preview": "What the engine read"}
        )
        st.dataframe(weak, use_container_width=True, hide_index=True)
        if report["weak_count"] > len(report["weak"]):
            st.caption(f"Showing the {len(report['weak'])} weakest of {report['weak_count']}.")
    else:
        st.success("Everything the engine read scored well.")
    if report.get("ai_repaired"):
        st.info(f"{report['ai_repaired']} weak elements were repaired with AI.")


def show_result(result):
    uploaded = result.get("uploaded", {})
    markdown_text = result.get("markdown_text", result["markdown_preview"])
    file_name = result["s3_key"].rsplit("/", 1)[-1]
    report = result.get("quality_report")

    st.success("Done. Your document is ready.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Characters extracted", f"{result['markdown_length']:,}")
    c2.metric("Tables found", uploaded.get("table_count", 0))
    c3.metric("Images saved", uploaded.get("image_count", 0))
    c4.metric("Time taken", f"{result['seconds']}s" if result.get("seconds") is not None else "-")

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

    st.download_button(
        "⬇ Download everything (.zip)",
        data=zip_everything(result, markdown_text, file_name),
        file_name=file_name.rsplit(".", 1)[0] + ".zip",
        mime="application/zip",
        key=f"zip_{result['s3_key']}",
        help="The Markdown, every table as a CSV file, and the quality report. Images are saved in S3.",
    )

    names = ["Markdown", f"Tables ({len(result.get('tables', []))})", "Files in S3"]
    if report:
        names.insert(1, "Extraction quality")
    tabs = st.tabs(names)
    tab_md, tab_tables, tab_files = tabs[0], tabs[-2], tabs[-1]

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

    if report:
        with tabs[1]:
            show_quality(report)

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


def run_pdf(file, tool):
    """Sends one PDF to the API with one tool. Returns the result, or raises with a readable message."""
    response = None
    try:
        response = requests.post(
            f"{API_URL.rstrip('/')}/process-pdf",
            files={"file": (file.name, file.getvalue(), "application/pdf")},
            data={"tool": tool},
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
    left, right = st.columns([2, 1])
    with left:
        tool = st.selectbox(
            "Conversion tool", PDF_TOOLS, format_func=lambda t: TOOL_NAMES[t],
            help="Pagewise engine is our own tool. The others are there to compare.",
        )
    with right:
        want_summary = st.checkbox("Add a brief summary", value=False, key="pdf_summary",
                                   help="Needs AI access on the server. Off by default.")

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
                result = run_pdf(uploaded_file, tool)
                if want_summary:
                    add_summary(result)
                st.session_state["pdf_result"] = result
            except Exception as e:
                st.session_state.pop("pdf_result", None)
                st.error(f"Error: {e}")

    if "pdf_result" in st.session_state:
        show_result(st.session_state["pdf_result"])

with tab_compare:
    st.write("Run two tools on the same PDF and see what each one found.")
    compare_file = st.file_uploader("Choose a PDF", type=["pdf"], key="compare_file")
    a, b = st.columns(2)
    with a:
        tool_a = st.selectbox("Tool A", PDF_TOOLS, index=0, format_func=lambda t: TOOL_NAMES[t], key="tool_a")
    with b:
        tool_b = st.selectbox("Tool B", PDF_TOOLS, index=1, format_func=lambda t: TOOL_NAMES[t], key="tool_b")
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
            label = TOOL_NAMES[item["tool"]]
            if "error" in item:
                rows[label] = {"Result": f"Failed: {item['error']}"}
                continue
            res = item["result"]
            stats = doc_stats(res.get("markdown_text", ""))
            rows[label] = {"Time (s)": res.get("seconds"), "Characters": res["markdown_length"], **stats}
            if res.get("quality_report"):
                rows[label]["Average confidence"] = res["quality_report"]["average_confidence"]
        st.dataframe(pd.DataFrame(rows).fillna("-"), use_container_width=True)

        left, right = st.columns(2)
        for column, key in ((left, "a"), (right, "b")):
            item = outcome[key]
            with column:
                st.markdown(f"**{TOOL_NAMES[item['tool']]}**")
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
    left, right = st.columns([2, 1])
    with left:
        url_tool = st.selectbox(
            "Conversion tool",
            ["beautifulsoup", "azure", "pypdf", "pdfplumber"],
            format_func=lambda t: {"beautifulsoup": "BeautifulSoup (web pages)", **TOOL_NAMES}[t],
            key="url_tool",
        )
    with right:
        want_url_summary = st.checkbox("Add a brief summary", value=False, key="url_summary",
                                       help="Needs AI access on the server. Off by default.")

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
                result = response.json()
                if want_url_summary:
                    add_summary(result)
                st.session_state["url_result"] = result
            except Exception as e:
                st.session_state.pop("url_result", None)
                st.error(f"Error: {error_text(response, e)}")

    if "url_result" in st.session_state:
        show_result(st.session_state["url_result"])

st.divider()
st.caption("Pagewise · PDFs and web pages in, clean Markdown out.")
