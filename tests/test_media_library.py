import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from media_library import MediaLibrary, _is_cc0_license


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


if __name__ == "__main__":
    unittest.main()
