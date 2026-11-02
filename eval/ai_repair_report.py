"""Before and after for AI repair, with tokens and cost per page.

python eval/ai_repair_report.py data/pdfs/ACE.pdf
python eval/ai_repair_report.py data/pdfs/ACE.pdf data/pdfs/LSC.pdf

Needs AI_REPAIR_ENABLED=true and the BEDROCK_* values in .env. It runs the engine twice per PDF, once
as economy (functions only) and once as standard (functions, then Amazon Nova on the weak elements),
and writes eval/ai_repair_report.md. Attempts that were not accepted are listed too, either as
"No change" (the AI returned the same content) or "AI made it worse" (it scored lower, so the
original was kept).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from engine import extract_pdf  # noqa: E402


def block(text):
    return "\n".join("    " + line for line in (text or "(empty)").splitlines())


def verdict_for(r):
    if r["accepted"]:
        return "Repaired"
    if r["after"].strip() == r["before"].strip():
        return "No change, the AI returned the same content"
    return "AI made it worse, original kept"


def report(pdf):
    economy = extract_pdf(pdf, image_store=lambda n, b: "", quality="economy")
    standard = extract_pdf(pdf, image_store=lambda n, b: "", quality="standard")
    cost = standard.cost
    repairs = cost.get("repairs", [])
    counts = {
        "Repaired": sum(1 for r in repairs if r["accepted"]),
        "No change": sum(1 for r in repairs if not r["accepted"] and r["after"].strip() == r["before"].strip()),
        "AI made it worse": sum(1 for r in repairs if not r["accepted"] and r["after"].strip() != r["before"].strip()),
    }
    out = [f"## {Path(pdf).name}", ""]
    out.append(f"Pages: {standard.pages}. Weak elements sent to the model: {cost.get('ai_calls', 0)}. "
               f"Average confidence {economy.stats()['average_confidence']} (economy) and "
               f"{standard.stats()['average_confidence']} (standard).")
    out.append("")
    out.append("| | Value |\n|---|---|")
    out.append(f"| Model | {cost.get('ai_model', '-')} |")
    out.append(f"| Repaired | {counts['Repaired']} |")
    out.append(f"| No change | {counts['No change']} |")
    out.append(f"| AI made it worse | {counts['AI made it worse']} |")
    out.append(f"| Tokens in / out | {cost.get('ai_input_tokens', 0)} / {cost.get('ai_output_tokens', 0)} |")
    out.append(f"| Cost of AI repair | ${cost.get('ai_cost_usd', 0):.6f} |")
    out.append(f"| Cost per page | ${cost.get('ai_cost_per_page_usd', 0):.6f} |")
    out.append("")
    for warning in standard.warnings:
        if "AI repair" in warning:
            out.append(f"Note: {warning}\n")
    for r in repairs:
        out.append(f"### Page {r['page']}, {r['type']} ({verdict_for(r)})")
        out.append(f"Score before {r['score_before']}, after {r['score_after']}. "
                   f"Tokens {r['input_tokens']} in, {r['output_tokens']} out.\n")
        out.append("Before:\n\n" + block(r["before"]) + "\n")
        out.append("After:\n\n" + block(r["after"]) + "\n")
    return "\n".join(out)


def main():
    pdfs = sys.argv[1:]
    if not pdfs:
        raise SystemExit(__doc__)
    if os.getenv("AI_REPAIR_ENABLED", "").lower() not in ("1", "true", "yes", "on"):
        raise SystemExit("Set AI_REPAIR_ENABLED=true in .env first.")
    parts = ["# AI repair: before and after", ""]
    for pdf in pdfs:
        parts.append(report(pdf))
    target = ROOT / "eval" / "ai_repair_report.md"
    target.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
