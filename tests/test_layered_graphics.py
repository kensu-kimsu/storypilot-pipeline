import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motion_graphics import highlight_document_text, prepare_parallax_cutout, render_overlay_sequence


class LayeredGraphicsTests(unittest.TestCase):
    def test_ocr_highlight_uses_located_word_boxes(self):
        fake = types.ModuleType("pytesseract")
        fake.Output = types.SimpleNamespace(DICT="dict")
        fake.image_to_data = lambda image, lang, output_type: {
            "text": ["Revenue", "rose"], "left": [10, 55], "top": [20, 20],
            "width": [40, 30], "height": [18, 18],
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"pytesseract": fake}):
            root = Path(directory)
            source = root / "document.png"
            out = root / "highlight.png"
            Image.new("RGB", (180, 80), "white").save(source)
            highlight_document_text(source, "Revenue rose", out)
            with Image.open(out) as image:
                self.assertEqual(image.mode, "RGBA")
                self.assertGreater(image.getpixel((30, 28))[0], 245)
                self.assertGreater(image.getpixel((30, 28))[1], 190)

    def test_rembg_cutout_is_cached_as_png(self):
        fake = types.ModuleType("rembg")
        fake.remove = lambda image: image
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"rembg": fake}):
            root = Path(directory)
            source = root / "portrait.jpg"
            output = root / "portrait_cutout.png"
            Image.new("RGB", (40, 60), "blue").save(source)
            prepare_parallax_cutout(source, output)
            self.assertTrue(output.is_file())
            with Image.open(output) as image:
                self.assertEqual(image.mode, "RGBA")

    def test_geographic_map_fails_clearly_without_optional_cartopy(self):
        if __import__("importlib").util.find_spec("cartopy"):
            self.skipTest("Cartopy is installed in this environment")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "requirements-maps.txt"):
                render_overlay_sequence({"kind": "geographic_map", "points": [
                    {"name": "A", "latitude": 10, "longitude": 10},
                    {"name": "B", "latitude": 20, "longitude": 20},
                ]}, Path(directory), width=320, height=180, fps=1, duration=1)


if __name__ == "__main__":
    unittest.main()
