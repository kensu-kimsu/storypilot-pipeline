import json
import tempfile
import unittest
from pathlib import Path

from prepost_pipeline import asset_research, fulfill_news_headline_requests, is_duplicate, qa_package


class PrePostPipelineTests(unittest.TestCase):
    def test_history_duplicate_detection(self):
        history = [{"topic": "How Rockefeller Built Standard Oil"}]
        self.assertTrue(is_duplicate("How Rockefeller Built Standard Oil", history))
        self.assertFalse(is_duplicate("The Rise of Japanese Robotics", history))

    def test_qa_flags_unlinked_claims_and_missing_production_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "script.json").write_text(json.dumps({"unsupported_claim_ids": ["C99"], "target_met_estimate": False}))
            (root / "research.json").write_text(json.dumps({"claims": [{"claim_id": "C1", "url": ""}]}))
            issues = qa_package(root)
            self.assertTrue(any("Missing metadata_ab_test.json" in issue for issue in issues))
            self.assertTrue(any("claim IDs absent" in issue for issue in issues))
            self.assertTrue(any("no source URL" in issue for issue in issues))

    def test_asset_brief_compiles_visual_music_and_sfx_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = asset_research({"narration_chunks": [{
                "visual_note": "Show an oil refinery at dusk",
                "asset_queries": ["historical refinery", "oil pipeline map"],
                "music_mood": "low tension orchestral", "sfx_cues": ["paper rustle", "deep impact"]
            }]}, Path(temp))
            categories = [asset["category"] for asset in manifest["assets"]]
            self.assertEqual(categories, ["video", "picture", "music", "sfx", "sfx"])
            self.assertEqual(manifest["assets"][0]["max_duration_seconds"], 5)

    def test_news_headline_search_is_skipped_when_creative_brief_does_not_request_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = fulfill_news_headline_requests(
                "A topic", {"narration_chunks": [{"news_article_screenshot": None}]},
                root / "assets", job_id="test-job",
            )
            self.assertFalse(result["searched"])
            self.assertFalse((root / "assets" / "screenshots").exists())

    def test_creative_brief_request_creates_news_headline_asset(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = asset_research({"narration_chunks": [{
                "news_article_screenshot": {
                    "search_query": "company scandal newspaper headline",
                    "highlight_keywords": ["scandal", "company"],
                    "reason": "Show how the story broke publicly",
                }
            }]}, Path(temp))
            request = manifest["assets"][0]
            self.assertEqual(request["category"], "news_headline_screenshot")
            self.assertEqual(request["status"], "awaiting_search")
            self.assertEqual(request["highlight_keywords"], ["scandal", "company"])


if __name__ == "__main__":
    unittest.main()
