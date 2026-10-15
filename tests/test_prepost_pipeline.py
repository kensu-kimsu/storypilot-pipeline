import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from prepost_pipeline import (
    _ConsoleProgress,
    _matched_keyword_text,
    create_thumbnail_from_reference,
    _verified_news_source_for_request,
    make_metadata,
    resolve_thumbnail_reference,
    asset_research,
    fulfill_news_headline_requests,
    find_news_article,
    get_niche_config,
    is_duplicate,
    mark_job_render_status,
    qa_package,
    require_no_unfinished_jobs,
    retry_job_assets,
    search_asset_sources,
    validate_graphic_spec,
    verify_research_claims,
    write_script,
)


class PrePostPipelineTests(unittest.TestCase):
    def test_headline_screenshot_keyword_matching_allows_amount_precision(self):
        text = "VOO attracted $124.4 billion late last week, according to ETF.com."
        self.assertEqual(_matched_keyword_text(text, ["$124 Billion"]), ["$124.4 billion"])
        self.assertEqual(_matched_keyword_text(text, ["$125 Billion"]), [])

    def test_next_topic_stays_blocked_until_qa_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "jobs" / "topic-one"
            folder.mkdir(parents=True)
            (folder / "job.json").write_text(json.dumps({"job_id": "topic-one", "status": "preproduction_ready"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "previous topic is still unfinished"):
                require_no_unfinished_jobs(root)
            manifest = folder / "selected_assets.json"
            manifest.write_text("{}", encoding="utf-8")
            mark_job_render_status(folder / "timed_edit_job.json", {"status": "passed", "video_path": str(root / "final.mp4")},
                                  {"assets_manifest": str(manifest), "format": "long_form"})
            require_no_unfinished_jobs(root)

    def test_short_render_alone_does_not_unlock_next_topic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "jobs" / "topic-one"
            folder.mkdir(parents=True)
            (folder / "job.json").write_text(json.dumps({"job_id": "topic-one", "status": "preproduction_ready"}), encoding="utf-8")
            manifest = folder / "selected_assets.json"
            manifest.write_text("{}", encoding="utf-8")
            mark_job_render_status(folder / "timed_edit_job.json", {"status": "passed", "video_path": "short.mp4"},
                                  {"assets_manifest": str(manifest), "format": "short_form"})
            with self.assertRaisesRegex(RuntimeError, "previous topic is still unfinished"):
                require_no_unfinished_jobs(root)

    def test_asset_retry_only_reprocesses_unresolved_and_keeps_existing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "jobs" / "topic-one"
            folder.mkdir(parents=True)
            existing_path = root / "present.mp4"
            existing_path.write_bytes(b"already present")
            (folder / "asset_requests.json").write_text(json.dumps({"assets": [
                {"asset_id": "a1", "category": "video", "query": "existing"},
                {"asset_id": "a2", "category": "sfx", "query": "soft page flip"},
            ]}), encoding="utf-8")
            (folder / "selected_assets.json").write_text(json.dumps({
                "assets": [{"asset_id": "a1", "selected": {"path": str(existing_path)}}],
                "unresolved": [{"asset_id": "a2", "category": "sfx", "query": "soft page flip"}],
            }), encoding="utf-8")
            def fake_acquire(manifest, _library):
                self.assertEqual([row["asset_id"] for row in manifest["assets"]], ["a2"])
                return {"assets": [{"asset_id": "a2", "selected": {"path": "effect.wav"}}], "unresolved": []}
            with patch("media_library.acquire_asset_requests", side_effect=fake_acquire):
                result = retry_job_assets(folder, root)
            self.assertEqual(len(result["assets"]), 2)
            self.assertEqual(result["unresolved"], [])
            self.assertEqual(result["retry_summary"]["resolved_this_retry"], 1)

    def test_asset_retry_recaptures_requested_news_headline_screenshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "jobs" / "topic-one"
            folder.mkdir(parents=True)
            screenshot_path = root / "assets" / "screenshots" / "topic-one" / "headline.png"
            screenshot_path.parent.mkdir(parents=True)
            screenshot_path.write_bytes(b"png")
            request = {
                "asset_id": "news_headline_01", "category": "news_headline_screenshot",
                "query": "Vanguard investor story", "highlight_keywords": ["Vanguard"],
            }
            (folder / "asset_requests.json").write_text(json.dumps({"assets": [request]}), encoding="utf-8")
            (folder / "job.json").write_text(json.dumps({"topic": "Vanguard investor story"}), encoding="utf-8")
            (folder / "script.json").write_text(json.dumps({"narration_chunks": []}), encoding="utf-8")
            (folder / "selected_assets.json").write_text(json.dumps({
                "assets": [], "unresolved": [{"asset_id": "news_headline_01", "category": "news_headline_screenshot"}],
            }), encoding="utf-8")

            def fake_capture(topic, script, assets_root, *, job_id, research=None):
                self.assertEqual(topic, "Vanguard investor story")
                self.assertEqual(job_id, "topic-one")
                screenshot_request = script["narration_chunks"][0]["news_article_screenshot"]
                self.assertEqual(screenshot_request["asset_id"], "news_headline_01")
                self.assertEqual(screenshot_request["search_query"], "Vanguard investor story")
                return {"screenshots": [{
                    "asset_id": "news_headline_01", "status": "captured", "url": "https://reuters.com/story",
                    "publisher": "Reuters", "headline": "Vanguard changes investing", "image_path": str(screenshot_path),
                    "highlighted_keywords": ["Vanguard"],
                }]}

            def fake_acquire(manifest, _library):
                item = manifest["assets"][0]
                self.assertEqual(item["status"], "captured")
                self.assertEqual(item["local_path"], str(screenshot_path))
                return {"assets": [{**item, "selected": {"path": str(screenshot_path)}}], "unresolved": []}

            with patch("prepost_pipeline.fulfill_news_headline_requests", side_effect=fake_capture), \
                    patch("media_library.acquire_asset_requests", side_effect=fake_acquire):
                result = retry_job_assets(folder, root)

            self.assertEqual(result["unresolved"], [])
            source = json.loads((folder / "asset_requests.json").read_text(encoding="utf-8"))
            self.assertEqual(source["assets"][0]["status"], "captured")
            self.assertEqual(source["assets"][0]["headline"], "Vanguard changes investing")

    def test_progress_dashboard_shows_subtask_and_clears_wrapped_status(self):
        progress = _ConsoleProgress()
        progress.stop_event.set()
        progress.thread.join(timeout=0.2)
        progress.update_subtask("Downloading video from Pixabay")
        output = io.StringIO()
        with patch("prepost_pipeline.sys.stdout", output), \
                patch("prepost_pipeline.shutil.get_terminal_size", return_value=unittest.mock.Mock(columns=120)):
            progress.render()
        rendered = output.getvalue()
        self.assertIn("\x1b[2K\r", rendered)
        self.assertIn("Downloading video from Pixabay", rendered)
        self.assertLessEqual(len(rendered.replace("\x1b[2K\r", "")), 120)

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
        self.assertIn("generated_media_required", captured["prompt"])
        self.assertIn("write music_mood as a short, literal search phrase", captured["prompt"])
        self.assertIn("marker writing on paper", captured["prompt"])
        self.assertIn("electronic notification beep", captured["prompt"])
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
            self.assertIn("Freesound API", manifest["assets"][2]["suggested_sources"])
            self.assertEqual(manifest["assets"][2]["download_status"], "search_freesound_or_local_catalog")

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

    def test_news_article_discovery_uses_python_rss_not_openai(self):
        rss = b'''<?xml version="1.0" encoding="UTF-8"?>
        <rss><channel><item>
          <title>VOO Sets ETF Flow Record With $124 Billion in 2025 Inflows - ETF.com</title>
          <link>https://news.google.com/rss/articles/example</link>
          <pubDate>Tue, 23 Sep 2025 10:00:00 GMT</pubDate>
          <source url="https://www.etf.com">ETF.com</source>
        </item></channel></rss>'''
        response = unittest.mock.Mock(content=rss)
        response.raise_for_status.return_value = None
        http = unittest.mock.Mock()
        http.get.return_value = response
        with patch("prepost_pipeline.requests", object()), patch("prepost_pipeline.HTTP", http), \
                patch("prepost_pipeline.openai_response", side_effect=AssertionError("OpenAI must not be used for screenshot search")):
            result = find_news_article("Vanguard story", {
                "search_query": "ETF.com VOO sets ETF flow record $124 billion 2025 inflows September 23",
                "highlight_keywords": ["$124 Billion"],
            })
        self.assertEqual(result["headline"], "VOO Sets ETF Flow Record With $124 Billion in 2025 Inflows")
        self.assertEqual(result["publisher"], "ETF.com")
        self.assertEqual(result["search_provider"], "google_news_rss")
        self.assertEqual(http.get.call_args.args[0], "https://news.google.com/rss/search")
        self.assertEqual(http.get.call_args.kwargs["params"]["q"], "ETF.com VOO sets ETF flow record $124 billion 2025 inflows September 23")

    def test_news_screenshot_reuses_matching_verified_research_url(self):
        research = {"verified_claims": [{
            "claim_id": "C12", "verification_status": "verified",
            "claim": "VOO received $124.4 billion in inflows during 2025.",
            "source_title": "VOO Sets ETF Flow Record With $124 Billion in 2025 Inflows",
            "publisher": "ETF.com",
            "url": "https://www.etf.com/sections/features/voos-record-shattering-run-shows-no-signs-slowing",
        }]}
        request = {
            "search_query": "ETF.com VOO sets ETF flow record $124 billion 2025 inflows September 23",
            "highlight_keywords": ["$124 Billion"],
        }
        result = _verified_news_source_for_request(research, request)
        self.assertIsNotNone(result)
        self.assertEqual(result["publisher"], "ETF.com")
        self.assertEqual(result["url"], research["verified_claims"][0]["url"])

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


    def test_generated_image_permission_applies_only_to_parallax_background(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = asset_research({"narration_chunks": [{
                "visual_mode": "parallax_portrait",
                "parallax_assets": {
                    "portrait_query": "recognizable founder portrait",
                    "background_query": "abstract illustrated office",
                    "generated_media_required": True,
                },
            }]}, Path(temp))
            requests_by_role = {row["parallax_role"]: row for row in manifest["assets"]}
            self.assertFalse(requests_by_role["portrait_foreground"]["generated_media_required"])
            self.assertTrue(requests_by_role["parallax_background"]["generated_media_required"])

if __name__ == "__main__":
    unittest.main()
