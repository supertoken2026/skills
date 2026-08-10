import contextlib
import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "supertoken-video-generation" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import supertoken_video_config as config


def load_video_setup():
    spec = importlib.util.spec_from_file_location(
        "supertoken_video_setup", SCRIPTS / "setup.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


setup = load_video_setup()


class VideoSetupTests(unittest.TestCase):
    def run_setup(self, argv, prompts, config_dir):
        stdout = io.StringIO()
        stderr = io.StringIO()
        environment = {
            config.CONFIG_DIR_ENV: str(config_dir),
            config.MODEL_KEY_ENV: "",
            config.RESOURCE_KEY_ENV: "",
        }
        with patch.dict(os.environ, environment, clear=False), \
             patch.object(setup.getpass, "getpass", side_effect=prompts) as hidden, \
             contextlib.redirect_stdout(stdout), \
             contextlib.redirect_stderr(stderr):
            code = setup.main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), hidden

    def test_model_only_uses_hidden_prompt_and_persists_only_model_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_key = "sk_model_test"
            code, stdout, stderr, hidden = self.run_setup([], [model_key], temp_dir)

            self.assertEqual(code, 0, stderr)
            hidden.assert_called_once_with("SuperToken model Token: ")
            self.assertNotIn(model_key, stdout + stderr)
            with patch.dict(os.environ, {config.CONFIG_DIR_ENV: temp_dir}, clear=True):
                self.assertEqual(config.get_model_key(), model_key)
                with self.assertRaises(config.ConfigError):
                    config.get_resource_key()

    def test_with_resource_key_prompts_twice_and_persists_both_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_key = "sk_model_test"
            resource_key = "ak_resource_test"
            code, stdout, stderr, hidden = self.run_setup(
                ["--with-resource-key"], [model_key, resource_key], temp_dir
            )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(
                hidden.call_args_list,
                [
                    (("SuperToken model Token: ",), {}),
                    (("SuperToken resource Key: ",), {}),
                ],
            )
            self.assertNotIn(model_key, stdout + stderr)
            self.assertNotIn(resource_key, stdout + stderr)
            with patch.dict(os.environ, {config.CONFIG_DIR_ENV: temp_dir}, clear=True):
                self.assertEqual(config.get_model_key(), model_key)
                self.assertEqual(config.get_resource_key(), resource_key)

    def test_rejects_key_argv_unknown_args_and_url_or_wrong_type_without_leakage(self):
        secret = "sk_argv_secret"
        stderr = io.StringIO()
        for option in ("--api-key", "--unknown-option"):
            with self.subTest(option=option):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as captured:
                    setup.main([option, secret])
                self.assertEqual(captured.exception.code, 2)
                self.assertNotIn(secret, stderr.getvalue())

        with tempfile.TemporaryDirectory() as temp_dir:
            for value in ("https://invalid.example/key", "ak_wrong_type"):
                with self.subTest(value=value):
                    code, stdout, error, _hidden = self.run_setup([], [value], temp_dir)
                    self.assertEqual(code, 2)
                    self.assertNotIn(value, stdout + error)

    @unittest.skipUnless(os.name == "posix", "POSIX permission checks")
    def test_posix_credential_fallback_is_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            code, _stdout, stderr, _hidden = self.run_setup(
                [], ["sk_private_test"], temp_dir
            )
            self.assertEqual(code, 0, stderr)
            directory = Path(temp_dir)
            credentials = directory / "credentials"
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(credentials.stat().st_mode), 0o600)
