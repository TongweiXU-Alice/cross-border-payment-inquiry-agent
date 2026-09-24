import os
import tempfile
import unittest
from pathlib import Path

from evaluate import run_evaluation
from setup_db import DEFAULT_WORKBOOK, build


class EvaluationTests(unittest.TestCase):
    def test_checked_in_scenarios_pass_all_dimensions(self):
        with tempfile.TemporaryDirectory(prefix="payment-agent-eval-") as directory:
            db_path = Path(directory) / "payments.db"
            previous_db = os.environ.get("PAYMENT_AGENT_DB")
            os.environ["PAYMENT_AGENT_DB"] = str(db_path)
            try:
                build(db_path=db_path, workbook_path=DEFAULT_WORKBOOK, force=True)
                summary = run_evaluation(DEFAULT_WORKBOOK)
            finally:
                if previous_db is None:
                    os.environ.pop("PAYMENT_AGENT_DB", None)
                else:
                    os.environ["PAYMENT_AGENT_DB"] = previous_db
        self.assertEqual(summary["case_count"], 20)
        self.assertEqual(summary["passed_cases"], 20)
        self.assertTrue(all(value == 20 for value in summary["dimension_pass_counts"].values()))


if __name__ == "__main__":
    unittest.main()
