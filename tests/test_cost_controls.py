import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cost_controls import (
    fallback_model_env,
    load_checkpoint,
    record_openai_usage,
    resolve_model,
    save_checkpoint,
    signature,
)


class CostControlTests(unittest.TestCase):
    def test_stage_specific_model_overrides_global_default(self):
        with patch.dict(os.environ, {
            "OPENAI_MODEL": "gpt-6.1-sol",
            "OPENAI_MODEL_METADATA": "gpt-6-luna",
        }, clear=False):
            self.assertEqual(resolve_model("metadata"), "gpt-6-luna")
            self.assertEqual(resolve_model("research"), "gpt-6.1-sol")
            self.assertEqual(resolve_model("research", "explicit"), "explicit")
            self.assertEqual(fallback_model_env("scripting"), "OPENAI_MODEL_SCRIPT_FALLBACK")

    def test_checkpoints_reuse_only_matching_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sig = signature({"topic": "Example"})
            save_checkpoint(root, "research", sig, "research.json", {"claims": ["C1"]})
            self.assertEqual(load_checkpoint(root, "research", sig, "research.json"), {"claims": ["C1"]})
            self.assertIsNone(load_checkpoint(root, "research", signature({"topic": "Changed"}), "research.json"))

    def test_refresh_environment_bypasses_cached_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sig = signature({"topic": "Example"})
            save_checkpoint(root, "research", sig, "research.json", {"claims": ["C1"]})
            with patch.dict(os.environ, {"STORYPILOT_REFRESH_STAGES": "research"}):
                self.assertIsNone(load_checkpoint(root, "research", sig, "research.json"))

    def test_usage_report_records_tokens_cached_input_search_fee_and_aggregate(self):
        with tempfile.TemporaryDirectory() as temp:
            response = {
                "id": "resp_test",
                "usage": {
                    "input_tokens": 1000, "output_tokens": 200,
                    "input_tokens_details": {"cached_tokens": 400},
                },
                "output": [{"type": "web_search_call"}],
            }
            record_openai_usage(response, stage="research", model="gpt-6-luna", web_search=True, job_dir=temp)
            report = json.loads((Path(temp) / "cost_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["totals"]["input_tokens"], 1000)
            self.assertEqual(report["totals"]["cached_input_tokens"], 400)
            self.assertEqual(report["totals"]["web_search_calls"], 1)
            self.assertEqual(report["totals"]["estimated_cost_usd"], 0.010082)
            self.assertEqual(report["events"][0]["stage"], "research")


if __name__ == "__main__":
    unittest.main()
