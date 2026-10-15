import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from prepost_pipeline import (
    create_thumbnail_from_reference,
    make_metadata,
    resolve_thumbnail_reference,
    asset_research,
    fulfill_news_headline_requests,
    get_niche_config,
    is_duplicate,
    qa_package,
    search_asset_sources,
    validate_graphic_spec,
    verify_research_claims,
    write_script,
)


class PrePostPipelineTests(unittest.TestCase):
    def test_metadata_requests_and_keeps_only_one_thumbnail_brief(self):
        reply = {"output_text": json.dumps({
            "title_variants": [{"title": "A", "hypothesis": "A"}, {"title": "B", "hypothesis": "B"}, {"title": "C", "hypothesis": "C"}],
            "description": "Desc", "tags": [],
            "thumbnail_briefs": [
                {"concept": "A vault key", "composition": "large key", "text": "THE KEY", "contrast_angle": "hidden ownership"},
                {"concept": "A second idea", "composition": "fund", "text": "", "contrast_angle": "other"},
            ],
        })}
        captured = {}
        def fake_response(prompt, **kwargs):
            captured["prompt"] = prompt
            return reply
        with patch("prepost_pipeline.openai_response", side_effect=fake_response):
            result = make_metadata("Vanguard story", {"name": "Business Untold"}, {"logline": "A fund changed ownership."})
        self.assertIn("exactly ONE thumbnail brief", captured["prompt"])
        self.assertIn("recognizable named person, brand, or company", captured["prompt"])
        self.assertEqual(len(result["thumbnail_briefs"]), 1)
        self.assertEqual(result["thumbnail_briefs"][0]["concept"], "A vault key")

    def test_thumbnail_uses_real_subject_photo_and_outputs_1280x720(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "john_bogle.png"
            Image.new("RGB", (600, 400), color=(220, 30, 25)).save(source)
            output = root / "thumbnail_1.png"
            reference = {
                "path": str(source), "title": "John C. Bogle portrait",
                "source": "wikimedia_commons",
            }
            concept = {"subject": "John C. Bogle", "subject_type": "person", "text": "THE KEY"}
            create_thumbnail_from_reference(reference, concept, output, {"brand_accent": "#FFD34E"})
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (1280, 720))
                # The center of the large foreground subject panel retains the
                # source photo's red pixels rather than replacing the subject.
                self.assertGreater(rendered.getpixel((300, 300))[0], 150)

    def test_thumbnail_reference_downloads_keyless_public_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "vanguard_logo.png"
            Image.new("RGB", (300, 150), color=(25, 60, 130)).save(source)
            public_match = {
                "path": str(source), "title": "Vanguard Group logo",
                "tags": ["Vanguard logo"], "source": "wikimedia_commons",
                "source_url": "https://commons.wikimedia.org/wiki/File:Vanguard_logo.png",
                "license": "Public domain", "creator": "Example",
            }
            with patch("media_library.MediaLibrary.find_reusable", return_value=[]), \
                    patch("media_library.MediaLibrary.download_public_archives", return_value=[public_match]) as download:
                result = resolve_thumbnail_reference("Vanguard", "company", root / "assets")
            self.assertEqual(result["source"], "wikimedia_commons")
            self.assertEqual(result["subject_match_score"], 1.0)
            self.assertIn("Vanguard logo", download.call_args.args[0])

    def test_thumbnail_reference_does_not_trust_search_query_tags_as_subject_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "unrelated.png"
            Image.new("RGB", (300, 150), color=(50, 50, 50)).save(source)
            unrelated = {
                "path": str(source), "title": "Generic financial office",
                "tags": ["Vanguard logo"], "source": "wikimedia_commons",
                "metadata": {"query": "Vanguard logo"},
            }
            with patch("media_library.MediaLibrary.find_reusable", return_value=[]), \
                    patch("media_library.MediaLibrary.download_public_archives", return_value=[unrelated]):
                result = resolve_thumbnail_reference("Vanguard", "company", root / "assets")
            self.assertIsNone(result)

    def test_qa_blocks_thumbnail_when_real_reference_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "job.json").write_text(json.dumps({"thumbnail_requested": True}), encoding="utf-8")
            (root / "thumbnail_reference.json").write_text(json.dumps({"status": "subject_image_not_found"}), encoding="utf-8")
            issues = qa_package(root)
            self.assertTrue(any("Thumbnail reference is not ready" in issue for issue in issues))
            self.assertTrue(any("does not use a real downloaded source image" in issue for issue in issues))

    def test_asset_discovery_combines_social_candidates_with_archive_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = root / "jobs" / "test"
            job.mkdir(parents=True)
            manifest_path = job / "asset_requests.json"
            manifest_path.write_text(json.dumps({"assets": [{"asset_id": "v1", "category": "video", "query": "factory machinery"}]}), encoding="utf-8")
            social = [{"source": "tiktok", "url": "https://www.tiktok.com/@maker/video/123", "title": "Factory"}]
            with patch("media_library.MediaLibrary.search_public_archives", return_value=[{"source": "nasa_images", "source_url": "https://nasa.gov/item"}]), \
                    patch("social_video.discover_social_videos", return_value=social):
                result = search_asset_sources(manifest_path)
            candidates = result["assets"][0]["candidates"]
            self.assertEqual([item["source"] for item in candidates], ["tiktok", "nasa_images"])
            self.assertTrue(result["assets"][0]["social_search_performed"])

    def test_graphic_specs_accept_only_values_linked_to_scene_claims(self):
        claims = [{"claim_id": "C1", "claim": "Revenue rose from 2.0 billion dollars in 2019 to 2.5 billion dollars in 2020."}]
        chunk = {"claim_ids": ["C1"], "visual_mode": "animated_chart"}
        spec, issue = validate_graphic_spec({
            "kind": "chart", "title": "Revenue", "items": [
                {"label": "2019", "value": "2.0 billion dollars", "claim_id": "C1"},
                {"label": "2020", "value": "2.5 billion dollars", "claim_id": "C1"},
            ],
        }, chunk, claims)
        self.assertIsNone(issue)
        self.assertEqual(spec["items"][0]["numeric_value"], 2_000_000_000)
        rejected, issue = validate_graphic_spec({
            "kind": "counter", "value": "7 billion dollars", "claim_id": "C1",
        }, {"claim_ids": ["C1"], "visual_mode": "kinetic_fact"}, claims)
        self.assertIsNone(rejected)
        self.assertIn("not stated", issue)

    def test_geographic_map_requires_independently_checked_coordinates(self):
        claims = [{"claim_id": "C1", "claim": "The route ran from London to Paris.", "verified_geo_points": [
            {"name": "London", "latitude": 51.5072, "longitude": -0.1276, "source_url": "https://example.org/coords"},
            {"name": "Paris", "latitude": 48.8566, "longitude": 2.3522, "source_url": "https://example.org/coords"},
        ]}]
        chunk = {"claim_ids": ["C1"], "visual_mode": "animated_map"}
        spec, issue = validate_graphic_spec({"kind": "geographic_map", "title": "Route", "points": [
            {"name": "London", "latitude": 51.5072, "longitude": -0.1276, "claim_id": "C1"},
            {"name": "Paris", "latitude": 48.8566, "longitude": 2.3522, "claim_id": "C1"},
        ]}, chunk, claims)
        self.assertIsNone(issue)
        self.assertEqual(spec["points"][1]["source_url"], "https://example.org/coords")
        bad, issue = validate_graphic_spec({"kind": "geographic_map", "points": [
            {"name": "London", "latitude": 51.5, "longitude": -0.1276, "claim_id": "C1"},
            {"name": "Paris", "latitude": 48.8566, "longitude": 2.3522, "claim_id": "C1"},
        ]}, chunk, claims)
        self.assertIsNone(bad)
        self.assertIn("not independently verified", issue)

    def test_document_highlight_phrase_must_be_claim_linked(self):
        claims = [{"claim_id": "C1", "claim": "The company filed for bankruptcy in 2001."}]
        chunk = {"claim_ids": ["C1"], "visual_mode": "document_focus"}
        spec, issue = validate_graphic_spec({"kind": "document_highlight", "claim_id": "C1", "target_text": "filed for bankruptcy"}, chunk, claims)
        self.assertIsNone(issue)
        self.assertEqual(spec["target_text"], "filed for bankruptcy")
        bad, issue = validate_graphic_spec({"kind": "document_highlight", "claim_id": "C1", "target_text": "secret plot"}, chunk, claims)
        self.assertIsNone(bad)
        self.assertIn("verified claim", issue)

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

    def test_coordinate_is_accepted_only_when_verifier_returns_exact_point_and_source(self):
        research = {"claims": [{"claim_id": "C1", "claim": "A documented event in London.", "url": "https://example.org/event", "geo_points": [
            {"name": "London", "latitude": 51.5072, "longitude": -0.1276, "source_url": "https://example.org/coordinates"},
        ]}]}
        reply = {"output_text": json.dumps({"results": [{"claim_id": "C1", "status": "verified", "verification_note": "Confirmed", "checked_sources": [
            {"title": "Record", "url": "https://example.org/event", "support": "Confirms event"},
            {"title": "Coordinates", "url": "https://example.org/coordinates", "support": "Confirms coordinates"}], "checked_geo_points": [
            {"name": "London", "latitude": 51.5072, "longitude": -0.1276, "source_url": "https://example.org/coordinates"}]}]})}
        with patch("prepost_pipeline.openai_response", return_value=reply):
            checked = verify_research_claims("Example", research)
        self.assertEqual(len(checked["verified_claims"][0]["verified_geo_points"]), 1)
        checked["claims"][0]["geo_points"][0]["latitude"] = 51.5
        with patch("prepost_pipeline.openai_response", return_value=reply):
            checked = verify_research_claims("Example", checked)
        self.assertEqual(checked["verified_claims"][0]["verified_geo_points"], [])

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
            self.assertEqual(categories, ["video", "video", "music", "sfx", "sfx"])
            self.assertEqual(manifest["assets"][0]["max_duration_seconds"], 5)
            self.assertEqual(manifest["assets"][0]["visual_mode"], "document_focus")
            self.assertEqual(manifest["assets"][0]["visual_evidence"], "A dated refinery expansion record")

    def test_video_asset_brief_separates_urls_from_search_text(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = asset_research({"narration_chunks": [{
                "section_title": "Vanguard's low-cost model",
                "visual_evidence": "Vanguard opened an investor-owned fund in 1975",
                "asset_queries": ["https://example.org/news/vanguard-fund-1975"],
            }]}, Path(temp))
            video = manifest["assets"][0]
            self.assertEqual(video["category"], "video")
            self.assertNotIn("http", video["query"])
            self.assertIn("vanguard", video["query"].lower())
            self.assertEqual(video["reference_urls"], ["https://example.org/news/vanguard-fund-1975"])
            self.assertLessEqual(len(video["query"]), 100)

    def test_package_qa_blocks_unresolved_video_and_still_masquerading_as_clip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "selected_assets.json").write_text(json.dumps({
                "assets": [{"asset_id": "a1", "category": "video", "selected": {"path": str(root / "still.png")}}],
                "unresolved": [{"asset_id": "a2", "category": "video", "query": "factory footage"}],
            }))
            (root / "still.png").write_bytes(b"still")
            issues = qa_package(root)
            self.assertTrue(any("non-video file" in issue for issue in issues))
            self.assertTrue(any("unresolved" in issue and "factory footage" in issue for issue in issues))

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
