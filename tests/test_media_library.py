import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from media_library import MediaLibrary, _is_cc0_license, acquire_asset_requests


class MediaLibraryTests(unittest.TestCase):
    def test_cc0_filter_accepts_only_cc0_labels(self):
        self.assertTrue(_is_cc0_license("Creative Commons 0"))
        self.assertTrue(_is_cc0_license("CC0"))
        self.assertFalse(_is_cc0_license("Attribution"))
        self.assertFalse(_is_cc0_license("Attribution NonCommercial"))

    def test_pixabay_searches_are_cached_and_capped_per_run(self):
        with tempfile.TemporaryDirectory() as temp:
            library = MediaLibrary(Path(temp) / "assets")
            response = MagicMock()
            response.json.return_value = {"hits": []}
            client = MagicMock()
            client.get.return_value = response
            with patch.dict(os.environ, {"PIXABAY_API_KEY": "test", "PIXABAY_MAX_SEARCHES_PER_RUN": "1"}), \
                    patch("media_library._require_requests", return_value=client):
                self.assertEqual(library.download_pixabay("oil refinery", "picture"), [])
                self.assertEqual(library.download_pixabay("oil refinery", "picture"), [])
                self.assertEqual(library.download_pixabay("bank vault", "picture"), [])
            self.assertEqual(client.get.call_count, 1)
            self.assertEqual(client.get.call_args.kwargs["params"]["per_page"], 3)

    def test_asset_acquisition_falls_back_after_pixabay_error(self):
        with tempfile.TemporaryDirectory() as temp:
            library = MediaLibrary(Path(temp) / "assets")
            archive_asset = {"asset_id": "abc", "path": "archive.jpg", "source": "wikimedia_commons"}
            library.find_reusable = lambda *_args, **_kwargs: []
            library.download_pixabay = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable"))
            library.download_public_archives = lambda *_args, **_kwargs: [archive_asset]
            result = acquire_asset_requests({"assets": [{"category": "picture", "query": "company headquarters"}]}, library)
            self.assertEqual(len(result["assets"]), 1)
            self.assertEqual(result["assets"][0]["selected"]["source"], "wikimedia_commons")
            self.assertEqual(result["unresolved"], [])

    def test_video_request_never_falls_back_to_generated_still(self):
        with tempfile.TemporaryDirectory() as temp:
            library = MediaLibrary(Path(temp) / "assets")
            library.find_reusable = lambda *_args, **_kwargs: []
            library.download_pixabay = lambda *_args, **_kwargs: []
            library.download_public_archives = lambda *_args, **_kwargs: []
            library.generate_image = MagicMock(return_value={"path": "fake.png"})
            result = acquire_asset_requests({"assets": [{"asset_id": "v1", "category": "video", "query": "Vanguard trading floor"}]}, library)
            self.assertEqual(result["assets"], [])
            self.assertEqual(result["unresolved"][0]["status"], "no_video_clip_match_in_configured_sources")
            library.generate_image.assert_not_called()

    def test_asset_acquisition_uses_social_candidate_and_stores_selected_timecodes(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            library = MediaLibrary(Path(temp) / "assets")
            library.find_reusable = lambda *_args, **_kwargs: []
            library.download_pixabay = MagicMock(return_value=[])
            library.download_public_archives = MagicMock(return_value=[])
            candidate = {"source": "youtube", "url": "https://www.youtube.com/watch?v=abc", "title": "Factory"}
            details = {"segment_start_seconds": 2.0, "segment_end_seconds": 7.0,
                       "duration": 5.0, "source_audio_included": False}
            library.download_social_candidate = lambda _candidate, _request: library._register(
                b"selected video", category="video", title="Factory", tags=["factory"],
                source="youtube", source_url=candidate["url"], ext=".mp4", duration=5.0, metadata=details)
            result = acquire_asset_requests({"assets": [{"asset_id": "v1", "category": "video",
                "query": "active factory machines", "purpose": "Show industrial movement",
                "max_duration_seconds": 5, "candidates": [candidate]}]}, library)
            selected = result["assets"][0]["selected"]
            self.assertEqual(selected["source"], "youtube")
            self.assertEqual(selected["metadata"]["segment_end_seconds"] - selected["metadata"]["segment_start_seconds"], 5)
            library.download_pixabay.assert_not_called()

    def test_catalog_never_reuses_png_as_video(self):
        with tempfile.TemporaryDirectory() as temp:
            library = MediaLibrary(Path(temp) / "assets")
            bad = library._register(b"not really a clip", category="video", title="Vanguard trading",
                                    tags=["Vanguard", "trading"], source="test", source_url="", ext=".png")
            self.assertEqual(library.find_reusable("Vanguard trading", category="video"), [])

    def test_categories_deduplicate_and_reuse_by_tags(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = MediaLibrary(root / "assets")
            first = library._register(b"same bytes", category="picture", title="Oil refinery", tags=["oil", "refinery"],
                source="test", source_url="https://example.test/source", ext=".jpg")
            again = library._register(b"same bytes", category="picture", title="Different title", tags=["other"],
                source="test", source_url="https://example.test/source", ext=".jpg")
            self.assertFalse(first["reused"])
            self.assertTrue(again["reused"])
            self.assertEqual(len(library.find_reusable("oil refinery", category="picture")), 1)
            self.assertTrue((root / "assets" / "pictures").is_dir())
            self.assertTrue((root / "assets" / "video_clips").is_dir())
            self.assertTrue((root / "assets" / "generated_media").is_dir())
            self.assertTrue((root / "assets" / "background_music").is_dir())
            self.assertTrue((root / "assets" / "sfx").is_dir())

    def test_public_archive_download_uses_candidate_without_provider_key(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            library = MediaLibrary(Path(temp) / "assets")
            library._public_archive_candidates = lambda *_args, **_kwargs: [{
                "source": "wikimedia_commons", "download_url": "https://upload.wikimedia.org/example.jpg",
                "source_url": "https://commons.wikimedia.org/wiki/File:Example.jpg",
                "title": "Example image", "license": "CC BY-SA 4.0", "creator": "Example author",
            }]
            response = MagicMock()
            response.content = b"public image bytes"
            response.headers = {"Content-Type": "image/jpeg"}
            response.iter_content.return_value = [b"public image bytes"]
            response.raise_for_status.return_value = None
            client = MagicMock()
            client.get.return_value = response
            with patch("media_library._require_requests", return_value=client):
                downloaded = library.download_public_archives("example company", "picture", limit=1)
            self.assertEqual(len(downloaded), 1)
            self.assertTrue(Path(downloaded[0]["path"]).exists())
            self.assertEqual(downloaded[0]["source"], "wikimedia_commons")

    def test_commons_search_normalizes_file_and_license_metadata(self):
        normalized = MediaLibrary._commons_candidates({"query": {"pages": [{
            "title": "File:Example.jpg",
            "imageinfo": [{"url": "https://upload.wikimedia.org/example.jpg", "mime": "image/jpeg",
                "extmetadata": {"Artist": {"value": "Example author"},
                                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"}}}],
        }]}}, "picture", "example")
        self.assertEqual(normalized[0]["title"], "Example.jpg")
        self.assertEqual(normalized[0]["creator"], "Example author")
        self.assertEqual(normalized[0]["license"], "CC BY-SA 4.0")

    def test_nasa_original_assets_without_extensions_are_discovered(self):
        with tempfile.TemporaryDirectory() as temp:
            library = MediaLibrary(Path(temp) / "assets")
            def cached(provider, *_args, **_kwargs):
                if provider == "nasa_images":
                    return {"collection": {"items": [{"data": [{"nasa_id": "apollo-1", "title": "Apollo image"}]}]}}
                return {"collection": {"items": [{"href": "https://images-assets.nasa.gov/image/apollo-1/apollo-1~orig"}]}}
            library._cached_public_json = cached
            candidates = library._nasa_candidates("Apollo", "picture", 1)
            self.assertEqual(candidates[0]["download_url"], "https://images-assets.nasa.gov/image/apollo-1/apollo-1~orig")


if __name__ == "__main__":
    unittest.main()
