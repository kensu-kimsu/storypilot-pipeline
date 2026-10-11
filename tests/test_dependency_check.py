import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dependency_check
from unittest.mock import patch


class DependencyCheckTests(unittest.TestCase):
    def test_env_file_is_read_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / ".env").write_text("# comment\nOPENAI_API_KEY=secret-value\nNARRATION_PROVIDER=fish\n", encoding="utf-8")
            values = dependency_check._load_env_file(base)
            self.assertEqual(values["OPENAI_API_KEY"], "secret-value")
            self.assertEqual(values["NARRATION_PROVIDER"], "fish")

    def test_process_environment_takes_precedence_over_secret_sources(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "from-process"}, clear=True):
            self.assertTrue(dependency_check._configured(
                "OPENAI_API_KEY", {"OPENAI_API_KEY": "from-file"}, {"OPENAI_API_KEY": "from-dotenv"}
            ))

    def test_feature_names_include_supported_optional_components(self):
        self.assertEqual(set(dependency_check.FEATURE_REQUIREMENTS), {"maps", "ocr", "portraits", "news"})

    def test_full_workflow_setup_requests_required_keys_once(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            args = type("Args", (), {
                "stage": "all", "topic_provided": False, "base_dir": Path(temp)
            })()
            with patch("workspace_setup.configure_api_keys") as configure:
                dependency_check._configure_credentials(args)
            configure.assert_called_once_with(
                Path(temp).resolve(),
                required_keys=("OPENAI_API_KEY", "YOUTUBE_API_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"),
            )

    def test_render_only_check_does_not_prompt_for_api_keys(self):
        args = type("Args", (), {"stage": "edit", "topic_provided": False, "base_dir": Path(".")})()
        with patch("workspace_setup.configure_api_keys") as configure:
            dependency_check._configure_credentials(args)
        configure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
