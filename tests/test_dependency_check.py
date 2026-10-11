import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dependency_check


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


if __name__ == "__main__":
    unittest.main()
