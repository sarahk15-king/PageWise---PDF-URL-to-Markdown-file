"""AI repair: Amazon Nova on Bedrock rewrites only the elements the confidence functions marked as weak.

Off by default. Turn it on with AI_REPAIR_ENABLED=true and ask for quality="standard".

What is sent to the model
  A weak table: a picture of the table and the table as the engine read it.
  Weak text: a picture of the page and the text as the engine read it.
  Weak text that is really a formula (math symbols, almost no words): the page picture and the garbled
  formula, and the model is asked for LaTeX only. An answer that passes a simple LaTeX check becomes a formula element.
  Nothing else. Strong elements, headings, images and scanned pages (Azure reads those) are never sent.

What happens to the answer
  The same confidence functions score the answer. If it scores higher it replaces the element and the
  original is kept in data["before"]. If it scores the same or lower the original stays and the attempt is
  logged as rejected, so the cases where the AI made it worse are recorded too. There is no AI quality check.

Settings (all optional, read when the repair runs)
  AI_REPAIR_ENABLED            true to allow any AI call (default false)
  BEDROCK_AWS_ACCESS_KEY_ID, BEDROCK_AWS_SECRET_ACCESS_KEY, BEDROCK_AWS_SESSION_TOKEN
                               separate from the S3 keys; when absent the normal AWS credentials are used
  BEDROCK_REGION               default us-east-1
  BEDROCK_MODEL_ID             default amazon.nova-lite-v1:0
  AI_REPAIR_THRESHOLD          text below this is repaired (default 0.6)
  AI_REPAIR_TABLE_THRESHOLD    tables below this are repaired (default 0.8)
  AI_REPAIR_MAX_ELEMENTS       most elements repaired per document (default 15), a cost cap
  BEDROCK_PRICE_IN_PER_M, BEDROCK_PRICE_OUT_PER_M
                               dollars per million input and output tokens (default 0.06 and 0.24, Nova Lite)
"""
import io
import os
import re

from . import confidence as conf
from . import mdparse

DEFAULT_MODEL = "amazon.nova-lite-v1:0"
TABLE_PAD = 8          # points of page around a table when it is cut out for the model
TEXT_DPI = 110
TABLE_DPI = 150


def settings():
    return {
        "enabled": os.getenv("AI_REPAIR_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"),
        "model": os.getenv("BEDROCK_MODEL_ID") or DEFAULT_MODEL,
        "text_threshold": float(os.getenv("AI_REPAIR_THRESHOLD", "0.6")),
        "table_threshold": float(os.getenv("AI_REPAIR_TABLE_THRESHOLD", "0.8")),
        "max_elements": int(os.getenv("AI_REPAIR_MAX_ELEMENTS", "15")),
        "price_in": float(os.getenv("BEDROCK_PRICE_IN_PER_M", "0.06")),
        "price_out": float(os.getenv("BEDROCK_PRICE_OUT_PER_M", "0.24")),
    }


def make_client():
    import boto3
    kwargs = {"region_name": os.getenv("BEDROCK_REGION") or "us-east-1"}
    key, secret = os.getenv("BEDROCK_AWS_ACCESS_KEY_ID"), os.getenv("BEDROCK_AWS_SECRET_ACCESS_KEY")
    if key and secret:
        kwargs.update(aws_access_key_id=key, aws_secret_access_key=secret,
                      aws_session_token=os.getenv("BEDROCK_AWS_SESSION_TOKEN") or None)
    return boto3.client("bedrock-runtime", **kwargs)


TABLE_PROMPT = (
    "The picture shows one table from a PDF page. Below is what an automatic reader extracted from it.\n"
    "It may have wrong, missing, merged or split cells.\n\n{current}\n\n"
    "Read every cell from the picture and return the corrected table as a Markdown pipe table: a header row, "
    "a separator row, then the body rows. Return only the table, no comments."
)
TEXT_PROMPT = (
    "The picture shows one page of a PDF. Below is a passage an automatic reader extracted from it. "
    "It may contain scrambled or wrong characters, words run together or split apart.\n\n{current}\n\n"
    "Return that passage as it is printed on the page, with the same content and no additions, "
    "summaries or comments. Return only the passage."
)


