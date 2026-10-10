import json
import tempfile
import unittest
from pathlib import Path

from prepost_pipeline import asset_research, is_duplicate, qa_package, research_screenshot_targets


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

    def test_research_screenshot_targets_deduplicate_and_prefer_high_confidence(self):
        research = {"claims": [
            {"claim_id": "C1", "url": "https://example.org/article", "source_title": "Example article", "confidence": "low"},
            {"claim_id": "C2", "url": "https://example.org/article", "source_title": "Duplicate", "confidence": "high"},
            {"claim_id": "C3", "url": "file:///etc/passwd", "source_title": "Local file", "confidence": "high"},
            {"claim_id": "C4", "url": "http://127.0.0.1/admin", "source_title": "Local service", "confidence": "high"},
            {"claim_id": "C5", "url": "https://news.example.net/story", "source_title": "News story", "confidence": "medium"},
        ]}
        targets = research_screenshot_targets(research, limit=5)
        self.assertEqual([row["claim_id"] for row in targets], ["C2", "C5"])
        self.assertEqual(research_screenshot_targets(research, limit=0), [])
        manual = research_screenshot_targets(research, limit=2, extra_urls=["https://news.example.com/story"])
        self.assertEqual(manual[0]["confidence"], "user_provided")
        self.assertEqual(manual[0]["url"], "https://news.example.com/story")


if __name__ == "__main__":
    unittest.main()
