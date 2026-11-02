"""AI repair with a fake Bedrock client, so no AWS call is made. Run: python -m unittest discover -s tests -v"""
import os
import unittest
from pathlib import Path
from unittest import mock

from engine import ai, mdparse
from engine import confidence as conf
from engine.schema import Document, Element

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "data" / "pdfs" / "ACE.pdf"

WEAK_ROWS = [["Quarter", "North", "", ""], ["Q1", "", "95", ""], ["Q2", "135", "", "91"]]
GOOD_ROWS = [["Quarter", "North", "South", "East"], ["Q1", "120", "95", "110"], ["Q2", "135", "102", "91"]]
GARBLED = "tħe m∂thodpr0posedbyth e aut hors x q z k tħe m∂thodpr0posedbyth e aut hors"
CLEAN = "The method proposed by the authors reads the page in the order a person would and checks each line."


class FakeBedrock:
    """Returns the scripted answers one by one and reports token counts."""
    def __init__(self, answers, tokens_in=1000, tokens_out=200):
        self.answers, self.calls = list(answers), 0
        self.tokens_in, self.tokens_out = tokens_in, tokens_out

    def converse(self, **kwargs):
        self.calls += 1
        assert kwargs["modelId"]
        assert kwargs["messages"][0]["content"][1]["image"]["format"] == "png"
        return {"output": {"message": {"content": [{"text": self.answers.pop(0)}]}},
                "usage": {"inputTokens": self.tokens_in, "outputTokens": self.tokens_out}}


def doc_with(*elements):
    doc = Document(source=str(PDF), level="engine", pages=10)
    doc.elements = list(elements)
    mdparse.finish_flags(doc.elements)
    doc.number()
    return doc


def weak_table():
    return Element("table", 1, mdparse.table_markdown(WEAK_ROWS), conf.table_confidence(WEAK_ROWS), "pdfplumber",
                   data={"rows": WEAK_ROWS, "box": [100, 100, 400, 250]})


def md_table(rows):
    return mdparse.table_markdown(rows)


ON = {"AI_REPAIR_ENABLED": "true"}


