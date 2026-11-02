"""A very brief summary of an extracted document, written by Amazon Nova on Bedrock.

Text only: the Markdown the engine produced goes in, three or four sentences come out. Off unless
AI_REPAIR_ENABLED=true (the same switch and the same Bedrock keys as AI repair).
"""
import re
import time

from . import ai

MAX_CHARS = 60000   # about 15,000 tokens; longer documents are cut and the summary says so

PROMPT = (
    "Below is a document that was extracted from a PDF or web page as Markdown.\n"
    "Write a very brief summary of it in 3 to 4 sentences, for someone deciding whether to read it. "
    "Say what it is about, what it found or argues, and who it is for. "
    "Use only what the text says. Do not add facts, opinions or a preamble. Plain text, no bullet points.\n\n"
    "DOCUMENT:\n{text}"
)


def clean(markdown):
    """Drop image links and long runs of blank lines so the model reads text, not noise."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def summarize(markdown, client=None):
    """Returns {"summary", "truncated", "input_tokens", "output_tokens", "cost_usd", "seconds", "model"}."""
    cfg = ai.settings()
    if not cfg["enabled"]:
        raise RuntimeError("Summaries are off. Set AI_REPAIR_ENABLED=true and the Bedrock keys where the API runs.")
    text = clean(markdown)
    if not text:
        raise ValueError("There is no text to summarize.")
    truncated = len(text) > MAX_CHARS
    started = time.time()
    client = client or ai.make_client()
    response = client.converse(
        modelId=cfg["model"],
        messages=[{"role": "user", "content": [{"text": PROMPT.format(text=text[:MAX_CHARS])}]}],
        inferenceConfig={"maxTokens": 300, "temperature": 0},
    )
    answer = "".join(part.get("text", "") for part in response["output"]["message"]["content"]).strip()
    usage = response.get("usage", {})
    n_in, n_out = int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))
    cost = (n_in * cfg["price_in"] + n_out * cfg["price_out"]) / 1_000_000
    return {
        "summary": answer, "truncated": truncated, "model": cfg["model"],
        "input_tokens": n_in, "output_tokens": n_out,
        "cost_usd": round(cost, 6), "seconds": round(time.time() - started, 1),
    }
