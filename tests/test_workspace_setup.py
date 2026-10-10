import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_setup import OPTIONAL_API_KEYS, configure_api_keys, ensure_workspace


class WorkspaceSetupTests(unittest.TestCase):
    def test_only_user_provided_api_keys_are_prompted(self):
        self.assertEqual(set(OPTIONAL_API_KEYS), {
            "YOUTUBE_API_KEY", "PIXABAY_API_KEY", "FREESOUND_API_KEY",
            "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION",
        })

    def test_workspace_directories_are_created(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = ensure_workspace(Path(temp) / "production")
            expected = {
                "discovery", "jobs", "pictures", "video_clips", "generated_media",
                "background_music", "sfx", "pixabay_search_cache", "public_search_cache", "rendered_outputs",
                "temp_assets", "logs", "audio_library_music_seed", "audio_library_sfx_seed",
            }
            self.assertTrue(all(paths[name].is_dir() for name in expected))

    def test_missing_keys_are_prompted_once_then_loaded_from_secret_file(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            base = Path(temp) / "production"
            answers = iter(["openai-test-key", "", "", "", "", "", ""])
            status = configure_api_keys(
                base,
                required_keys=("OPENAI_API_KEY",),
                interactive=True,
                prompt_fn=lambda _prompt: next(answers),
            )
            secrets = json.loads(status["secrets_file"].read_text(encoding="utf-8"))
            self.assertEqual(secrets["OPENAI_API_KEY"], "openai-test-key")
            self.assertTrue(secrets["_optional_setup_completed"])
            self.assertIn("OPENAI_API_KEY", status["configured"])

            os.environ.pop("OPENAI_API_KEY")
            later = configure_api_keys(
                base,
                required_keys=("OPENAI_API_KEY",),
                interactive=True,
                prompt_fn=lambda _prompt: self.fail("Should not prompt again"),
            )
            self.assertEqual(os.environ["OPENAI_API_KEY"], "openai-test-key")
            self.assertEqual(later["secrets_file"], status["secrets_file"])

    def test_noninteractive_missing_required_key_fails_with_setup_instructions(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            base = Path(temp) / "production"
            with self.assertRaisesRegex(RuntimeError, "Run this command in a terminal"):
                configure_api_keys(base, required_keys=("OPENAI_API_KEY",), interactive=False)
            self.assertTrue((base / "assets" / "sfx").is_dir())


if __name__ == "__main__":
    unittest.main()
