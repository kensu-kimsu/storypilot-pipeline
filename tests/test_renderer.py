import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from PIL import Image

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from renderer import quality_assurance, render_final_video
from motion_graphics import render_overlay_sequence


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "job_id": "qa-test", "format": "preview", "duration": 2.0,
            "canvas": {"width": 320, "height": 180, "fps": 24},
            "shots": [
                {"asset_id": "a", "visual_mode": "minimalist_text"},
                {"asset_id": "b", "visual_mode": "cinematic_broll"},
            ],
        }

    def test_qa_reports_container_stream_canvas_duration_and_diversity(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "sample.mp4"
            output.write_bytes(b"x" * 100_000)
            probe = {
                "streams": [
                    {"codec_type": "video", "width": 320, "height": 180},
                    {"codec_type": "audio"},
                ],
                "format": {"duration": "2.0"},
            }
            with patch("renderer._probe", return_value=probe):
                result = quality_assurance(output, self.plan)
            self.assertEqual(result["status"], "passed")
            self.assertFalse(result["upload_performed"])
            self.assertTrue(all(check["passed"] for check in result["checks"]))

    def test_qa_fails_when_long_video_is_under_twenty_minutes(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "sample.mp4"
            output.write_bytes(b"x" * 2_000_000)
            plan = dict(self.plan, format="long_form", duration=1200.0)
            probe = {
                "streams": [
                    {"codec_type": "video", "width": 320, "height": 180},
                    {"codec_type": "audio"},
                ],
                "format": {"duration": "1199.0"},
            }
            with patch("renderer._probe", return_value=probe):
                result = quality_assurance(output, plan)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("under 20 minutes" in error for error in result["errors"]))

    def test_graphic_templates_write_transparent_animated_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for kind, spec in (
                ("chart", {"kind": "chart", "title": "Revenue", "items": [
                    {"label": "2020", "value": "2 billion", "numeric_value": 2_000_000_000},
                    {"label": "2021", "value": "3 billion", "numeric_value": 3_000_000_000},
                ]}),
                ("route", {"kind": "route_schematic", "title": "The journey", "places": [
                    {"name": "London"}, {"name": "New York"}, {"name": "Boston"},
                ]}),
            ):
                pattern = render_overlay_sequence(spec, root / kind, width=320, height=180, fps=12, duration=1.0)
                frames = sorted((root / kind).glob("graphic_*.png"))
                self.assertEqual(len(frames), 12)
                with Image.open(pattern.replace("%05d", "00011")) as frame:
                    self.assertEqual(frame.mode, "RGBA")
                    self.assertEqual(frame.size, (320, 180))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not installed")
    def test_local_render_smoke_burns_captions_and_writes_qa_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            narration = root / "narration.wav"
            with wave.open(str(narration), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24_000)
                audio.writeframes(b"\0\0" * 48_000)
            captions = root / "captions.srt"
            captions.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nFIRST CAPTION\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nSECOND CAPTION\n",
                encoding="utf-8",
            )
            job_path = root / "job.json"
            job_path.write_text(json.dumps({
                "job_id": "render-smoke", "captions_file": str(captions),
                "captions_required": True, "assets": {},
                "verified_claims": [{"claim_id": "C1", "claim": "Revenue reached 2 billion in 2020 and 3 billion in 2021."}],
            }), encoding="utf-8")
            plan = {
                "job_id": "render-smoke", "format": "preview", "duration": 2.0,
                "canvas": {"width": 320, "height": 180, "fps": 24},
                "narration_file": str(narration),
                "visual_brand": {"primary_font": "Arial", "brand_accent": "#FFD34E"},
                "palette": {"background": "#080A0F", "accent": "#FFD34E"},
                "shots": [
                    {"start": 0.0, "end": 1.0, "duration": 1.0, "segment_index": 0,
                     "asset_id": None, "visual_mode": "animated_chart", "claim_ids": ["C1"],
                     "graphic_spec": {"kind": "chart", "title": "Reported revenue", "items": [
                         {"label": "2020", "value": "2 billion", "claim_id": "C1"},
                         {"label": "2021", "value": "3 billion", "claim_id": "C1"},
                     ]}},
                    {"start": 1.0, "end": 2.0, "duration": 1.0, "segment_index": 1,
                     "asset_id": None, "visual_mode": "cinematic_broll", "overlay_text": "TWO",
                     "transition_out": "short_dissolve"},
                ],
            }
            result = render_final_video(job_path, root / "rendered", plan, encoder="cpu")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["motion_graphics"][0]["kind"], "chart")
            self.assertTrue(Path(result["video_path"]).is_file())
            self.assertTrue((root / "rendered" / "render-smoke_preview_qa.json").is_file())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not installed")
    def test_local_render_composites_cutout_over_separate_background(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            narration = root / "narration.wav"
            with wave.open(str(narration), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24_000)
                audio.writeframes(b"\0\0" * 48_000)
            backdrop = root / "backdrop.png"
            portrait = root / "portrait.png"
            Image.new("RGB", (320, 180), (25, 45, 65)).save(backdrop)
            Image.new("RGBA", (160, 160), (220, 180, 120, 180)).save(portrait)
            job_path = root / "job.json"
            job_path.write_text(json.dumps({
                "job_id": "parallax-smoke", "captions_required": False,
                "assets": {"background": {"path": str(backdrop), "kind": "image"}, "subject": {"path": str(portrait), "kind": "image"}},
            }), encoding="utf-8")
            plan = {
                "job_id": "parallax-smoke", "format": "preview", "duration": 2,
                "canvas": {"width": 320, "height": 180, "fps": 24}, "narration_file": str(narration),
                "visual_brand": {}, "palette": {"background": "#080A0F", "accent": "#FFD34E"},
                "shots": [{"start": 0, "end": 2, "duration": 2, "segment_index": 0,
                    "asset_id": "background", "parallax_foreground_asset_id": "subject", "visual_mode": "parallax_portrait"}],
            }
            def copy_cutout(source, output):
                with Image.open(source) as im:
                    im.convert("RGBA").save(output)
                return output
            with patch("renderer.prepare_parallax_cutout", side_effect=copy_cutout):
                result = render_final_video(job_path, root / "rendered", plan, encoder="cpu")
            self.assertEqual(result["status"], "passed")
            self.assertIn("parallax_portrait", [item["kind"] for item in result["motion_graphics"]])


if __name__ == "__main__":
    unittest.main()
