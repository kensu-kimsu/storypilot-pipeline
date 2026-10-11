import sys
import json
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from azure_tts import (
    _estimated_word_boundaries,
    _synthesize_fish_chunk,
    build_ssml,
    build_timed_edit_job,
    emotion_to_style,
    fish_direction_tag,
    synthesize_script,
    words_to_srt,
)


class AzureTtsTests(unittest.TestCase):
    def test_emotion_tags_map_to_supported_expressive_styles(self):
        self.assertEqual(emotion_to_style("fearful curiosity"), "fearful")
        self.assertEqual(emotion_to_style("intrigued", "dramatic_realization"), "hopeful")
        self.assertEqual(emotion_to_style("suspense"), "softvoice")

    def test_fish_tags_map_script_emotion_to_inline_direction(self):
        self.assertEqual(fish_direction_tag("fearful"), "[fearful, tense]")
        self.assertEqual(fish_direction_tag("curious"), "[curious, quietly intrigued]")
        self.assertEqual(fish_direction_tag("neutral"), "[natural, conversational]")

    def test_fish_local_request_uses_msgpack_and_returns_wav(self):
        captured = {}
        def pack_payload(payload):
            captured["payload"] = payload
            return b"msgpack"
        fake_msgpack = SimpleNamespace(packb=pack_payload)
        fake_response = Mock(ok=True, content=b"RIFF" + b"audio")
        fake_requests = SimpleNamespace(RequestException=Exception, post=Mock(return_value=fake_response))
        with patch.dict(sys.modules, {"ormsgpack": fake_msgpack, "requests": fake_requests}):
            result = _synthesize_fish_chunk(
                "The secret changed everything.", base_url="http://127.0.0.1:8080",
                emotion="suspense", tone="hushed",
            )
        self.assertTrue(result.startswith(b"RIFF"))
        self.assertEqual(captured["payload"]["format"], "wav")
        self.assertIn("[near-whisper, suspenseful]", captured["payload"]["text"])
        fake_requests.post.assert_called_once()
        self.assertEqual(fake_requests.post.call_args.args[0], "http://127.0.0.1:8080/v1/tts")

    def test_fish_word_timing_estimates_cover_the_audio_duration(self):
        words = _estimated_word_boundaries("A longer sentence.", 2.0)
        self.assertEqual(len(words), 3)
        self.assertAlmostEqual(words[0]["start"], 0)
        self.assertAlmostEqual(words[-1]["end"], 2.0)

    def test_fish_multi_chunk_script_requires_a_stable_voice_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script_path = root / "script.json"
            script_path.write_text(json.dumps({"narration_chunks": [
                {"text": "First section."}, {"text": "Second section."},
            ]}), encoding="utf-8")
            fake_requests = SimpleNamespace(get=Mock(return_value=Mock(ok=True)))
            with patch.dict(sys.modules, {"requests": fake_requests}), patch.dict(
                "os.environ", {"FISH_SPEECH_REFERENCE_ID": "", "FISH_SPEECH_REFERENCE_AUDIO": "",
                               "FISH_SPEECH_REFERENCE_TEXT_FILE": "", "FISH_SPEECH_ALLOW_RANDOM_VOICE": ""}
            ), self.assertRaisesRegex(RuntimeError, "consistent narrator"):
                synthesize_script(script_path, root / "out", provider="fish")

    def test_fish_synthesis_writes_narration_manifest_and_captions(self):
        audio_io = io.BytesIO()
        with wave.open(audio_io, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(b"\0\0" * 24000)
        script = {"narration_chunks": [{"text": "A documented fact.", "delivery_emotion": "curious"}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script_path = root / "script.json"
            script_path.write_text(json.dumps(script), encoding="utf-8")
            fake_requests = SimpleNamespace(get=Mock(return_value=Mock(ok=True)))
            with patch.dict(sys.modules, {"requests": fake_requests}), patch(
                    "azure_tts._synthesize_fish_chunk", return_value=audio_io.getvalue()):
                manifest = synthesize_script(script_path, root / "out", provider="fish")
            self.assertEqual(manifest["provider"], "fish")
            self.assertEqual(manifest["word_timing_quality"], "approximate")
            self.assertTrue(Path(manifest["audio_file"]).is_file())
            self.assertIn("A documented fact.", Path(manifest["caption_file"]).read_text(encoding="utf-8"))

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
                "title_working": "Demo", "narration_chunks": [{"text": "A fact.", "claim_ids": ["C1"], "graphic_spec": {"kind": "counter"}, "asset_queries": ["portrait"], "tone": "suspense", "visual_mode": "document_focus", "visual_evidence": "A verified court record", "visual_rationale": "Show the original source", "sfx_cues": []}]
            }), encoding="utf-8")
            (root / "research.json").write_text(json.dumps({"verified_claims": [{"claim_id": "C1", "claim": "A verified fact."}]}), encoding="utf-8")
            (root / "job.json").write_text(json.dumps({"topic": "A topic", "channel_id": "@history", "channel": {"handle": "@history", "visual_brand": {"primary_font": "Montserrat"}}}), encoding="utf-8")
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
            self.assertEqual(timed["segments"][0]["visual_mode"], "document_focus")
            self.assertEqual(timed["segments"][0]["visual_evidence"], "A verified court record")
            self.assertEqual(timed["segments"][0]["claim_ids"], ["C1"])
            self.assertEqual(timed["segments"][0]["graphic_spec"], {"kind": "counter"})
            self.assertEqual(timed["verified_claims"][0]["claim_id"], "C1")
            self.assertEqual(timed["channel_id"], "@history")
            self.assertEqual(timed["visual_brand"]["primary_font"], "Montserrat")

    def test_timed_job_maps_parallax_roles_to_separate_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            portrait, backdrop = root / "portrait.png", root / "background.png"
            portrait.write_bytes(b"portrait")
            backdrop.write_bytes(b"background")
            script = root / "script.json"
            script.write_text(json.dumps({"narration_chunks": [{
                "text": "A portrait beat.", "asset_queries": [], "tone": "standard",
                "visual_mode": "parallax_portrait", "parallax_assets": {
                    "portrait_query": "archival portrait", "background_query": "smoky office"},
            }]}), encoding="utf-8")
            manifest = root / "selected_assets.json"
            manifest.write_text(json.dumps({"assets": [
                {"query": "archival portrait", "category": "picture", "segment": 1, "parallax_role": "portrait_foreground",
                 "selected": {"asset_id": "person", "path": str(portrait), "category": "picture"}},
                {"query": "smoky office", "category": "picture", "segment": 1, "parallax_role": "parallax_background",
                 "selected": {"asset_id": "scene", "path": str(backdrop), "category": "picture"}},
            ]}), encoding="utf-8")
            timed = build_timed_edit_job(script, {"audio_file": str(root / "n.wav"), "duration_seconds": 2,
                "caption_file": str(root / "c.srt"), "word_timestamps_file": str(root / "words.json"),
                "chunks": [{"chunk_index": 0, "start": 0, "end": 2}]}, manifest)
            self.assertEqual(timed["segments"][0]["parallax_foreground_asset_id"], "person")
            self.assertEqual(timed["segments"][0]["asset_ids"], ["person", "scene"])


if __name__ == "__main__":
    unittest.main()