def ask(client, model, prompt, png, max_tokens=2000):
    """One Nova call with one picture. Returns (answer text, input tokens, output tokens)."""
    response = client.converse(
        modelId=model,
        messages=[{"role": "user", "content": [
            {"text": prompt},
            {"image": {"format": "png", "source": {"bytes": png}}},
        ]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    text = "".join(part.get("text", "") for part in response["output"]["message"]["content"])
    usage = response.get("usage", {})
    return text.strip(), int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))


def strip_fences(text):
    text = text.strip()
    text = re.sub(r"^```[A-Za-z]*\s*\n", "", text)
    return re.sub(r"\n```\s*$", "", text).strip()


FORMULA_PROMPT = (
    "The picture shows one page of a PDF. An automatic reader extracted this garbled piece of a math "
    "formula from it:\n\n{current}\n\n"
    "Find that formula on the page and return only that one formula as LaTeX. "
    "No $ signs, no explanation, no surrounding text."
)
MATH_CHARS = set("≤≥λ−∑∏√∈×·∥≈≠∞αβγδεθμσφωΔΣ=^")


def looks_like_math(text):
    """Weak text that is really a formula: has math symbols and almost no real words."""
    t = text.strip()
    if not t or len(t) > 400:
        return False
    words = re.findall(r"[A-Za-z]{6,}", t)
    return any(c in MATH_CHARS for c in t) and len(words) <= 1


def latex_from_answer(answer):
    t = re.sub(r"^```[A-Za-z]*\s*\n|\n```\s*$", "", answer.strip()).strip()
    return re.sub(r"^(\$\$|\\\[|\$)\s*|\s*(\$\$|\\\]|\$)$", "", t).strip()


KNOWN_COMMANDS = set("""left right sum prod frac dfrac sqrt max min leq geq le ge neq ne approx equiv sim propto
lambda kappa sigma mu alpha beta gamma delta epsilon theta rho phi omega pi tau eta nu xi zeta Sigma Delta Lambda
operatorname text mathbf mathrm mathcal mathbb mathit boldsymbol cdot times in infty log exp ln sin cos tan lim sup inf arg
mid top lVert rVert Vert lceil rceil lfloor rfloor langle rangle circ odot otimes oplus partial nabla ldots cdots dots quad
bar hat tilde vec to rightarrow Rightarrow subset subseteq cup cap forall exists limits""".split())


def latex_renders(t):
    """True if matplotlib's math parser accepts it. Skipped (True) when matplotlib is not installed."""
    try:
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import MathTextParser
    except Exception:
        # no matplotlib: fall back to a list of common LaTeX commands, anything else is rejected
        return all(c in KNOWN_COMMANDS for c in re.findall(r"\\([A-Za-z]+)", t))
    try:
        MathTextParser("path").parse("$" + t + "$", dpi=72, prop=FontProperties())
        return True
    except Exception:
        return False


def latex_ok(t):
    """Cheap sanity check: short, balanced, mostly symbols, has real LaTeX or math structure."""
    if not t or len(t) > 600 or "\n\n" in t:
        return False
    if t.count("{") != t.count("}"):
        return False
    if t.count("(") != t.count(")") and "\\left" not in t and "\\right" not in t:
        return False
    plain = re.sub(r"\\[A-Za-z]+", " ", t)
    if len(re.findall(r"[A-Za-z]{6,}", plain)) > 1:
        return False
    if not latex_renders(t):
        return False
    return bool(re.search(r"\\[A-Za-z]+|\^|_|=", t))


def candidates(doc, cfg):
    """Weak text and weak tables, weakest first. Scanned pages are Azure's job and are skipped."""
    found = []
    for e in doc.elements:
        if not e.in_markdown or e.data.get("scanned") or "ocr" in e.flags or "ai_repaired" in e.flags:
            continue
        if e.type == "table" and e.confidence < cfg["table_threshold"] and e.data.get("box"):
            found.append(e)
        elif e.type == "text" and e.confidence < cfg["text_threshold"] and e.content.strip():
            found.append(e)
    return sorted(found, key=lambda e: e.confidence)


def rows_from_pipe_table(block):
    """Rows of a Markdown pipe table. A pipe written as \\| stays inside its cell (PPL\\|Time is one cell)."""
    rows = []
    for line in block.strip().splitlines():
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


def table_from_answer(answer):
    lines = [l for l in strip_fences(answer).splitlines() if l.strip().startswith("|")]
    if len(lines) < 2:
        return None
    rows = rows_from_pipe_table("\n".join(lines))
    return rows if len(rows) >= 2 else None


def repair_document(doc, pdf_path, client=None):
    """Repairs weak elements of doc in place. Returns doc. Never raises: problems become warnings."""
    cfg = settings()
    doc.cost.setdefault("ai_calls", 0)
    if not cfg["enabled"]:
        doc.warnings.append("AI repair was asked for but AI_REPAIR_ENABLED is not true, so it was skipped.")
        return doc
    todo = candidates(doc, cfg)
    if not todo:
        return doc
    if len(todo) > cfg["max_elements"]:
        doc.warnings.append(f"AI repair handled the {cfg['max_elements']} weakest of {len(todo)} weak elements "
                            f"(AI_REPAIR_MAX_ELEMENTS).")
        todo = todo[:cfg["max_elements"]]

    try:
        import pdfplumber
        client = client or make_client()
        pdf = pdfplumber.open(str(pdf_path))
    except Exception as error:
        doc.warnings.append(f"AI repair could not start: {error}")
        return doc

    repairs, tokens_in, tokens_out, calls = [], 0, 0, 0
    page_images = {}
    try:
        for e in todo:
            page = pdf.pages[e.page - 1]
            is_math = False
            try:
                if e.type == "table":
                    x0, top, x1, bottom = e.data["box"]
                    box = (max(x0 - TABLE_PAD, page.bbox[0]), max(top - TABLE_PAD, page.bbox[1]),
                           min(x1 + TABLE_PAD, page.bbox[2]), min(bottom + TABLE_PAD, page.bbox[3]))
                    image = page.crop(box).to_image(resolution=TABLE_DPI).original
                    prompt = TABLE_PROMPT.format(current=e.content)
                else:
                    if e.page not in page_images:
                        page_images[e.page] = page.to_image(resolution=TEXT_DPI).original
                    image = page_images[e.page]
                    is_math = looks_like_math(e.content)
                    prompt = (FORMULA_PROMPT if is_math else TEXT_PROMPT).format(current=e.content)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                answer, n_in, n_out = ask(client, cfg["model"], prompt, buffer.getvalue(),
                                          max_tokens=400 if is_math else 2000)
            except Exception as error:
                doc.warnings.append(f"AI repair stopped at page {e.page}: {error}")
                break
            calls += 1
            tokens_in += n_in
            tokens_out += n_out

            before, score_before = e.content, e.confidence
            if e.type == "table":
                rows = table_from_answer(answer)
                after = mdparse.table_markdown(rows) if rows else ""
                score_after = conf.table_confidence(rows) if rows else 0.0
            else:
                rows = None
                if is_math:
                    after = latex_from_answer(answer)
                    score_after = 0.85 if latex_ok(after) else 0.0
                else:
                    after = strip_fences(answer)
                    score_after = conf.text_confidence(after) if after else 0.0
                    # a rewrite that is much shorter or longer than the original is not a repair of it
                    if not (0.5 <= len(after) / max(len(before), 1) <= 1.6):
                        score_after = 0.0

            accepted = score_after > score_before
            repairs.append({
                "id": e.id, "page": e.page, "type": "formula" if is_math else e.type, "accepted": accepted,
                "before": before, "after": after,
                "score_before": round(score_before, 3), "score_after": round(score_after, 3),
                "input_tokens": n_in, "output_tokens": n_out,
            })
            if accepted:
                e.data["before"] = before
                e.data["score_before"] = round(score_before, 3)
                if rows is not None:
                    e.data["rows"] = rows
                if is_math:
                    e.type = "formula"
                e.content, e.confidence, e.source = after, score_after, f"ai:{cfg['model']}"
                e.flags.append("ai_repaired")
                if score_after >= conf.LOW and "low_confidence" in e.flags:
                    e.flags.remove("low_confidence")
            else:
                e.flags.append("ai_rejected")
    finally:
        pdf.close()

    cost = (tokens_in * cfg["price_in"] + tokens_out * cfg["price_out"]) / 1_000_000
    doc.cost.update({
        "ai_model": cfg["model"], "ai_calls": calls,
        "ai_input_tokens": tokens_in, "ai_output_tokens": tokens_out,
        "ai_cost_usd": round(cost, 6),
        "ai_cost_per_page_usd": round(cost / max(doc.pages, 1), 6),
        "repairs": repairs,
    })
    return doc
