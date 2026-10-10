import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from azure_tts import build_ssml, build_timed_edit_job, emotion_to_style, words_to_srt


class AzureTtsTests(unittest.TestCase):
    def test_emotion_tags_map_to_supported_expressive_styles(self):
        self.assertEqual(emotion_to_style("fearful curiosity"), "fearful")
        self.assertEqual(emotion_to_style("intrigued", "dramatic_realization"), "hopeful")
        self.assertEqual(emotion_to_style("suspense"), "softvoice")

    def test_ssml_escapes_text_and_adds_emotional_style_only_for_mai_voice(self):
        expressive = build_ssml("A & B", voice="en-US-Ethan:MAI-Voice-2.1", emotion="fearful", pace="slow")
        standard = build_ssml("A & B", voice="en-US-GuyNeural", emotion="fearful", pace="slow")
        self.assertIn('style="fearful"', expressive)
        female_expressive = build_ssml("A & B", voice="en-US-Olivia:MAI-Voice-2.1", emotion="hopeful")
        self.assertIn('style="hopeful"', female_expressive)
        self.assertIn("A &amp; B", expressive)
        self.assertNotIn("express-as", standard)
        self.assertIn('rate="-8%"', standard)

    def test_word_timestamps_build_readable_srt(self):
        text = words_to_srt([
            {"text": "The", "start": 0, "end": 0.2},
            {"text": "story", "start": 0.25, "end": 0.6},
            {"text": "changed.", "start": 0.65, "end": 1.1},
        ])
        self.assertIn("00:00:00,000 --> 00:00:01,100", text)
        self.assertIn("The story changed.", text)

    def test_timed_job_joins_script_timings_and_selected_asset_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root / "portrait.png"
            media.write_bytes(b"image")
            script_path = root / "script.json"
            script_path.write_text(json.dumps({
                "title_working": "Demo", "narration_chunks": [{"text": "A fact.", "asset_queries": ["portrait"], "tone": "suspense", "sfx_cues": []}]
            }), encoding="utf-8")
            (root / "job.json").write_text(json.dumps({"topic": "A topic"}), encoding="utf-8")
            selection_path = root / "selected_assets.json"
            selection_path.write_text(json.dumps({"assets": [{
                "query": "portrait", "category": "picture", "segment": 1,
                "selected": {"asset_id": "asset1", "path": str(media), "category": "picture", "license": "test"}
            }], "unresolved": []}), encoding="utf-8")
            timed = build_timed_edit_job(script_path, {
                "audio_file": str(root / "narration.wav"), "duration_seconds": 1.2,
                "caption_file": str(root / "captions.srt"), "word_timestamps_file": str(root / "words.json"),
                "chunks": [{"chunk_index": 0, "start": 0, "end": 1.2}],
            }, selection_path)
            self.assertEqual(timed["topic"], "A topic")
            self.assertEqual(timed["segments"][0]["asset_ids"], ["asset1"])
            self.assertEqual(timed["assets"]["asset1"]["path"], str(media))


if __name__ == "__main__":
    unittest.main()