@unittest.skipUnless(PDF.exists(), "needs data/pdfs/ACE.pdf")
class AiRepairTests(unittest.TestCase):
    def test_off_by_default(self):
        doc = doc_with(weak_table())
        client = FakeBedrock([md_table(GOOD_ROWS)])
        with mock.patch.dict(os.environ, {"AI_REPAIR_ENABLED": "false"}):
            ai.repair_document(doc, PDF, client=client)
        self.assertEqual(client.calls, 0)
        self.assertNotIn("ai_repaired", doc.elements[0].flags)
        self.assertTrue(any("AI_REPAIR_ENABLED" in w for w in doc.warnings))

    def test_weak_table_is_repaired_and_original_kept(self):
        el = weak_table()
        before = el.content
        doc = doc_with(el)
        self.assertLess(el.confidence, 0.8)
        with mock.patch.dict(os.environ, ON):
            ai.repair_document(doc, PDF, client=FakeBedrock([md_table(GOOD_ROWS)]))
        self.assertIn("ai_repaired", el.flags)
        self.assertEqual(el.data["before"], before)
        self.assertGreater(el.confidence, el.data["score_before"])
        self.assertTrue(el.source.startswith("ai:"))
        self.assertIn("| Q1 | 120 | 95 | 110 |", el.content)
        self.assertEqual(el.data["rows"], GOOD_ROWS)

    def test_garbled_text_is_repaired(self):
        el = Element("text", 1, GARBLED, conf.text_confidence(GARBLED), "reader")
        doc = doc_with(el)
        self.assertLess(el.confidence, 0.6)
        with mock.patch.dict(os.environ, ON):
            ai.repair_document(doc, PDF, client=FakeBedrock([CLEAN]))
        self.assertEqual(el.content, CLEAN)
        self.assertNotIn("low_confidence", el.flags)

    def test_ai_made_it_worse_original_is_kept_and_logged(self):
        el = weak_table()
        before = el.content
        doc = doc_with(el)
        worse = "| 1 | 2 | 3 | 4 |\n|---|---|---|---|\n| | | | |\n| | | | |"   # a valid table, but it lost the header words and the cells
        with mock.patch.dict(os.environ, ON):
            ai.repair_document(doc, PDF, client=FakeBedrock([worse]))
        self.assertEqual(el.content, before)
        self.assertIn("ai_rejected", el.flags)
        self.assertNotIn("ai_repaired", el.flags)
        log = doc.cost["repairs"][0]
        self.assertFalse(log["accepted"])
        self.assertIn("| 1 | 2 | 3 | 4 |", log["after"])   # what the AI said is kept for the report
        self.assertLessEqual(log["score_after"], log["score_before"])

    def test_answer_that_is_not_a_table_is_rejected(self):
        el = weak_table()
        before = el.content
        doc = doc_with(el)
        with mock.patch.dict(os.environ, ON):
            ai.repair_document(doc, PDF, client=FakeBedrock(["Sorry, I cannot read this table."]))
        self.assertEqual(el.content, before)
        self.assertEqual(doc.cost["repairs"][0]["after"], "Sorry, I cannot read this table.")
        self.assertFalse(doc.cost["repairs"][0]["accepted"])

    def test_text_rewrite_of_very_different_length_is_rejected(self):
        el = Element("text", 1, GARBLED, conf.text_confidence(GARBLED), "reader")
        doc = doc_with(el)
        with mock.patch.dict(os.environ, ON):
            ai.repair_document(doc, PDF, client=FakeBedrock(["Short."]))
        self.assertEqual(el.content, GARBLED)
        self.assertFalse(doc.cost["repairs"][0]["accepted"])

    def test_cost_meter(self):
        doc = doc_with(weak_table())
        with mock.patch.dict(os.environ, ON):
            ai.repair_document(doc, PDF, client=FakeBedrock([md_table(GOOD_ROWS)], tokens_in=1_000_000, tokens_out=500_000))
        # 1M input at $0.06 and 0.5M output at $0.24 per million
        self.assertAlmostEqual(doc.cost["ai_cost_usd"], 0.06 + 0.12, places=6)
        self.assertAlmostEqual(doc.cost["ai_cost_per_page_usd"], 0.018, places=6)
        self.assertEqual(doc.cost["ai_calls"], 1)

    def test_strong_elements_and_scans_are_not_sent(self):
        strong = Element("text", 1, CLEAN, conf.text_confidence(CLEAN), "reader")
        scan = Element("text", 2, "", 0.0, "reader", flags=["no_text_layer"], data={"scanned": True})
        doc = doc_with(strong, scan)
        client = FakeBedrock([])
        with mock.patch.dict(os.environ, ON):
            ai.repair_document(doc, PDF, client=client)
        self.assertEqual(client.calls, 0)

    def test_cap_on_elements(self):
        doc = doc_with(*[weak_table() for _ in range(5)])
        client = FakeBedrock([md_table(GOOD_ROWS)] * 5)
        with mock.patch.dict(os.environ, {**ON, "AI_REPAIR_MAX_ELEMENTS": "2"}):
            ai.repair_document(doc, PDF, client=client)
        self.assertEqual(client.calls, 2)
        self.assertTrue(any("weakest" in w for w in doc.warnings))

    def test_failure_becomes_a_warning(self):
        class Broken:
            def converse(self, **kwargs):
                raise RuntimeError("ExpiredToken")
        el = weak_table()
        doc = doc_with(el)
        with mock.patch.dict(os.environ, ON):
            ai.repair_document(doc, PDF, client=Broken())
        self.assertTrue(any("ExpiredToken" in w for w in doc.warnings))
        self.assertNotIn("ai_repaired", el.flags)


if __name__ == "__main__":
    unittest.main()
