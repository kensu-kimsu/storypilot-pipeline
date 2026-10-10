import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from creative_director import build_edit_plan, prepare_assets_before_edit, write_fcpxml


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
