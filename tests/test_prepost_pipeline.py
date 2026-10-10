import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prepost_pipeline import (
    asset_research,
    fulfill_news_headline_requests,
    get_niche_config,
    is_duplicate,
    qa_package,
    verify_research_claims,
    write_script,
)


class PrePostPipelineTests(unittest.TestCase):
    def test_niche_profiles_supply_english_expressive_voice_and_retention_guidance(self):
        config = get_niche_config("business history documentaries")
        self.assertEqual(config["voice_name"], "en-US-Ethan:MAI-Voice-2.1")
        self.assertIn("pay off", config["retention_guidance"])
        self.assertIn("filings", config["research_prompt"])

    def test_verification_excludes_unsupported_claims_from_script_evidence(self):
        research = {"claims": [
            {"claim_id": "C1", "claim": "Supported fact", "url": "https://example.org/a"},
            {"claim_id": "C2", "claim": "Weak claim", "url": "https://example.org/b"},
        ]}
        reply = {"output_text": json.dumps({"results": [
            {"claim_id": "C1", "status": "verified", "verification_note": "Corroborated", "checked_sources": [{"title": "Record", "url": "https://example.org/record", "support": "Confirms it"}]},
            {"claim_id": "C2", "status": "unverified", "verification_note": "No corroboration", "checked_sources": []},
        ]})}
        with patch("prepost_pipeline.openai_response", return_value=reply):
            checked = verify_research_claims("Example", research)
        self.assertEqual([claim["claim_id"] for claim in checked["verified_claims"]], ["C1"])
        self.assertEqual(checked["verification_summary"]["status"], "passed")

    def test_script_prompt_sets_narrator_delivery_and_retention_contract(self):
        research = {"verified_claims": [{"claim_id": "C1", "claim": "A documented fact", "url": "https://example.org"}]}
        script = {
            "narration_direction": "English; curious, then grave.",
            "narration_chunks": [{"text": "A documented opening.", "claim_ids": ["C1"], "tone": "curious", "delivery_emotion": "intrigued", "delivery_pace": "measured", "vocal_intensity": 0.4, "pause_after_seconds": 0.6}],
            "shorts_script": {"text": "A complete short story.", "claim_ids": ["C1"]},
        }
        captured = {}
        def fake_response(prompt, **kwargs):
            captured["prompt"] = prompt
            return {"output_text": json.dumps(script)}
        channel = {"niche": "business", "visual_brand": {"primary_font": "Montserrat", "brand_accent": "#FFD34E"}}
        with patch("prepost_pipeline.openai_response", side_effect=fake_response):
            result = write_script("Example", channel, research, get_niche_config("business"))
        self.assertIn("truthful story promise", captured["prompt"])
        self.assertIn("visual_evidence", captured["prompt"])
        self.assertIn("Choose it because it expresses the verified evidence", captured["prompt"])
        self.assertIn("Montserrat", captured["prompt"])
        self.assertIn("English", captured["prompt"])
        self.assertEqual(result["voice_name"], "en-US-Ethan:MAI-Voice-2.1")
        self.assertEqual(result["visual_brand"], channel["visual_brand"])
        self.assertFalse(result["narration_audio_generated"])
        self.assertFalse(result["unsupported_claim_ids"])

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
                "visual_mode": "document_focus",
                "visual_evidence": "A dated refinery expansion record",
                "visual_rationale": "Connect the facility image to the documented expansion",
                "asset_queries": ["historical refinery", "oil pipeline map"],
                "music_mood": "low tension orchestral", "sfx_cues": ["paper rustle", "deep impact"]
            }]}, Path(temp))
            categories = [asset["category"] for asset in manifest["assets"]]
            self.assertEqual(categories, ["video", "picture", "music", "sfx", "sfx"])
            self.assertEqual(manifest["assets"][0]["max_duration_seconds"], 5)
            self.assertEqual(manifest["assets"][0]["visual_mode"], "document_focus")
            self.assertEqual(manifest["assets"][0]["visual_evidence"], "A dated refinery expansion record")

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
