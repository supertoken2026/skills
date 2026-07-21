import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "supertoken-gpt-image-2"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import supertoken_config as config  # noqa: E402


class SupertokenConfigTests(unittest.TestCase):
    def test_build_config_uses_supertoken_defaults(self):
        value = config.build_config()

        self.assertEqual(value["base_url"], "https://api.supertoken.cc/image-wrapper/v1")
        self.assertEqual(value["model"], "gpt-image-2-count")
        self.assertEqual(set(value), {"base_url", "model"})

    def test_build_config_trims_trailing_slash(self):
        value = config.build_config(base_url="https://example.test/v1/")

        self.assertEqual(value["base_url"], "https://example.test/v1")

    def test_config_dir_uses_environment_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {config.CONFIG_DIR_ENV: temp_dir}):
                self.assertEqual(config.config_dir(), Path(temp_dir))

    def test_get_api_key_prefers_environment_over_secure_store(self):
        with patch.dict(os.environ, {config.API_KEY_ENV: "env-secret"}):
            with patch.object(config, "_macos_read_key") as secure_read:
                self.assertEqual(config.get_api_key(), "env-secret")
                secure_read.assert_not_called()

    def test_save_api_key_rejects_empty_value(self):
        with self.assertRaisesRegex(config.ConfigError, "API Key"):
            config.save_api_key("")

    def test_plaintext_fallback_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.DISABLE_SECURE_STORE_ENV: "1",
            }
            with patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(config.ConfigError, "明文"):
                    config.save_api_key("test-secret", allow_plaintext=False)
                backend = config.save_api_key("test-secret", allow_plaintext=True)

            self.assertEqual(backend, "plaintext-fallback")
            credential_path = Path(temp_dir) / "credentials.json"
            self.assertEqual(json.loads(credential_path.read_text())["api_key"], "test-secret")
            if os.name == "posix":
                mode = stat.S_IMODE(credential_path.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_linux_without_secret_tool_returns_no_secure_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.API_KEY_ENV: "",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(config.platform, "system", return_value="Linux"):
                    with patch.object(config.shutil, "which", return_value=None):
                        self.assertIsNone(config.get_api_key())


if __name__ == "__main__":
    unittest.main()
