import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import social_video


class SocialVideoTests(unittest.TestCase):
    def test_supported_social_url_accepts_canonical_public_video_pages_only(self):
        self.assertTrue(social_video.supported_social_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(social_video.supported_social_url("https://youtu.be/abc"))
        self.assertTrue(social_video.supported_social_url("https://www.tiktok.com/@creator/video/123"))
        self.assertFalse(social_video.supported_social_url("file:///tmp/video.mp4"))
        self.assertFalse(social_video.supported_social_url("https://example.com/video"))
        self.assertFalse(social_video.supported_social_url("https://user:pass@youtube.com/watch?v=abc"))

    def test_tiktok_search_response_is_reduced_to_real_canonical_video_links(self):
        response = {"output_text": json.dumps({"videos": [
            {"url": "https://www.tiktok.com/@maker/video/123?is_from_webapp=1", "title": "Factory clip", "reason": "Relevant scene"},
            {"url": "https://example.com/@maker/video/555", "title": "bad"},
        ]})}
        rows = social_video._extract_tiktok_urls(response, 3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://www.tiktok.com/@maker/video/123")

    def test_youtube_candidate_search_uses_official_api_and_returns_video_urls(self):
        response = MagicMock()
        response.json.return_value = {"items": [{
            "id": {"videoId": "v123"}, "snippet": {"title": "Factory history", "channelTitle": "Archive", "description": "Footage"}
        }]}
        response.raise_for_status.return_value = None
        with patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}), patch("social_video._request_session") as client:
            client.return_value.get.return_value = response
            candidates = social_video.search_youtube_videos("old factory machinery", limit=2)
        self.assertEqual(candidates[0]["url"], "https://www.youtube.com/watch?v=v123")
        self.assertEqual(candidates[0]["source"], "youtube")
        self.assertEqual(client.return_value.get.call_args.kwargs["params"]["key"], "test-key")

    def test_ai_visual_selector_sends_contact_sheet_and_returns_valid_center(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "source.mp4"
            video.write_bytes(b"source")
            sheet = Path(temp) / "sheet.jpg"
            sheet.write_bytes(b"frame-sheet")
            response = MagicMock()
            response.json.return_value = {"output_text": json.dumps({
                "suitable": True, "center_second": 7.2, "reason": "The machinery is visible and active"
            })}
            response.raise_for_status.return_value = None
            def fake_sheet(_video, _duration, output, _frames=12):
                Path(output).write_bytes(b"contact sheet")
                return [2.0, 7.0, 12.0]
            with patch.object(social_video, "_probe_duration", return_value=20.0), \
                    patch.object(social_video, "_contact_sheet", side_effect=fake_sheet), \
                    patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
                    patch("social_video._request_session") as client:
                client.return_value.post.return_value = response
                selected = social_video.analyze_clip_moment(video, query="working factory machinery", purpose="Show motion")
            self.assertEqual(selected["center_second"], 7.2)
            payload = client.return_value.post.call_args.kwargs["json"]
            image_part = payload["input"][0]["content"][1]
            self.assertTrue(image_part["image_url"].startswith("data:image/jpeg;base64,"))

    def test_download_and_cut_keeps_only_muted_five_second_excerpt(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "clip.mp4"

            def fake_run(command, **kwargs):
                result = MagicMock(returncode=0, stdout="", stderr="")
                if "-m" in command and "yt_dlp" in command:
                    template = Path(command[command.index("-o") + 1])
                    source = template.parent / "source.mp4"
                    source.write_bytes(b"full temporary source")
                elif "-i" in command and str(output) == command[-1]:
                    output.write_bytes(b"selected excerpt")
                return result

            moment = {"center_second": 8.0, "duration_seconds": 20.0,
                      "reason": "Relevant visual", "sampled_frame_times": [2, 7, 12]}
            candidate = {"source": "youtube", "url": "https://www.youtube.com/watch?v=example", "title": "Factory"}
            with patch.object(social_video.subprocess, "run", side_effect=fake_run), \
                    patch.object(social_video, "analyze_clip_moment", return_value=moment):
                details = social_video.download_and_cut(candidate, query="factory machinery", purpose="Show activity", output=output)
            self.assertTrue(output.exists())
            self.assertEqual(details["segment_start_seconds"], 5.5)
            self.assertEqual(details["segment_end_seconds"], 10.5)
            self.assertFalse(details["source_audio_included"])

    def test_download_rejects_non_social_urls_before_running_downloader(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(social_video.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                social_video.download_and_cut({"url": "https://example.com/video.mp4"}, query="x", purpose="y", output=Path(temp) / "clip.mp4")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
