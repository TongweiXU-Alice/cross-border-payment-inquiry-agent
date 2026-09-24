import os
import tempfile
import unittest
from pathlib import Path

from agent import run_agent
from agent.understanding import deterministic_understand
from setup_db import DEFAULT_WORKBOOK, build


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="payment-agent-test-")
        cls.db_path = Path(cls.temp_dir.name) / "payments.db"
        cls.previous_db = os.environ.get("PAYMENT_AGENT_DB")
        os.environ["PAYMENT_AGENT_DB"] = str(cls.db_path)
        build(db_path=cls.db_path, workbook_path=DEFAULT_WORKBOOK, force=True)

    @classmethod
    def tearDownClass(cls):
        if cls.previous_db is None:
            os.environ.pop("PAYMENT_AGENT_DB", None)
        else:
            os.environ["PAYMENT_AGENT_DB"] = cls.previous_db
        cls.temp_dir.cleanup()

    def test_simple_status(self):
        result = run_agent("客户问FL260002现在钱到哪里了。")
        self.assertEqual(result.verified_facts["bank_status"], "PROCESSING")
        self.assertEqual(result.decision, "ANSWER")
        self.assertTrue(result.tool_trace)

    def test_ambiguous_lookup_is_clarify(self):
        result = run_agent("客户查9月20日Citi的一笔5000 USD出金，没有FL号。")
        self.assertEqual(result.decision, "CLARIFY")
        self.assertEqual(result.verified_facts["candidate_count"], 3)
        self.assertEqual(len(result.matched_transactions), 3)
        self.assertIn("请提供FL号", result.draft_response)

    def test_fee_grounding_and_claim_classification(self):
        result = run_agent(
            "客户汇了10000 USD，FL260013只收到9950，认为平台扣了50手续费，请确认。"
        )
        self.assertEqual(result.verified_facts["amount_difference"], 50.0)
        self.assertEqual(result.verified_facts["verified_fee"], 10.0)
        self.assertEqual(result.verified_facts["unexplained_difference"], 40.0)
        self.assertIn("remaining_amount_difference_reason", result.unsupported)
        self.assertIn("customer_claim_assessment", result.verified_facts)
        self.assertIn("不能直接归因", result.draft_response)

    def test_status_conflict_is_escalated_and_shows_both_values(self):
        result = run_agent(
            "FL260024内部仍显示Processing，但银行侧显示Completed，客户催问资金状态。"
        )
        self.assertEqual(result.decision, "ESCALATE")
        self.assertIn("INTERNAL_BANK_STATUS_CONFLICT", result.conflicts)
        self.assertEqual(result.verified_facts["status_conflict"], {
            "internal_status": "PROCESSING",
            "bank_status": "COMPLETED",
        })
        self.assertIn("分别为内部 PROCESSING、银行 COMPLETED", result.draft_response)

    def test_uppercase_gpi_uses_document_metadata_tool(self):
        inquiry = deterministic_understand("请提供FL260001的GPI")
        self.assertEqual(inquiry.intents, ["DOCUMENT_REQUEST"])
        result = run_agent("请提供FL260001的GPI")
        self.assertIn("get_document", {event["name"] for event in result.tool_trace})
        self.assertTrue(result.verified_facts["document_metadata_available"])
        self.assertFalse(result.verified_facts["document_downloadable"])
        self.assertIn("不提供真实银行文件下载", result.draft_response)

    def test_missing_fee_evidence_cannot_answer(self):
        result = run_agent("FL260002手续费是多少？")
        self.assertNotEqual(result.decision, "ANSWER")
        self.assertIn("verified_fee_evidence_missing", result.unsupported)

    def test_missing_refund_evidence_cannot_answer(self):
        result = run_agent("FL260002退款到哪里了？")
        self.assertNotEqual(result.decision, "ANSWER")
        self.assertIn("refund_record_missing", result.unsupported)

    def test_non_domain_input_does_not_scan_all_transactions(self):
        result = run_agent("今天天气怎么样？")
        self.assertEqual(result.decision, "CLARIFY")
        self.assertEqual(result.tool_trace, [])
        self.assertEqual(result.matched_transactions, [])

    def test_missing_database_is_a_controlled_escalation(self):
        previous = os.environ["PAYMENT_AGENT_DB"]
        os.environ["PAYMENT_AGENT_DB"] = str(Path(self.temp_dir.name) / "missing.db")
        try:
            result = run_agent("客户问FL260002现在钱到哪里了。")
        finally:
            os.environ["PAYMENT_AGENT_DB"] = previous
        self.assertEqual(result.decision, "ESCALATE")
        self.assertIn("synthetic_data_source_unavailable", result.unsupported)
        self.assertTrue(result.human_review_required)

    def test_processing_age_uses_explicit_snapshot(self):
        result = run_agent(
            "FL260016已经Processing四天了，为什么还没到？",
            as_of="2026-09-22 12:00",
        )
        self.assertEqual(result.verified_facts["processing_age_days"], 5)
        self.assertEqual(result.decision, "ESCALATE")

    def test_trace_is_minimal_and_counts_dict_as_one_record(self):
        result = run_agent("请提供FL260001的GPI")
        document_event = next(event for event in result.tool_trace if event["name"] == "get_document")
        self.assertEqual(document_event["record_count"], 1)
        self.assertNotIn("output", document_event)
        self.assertIn("source", document_event)


if __name__ == "__main__":
    unittest.main()
