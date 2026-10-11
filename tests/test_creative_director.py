import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from creative_director import build_edit_plan, build_package, prepare_assets_before_edit, write_fcpxml


class CreativeDirectorTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            "job_id": "demo-rockefeller-001",
            "topic": "Historical Monopoly: Rockefeller",
            "format": "long_form",
            "narration_file": "/tmp/narration.wav",
            "narration_duration": 12.0,
            "segments": [
                {"start": 0.0, "end": 4.0, "text": "He saw an opportunity.", "tone": "dramatic_realization", "audio_energy": 0.12, "asset_ids": ["portrait"]},
                {"start": 4.0, "end": 8.0, "text": "The railroad changed the economics.", "tone": "historical_context", "audio_energy": 0.55, "asset_ids": ["map"]},
                {"start": 8.0, "end": 12.0, "text": "The number was staggering.", "tone": "rapid_fire_facts", "audio_energy": 0.92, "asset_ids": ["chart"]},
            ],
            "assets": {
                "portrait": {"path": "/tmp/portrait.png", "kind": "image", "duration": 4, "rights_status": "public_domain"},
                "map": {"path": "/tmp/map.png", "kind": "map", "duration": 4, "rights_status": "licensed"},
                "chart": {"path": "/tmp/chart.png", "kind": "chart", "duration": 4, "rights_status": "owned"},
            },
        }

    def test_seeded_edit_plan_is_reproducible_and_varies_by_tone(self):
        a = build_edit_plan(self.job)
        b = build_edit_plan(self.job)
        self.assertEqual(a, b)
        modes = {shot["visual_mode"] for shot in a["shots"]}
        self.assertGreaterEqual(len(modes), 2)
        self.assertEqual(a["palette"]["accent"], "#FFD700")
        self.assertEqual(a["workflow"], "assistant_editor_handoff")
        self.assertTrue(a["human_polish_targets"])

    def test_asset_presence_is_not_rejected_for_missing_rights_label(self):
        self.job["assets"]["portrait"]["rights_status"] = "unknown"
        plan = build_edit_plan(self.job)
        self.assertTrue(any(shot["asset_id"] == "portrait" for shot in plan["shots"]))

    def test_parallax_scene_uses_portrait_cutout_over_distinct_background_asset(self):
        self.job["assets"]["backdrop"] = {"path": "/tmp/backdrop.png", "kind": "image"}
        self.job["segments"] = [{
            "start": 0.0, "end": 4.0, "text": "The portrait tells the story.", "tone": "dramatic_realization",
            "visual_mode": "parallax_portrait", "asset_ids": ["portrait", "backdrop"],
            "parallax_foreground_asset_id": "portrait",
        }]
        self.job["narration_duration"] = 4.0
        plan = build_edit_plan(self.job)
        self.assertTrue(plan["shots"])
        self.assertTrue(all(shot["asset_id"] == "backdrop" for shot in plan["shots"]))
        self.assertTrue(all(shot["parallax_foreground_asset_id"] == "portrait" for shot in plan["shots"]))

    def test_scene_directed_visual_and_channel_brand_are_preserved(self):
        self.job["visual_brand"] = {
            "primary_font": "Aptos Display", "brand_accent": "#12ABCD",
            "caption_position": "lower_third", "caption_positions": ["lower_third", "top_safe"],
        }
        self.job["segments"][0].update({
            "visual_mode": "document_focus",
            "visual_evidence": "The 1911 Supreme Court dissolution order",
            "visual_rationale": "The legal document makes the outcome tangible",
        })
        plan = build_edit_plan(self.job)
        first = next(shot for shot in plan["shots"] if shot["segment_index"] == 0)
        self.assertEqual(first["visual_mode"], "document_focus")
        self.assertEqual(first["visual_evidence"], "The 1911 Supreme Court dissolution order")
        self.assertEqual(first["caption_position"], "top_safe")
        self.assertEqual(plan["visual_brand"]["primary_font"], "Aptos Display")
        self.assertEqual(plan["visual_brand"]["brand_accent"], "#12ABCD")
        self.assertEqual(first["style_selection"], "scene_directed")

    def test_recent_channel_style_history_discourages_repeating_modes(self):
        self.job["channel"] = {"handle": "@historychannel"}
        self.job["segments"] = [
            {"start": 0.0, "end": 12.0, "text": "A story beat.", "tone": "standard", "audio_energy": 0.5, "asset_ids": ["portrait"]}
        ]
        plan = build_edit_plan(self.job, [{
            "channel_key": "@historychannel",
            "signature": {"dominant_modes": ["cinematic_broll"]},
        }])
        self.assertNotEqual(plan["shots"][0]["visual_mode"], "cinematic_broll")
        self.assertEqual(plan["episode_style"]["recent_mode_counts"]["cinematic_broll"], 1)

    def test_type_treatment_and_caption_zone_follow_layout(self):
        self.job["segments"] = [
            {"start": 0.0, "end": 12.0, "text": "A story beat.", "tone": "dramatic_realization", "visual_mode": "minimalist_text", "asset_ids": []}
        ]
        plan = build_edit_plan(self.job)
        self.assertTrue(all(shot["caption_position"] == "center_emphasis" for shot in plan["shots"]))
        self.assertTrue(all(shot["type_treatment"] == "single_statement" for shot in plan["shots"]))

    def test_build_package_persists_one_style_history_row_per_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job_dir = root / "jobs" / "episode-one"
            job_dir.mkdir(parents=True)
            narration = job_dir / "narration.wav"
            narration.write_bytes(b"audio")
            captions = job_dir / "captions.srt"
            captions.write_text("1\\n00:00:00,000 --> 00:00:04,000\\nA story beat.\\n", encoding="utf-8")
            job = {
                "job_id": "episode-one", "channel_id": "@history", "topic": "A sourced story",
                "narration_file": str(narration), "narration_duration": 4.0,
                "captions_required": True, "captions_file": str(captions),
                "segments": [{"start": 0, "end": 4, "text": "A story beat.", "tone": "standard", "asset_ids": []}],
                "assets": {},
            }
            job_path = job_dir / "timed_edit_job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            output = job_dir / "edit_handoff"
            build_package(job_path, output)
            history_path = root / "style_history.json"
            saved = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["videos"]), 1)
            self.assertEqual(saved["videos"][0]["channel_key"], "@history")
            build_package(job_path, output)
            saved = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["videos"]), 1)

    def test_fcpxml_is_well_formed_and_contains_storyline_and_shots(self):
        plan = build_edit_plan(self.job)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "edit.fcpxml"
            write_fcpxml(path, plan, self.job["assets"])
            tree = ET.parse(path)
            self.assertEqual(tree.getroot().tag, "fcpxml")
            self.assertEqual(tree.getroot().attrib["version"], "1.10")
            clips = tree.findall(".//asset-clip")
            self.assertGreaterEqual(len(clips), 2)
            self.assertTrue(any("Narration" in clip.attrib.get("name", "") for clip in clips))

    def test_invalid_segment_time_fails_closed(self):
        self.job["segments"][1]["start"] = 9
        with self.assertRaises(ValueError):
            build_edit_plan(self.job)

    def test_pre_edit_asset_check_writes_blocked_report_for_missing_visual(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = {"job_id": "preflight", "assets": {}, "segments": [
                {"asset_queries": ["Rockefeller portrait"], "asset_ids": []}
            ]}
            source = root / "timed_edit_job.json"
            source.write_text(json.dumps(job), encoding="utf-8")
            report = root / "edit_readiness.json"
            with self.assertRaisesRegex(RuntimeError, "Pre-edit asset check blocked"):
                prepare_assets_before_edit(job, source, report)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["items"][0]["query"], "Rockefeller portrait")


if __name__ == "__main__":
    unittest.main()
