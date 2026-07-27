import base64
import contextlib
import io
import json
import os
import stat
import subprocess
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
import generate_image as generator  # noqa: E402
import setup as setup_script  # noqa: E402
import supertoken_api as api  # noqa: E402
import supertoken_image as cli  # noqa: E402


class SupertokenConfigTests(unittest.TestCase):
    def test_build_config_uses_v2_defaults(self):
        self.assertEqual(
            config.build_config(),
            {
                "version": 2,
                "base_url": "https://api.supertoken.cc",
                "model": "gpt-image-2-count",
            },
        )

    def test_build_config_trims_trailing_slash(self):
        value = config.build_config(base_url="https://example.test/v1/")

        self.assertEqual(value["base_url"], "https://example.test/v1")

    def test_build_config_normalizes_clean_https_bases(self):
        cases = {
            "  https://EXAMPLE.TEST:443/v1/\t": "https://example.test/v1",
            "https://127.0.0.1:8443/api": "https://127.0.0.1:8443/api",
            "https://[2001:DB8::1]:443/v1": "https://[2001:db8::1]/v1",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(config.build_config(raw)["base_url"], expected)

    def test_build_config_rejects_unclean_or_non_https_bases(self):
        invalid = [
            "http://example.test",
            "//example.test/v1",
            "/v1",
            "https:///v1",
            "https://user:pass@example.test/v1",
            "https://example.test/v1?token=secret",
            "https://example.test/v1#fragment",
            "https://example.test:0/v1",
            "https://example.test:65536/v1",
            "https://example.test:bad/v1",
            "https://example.test/a//b",
            "https://example.test/a/../b",
            "https://example.test/a/%2e%2e/b",
            "https://example.test/a/%2fb",
            "https://example.test/a/%5cb",
            "https://example.test/a%20b",
            "https://example.test/%00v1",
            "https://example.test/%7fv1",
            "https://example.test\\v1",
            "https://example.test:/v1",
            "https://example.test/a b",
            "https://example.test/\u63a5\u53e3",
            "https://example.test/\x00v1",
            "https://[2001:db8::1/v1",
        ]
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(config.ConfigError, "base_url"):
                    config.build_config(value)

    def test_config_dir_uses_environment_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {config.CONFIG_DIR_ENV: temp_dir}):
                self.assertEqual(config.config_dir(), Path(temp_dir))

    def test_get_api_key_prefers_environment_over_secure_store(self):
        with patch.dict(os.environ, {config.API_KEY_ENV: "env-secret"}):
            with patch.object(config, "_macos_read_key") as secure_read:
                self.assertEqual(config.get_api_key(), "env-secret")
                secure_read.assert_not_called()

    def test_api_keys_are_trimmed_before_type_validation(self):
        self.assertEqual(
            config.validate_api_key("  sk-model123456  ", config.MODEL_KEY),
            "sk-model123456",
        )
        with self.assertRaisesRegex(config.ConfigError, "资源 API Key"):
            config.validate_api_key("  ak_resource123456  ", config.MODEL_KEY)

    def test_api_keys_reject_control_characters_without_echoing_the_value(self):
        for value in (
            "secret\nvalue", "secret\x00value", "secret\x7fvalue",
            "sk-model123456\n", "\tsk-model123456",
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(config.ConfigError) as raised:
                    config.validate_api_key(value)
                self.assertIn("控制字符", str(raised.exception))
                self.assertNotIn(value, str(raised.exception))

    def test_environment_and_plaintext_keys_return_normalized_values(self):
        with patch.dict(os.environ, {config.API_KEY_ENV: "  env-secret  "}):
            self.assertEqual(config.get_api_key(), "env-secret")

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "credentials.json").write_text(
                json.dumps({"api_key": "  stored-secret  "}), encoding="utf-8"
            )
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.API_KEY_ENV: "",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(config.platform, "system", return_value="Linux"):
                    with patch.object(config.shutil, "which", return_value=None):
                        self.assertEqual(config.get_api_key(), "stored-secret")

    def test_save_api_key_rejects_empty_value(self):
        with self.assertRaisesRegex(config.ConfigError, "Key"):
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

    @unittest.skipUnless(os.name == "posix", "POSIX permission behavior")
    def test_plaintext_credentials_use_private_unique_temp_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "config"
            config_dir.mkdir(mode=0o755)
            config_dir.chmod(0o755)
            credential_path = config_dir / "credentials.json"
            stale_part = Path(f"{credential_path}.part")
            stale_part.write_text("leave this alone", encoding="utf-8")
            observed_parts = []
            real_replace = os.replace

            def inspect_replace(source, destination):
                source = Path(source)
                destination = Path(destination)
                if destination == credential_path:
                    observed_parts.append(source)
                    self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), 0o700)
                    self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o600)
                return real_replace(source, destination)

            environment = {
                config.CONFIG_DIR_ENV: str(config_dir),
                config.DISABLE_SECURE_STORE_ENV: "1",
            }
            previous_umask = os.umask(0o022)
            try:
                with patch.dict(os.environ, environment, clear=False):
                    with patch.object(config.os, "replace", side_effect=inspect_replace):
                        config.save_api_key("model-secret", allow_plaintext=True)
                        config.save_api_key(
                            "resource-secret", allow_plaintext=True,
                            kind=config.RESOURCE_KEY,
                        )
            finally:
                os.umask(previous_umask)

            self.assertEqual(len(observed_parts), 2)
            self.assertEqual(len(set(observed_parts)), 2)
            self.assertNotIn(stale_part, observed_parts)
            self.assertEqual(stale_part.read_text(encoding="utf-8"), "leave this alone")
            self.assertEqual(stat.S_IMODE(credential_path.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX fsync behavior")
    def test_plaintext_write_failure_cleans_only_its_private_temp_file(self):
        secret = "model-secret"
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "config"
            environment = {
                config.CONFIG_DIR_ENV: str(config_dir),
                config.DISABLE_SECURE_STORE_ENV: "1",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(config.os, "fsync", side_effect=OSError("sync failed")):
                    with self.assertRaisesRegex(OSError, "sync failed") as raised:
                        config.save_api_key(secret, allow_plaintext=True)

            self.assertNotIn(secret, str(raised.exception))
            self.assertEqual(list(config_dir.glob(".credentials.json.*")), [])
            self.assertFalse((config_dir / "credentials.json").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor ownership")
    def test_plaintext_fchmod_failure_closes_unowned_descriptor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "config"
            environment = {
                config.CONFIG_DIR_ENV: str(config_dir),
                config.DISABLE_SECURE_STORE_ENV: "1",
            }
            closed = []
            real_close = config.os.close

            def record_close(descriptor):
                closed.append(descriptor)
                return real_close(descriptor)

            with patch.dict(os.environ, environment, clear=False):
                with patch.object(config.os, "fchmod", side_effect=OSError("mode failed")):
                    with patch.object(config.os, "close", side_effect=record_close):
                        with self.assertRaisesRegex(OSError, "mode failed"):
                            config.save_api_key("model-secret", allow_plaintext=True)

            self.assertEqual(len(closed), 1)
            self.assertEqual(list(config_dir.glob(".credentials.json.*")), [])
            self.assertFalse((config_dir / "credentials.json").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX replace behavior")
    def test_plaintext_replace_failure_cleans_temp_without_exposing_key(self):
        secret = "model-secret"
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "config"
            environment = {
                config.CONFIG_DIR_ENV: str(config_dir),
                config.DISABLE_SECURE_STORE_ENV: "1",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(
                    config.os, "replace", side_effect=OSError("replace failed")
                ):
                    with self.assertRaisesRegex(OSError, "replace failed") as raised:
                        config.save_api_key(secret, allow_plaintext=True)

            self.assertNotIn(secret, str(raised.exception))
            self.assertEqual(list(config_dir.glob(".credentials.json.*")), [])
            self.assertFalse((config_dir / "credentials.json").exists())

    def test_save_api_key_persists_only_the_normalized_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.DISABLE_SECURE_STORE_ENV: "1",
            }
            with patch.dict(os.environ, environment, clear=False):
                config.save_api_key("  saved-secret  ", allow_plaintext=True)

            stored = json.loads(
                (Path(temp_dir) / "credentials.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stored["api_key"], "saved-secret")

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

    def test_load_config_rejects_malformed_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text("{", encoding="utf-8")
            with patch.dict(os.environ, {config.CONFIG_DIR_ENV: temp_dir}):
                with self.assertRaisesRegex(config.ConfigError, "配置文件格式无效"):
                    config.load_config()

    def test_load_config_rejects_non_object_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text("[]", encoding="utf-8")
            with patch.dict(os.environ, {config.CONFIG_DIR_ENV: temp_dir}):
                with self.assertRaisesRegex(config.ConfigError, "配置文件格式无效"):
                    config.load_config()

    def test_load_config_rejects_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_bytes(b"\xff")
            with patch.dict(os.environ, {config.CONFIG_DIR_ENV: temp_dir}):
                with self.assertRaisesRegex(config.ConfigError, "配置文件格式无效"):
                    config.load_config()

    def test_load_config_migrates_the_exact_legacy_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps({
                    "base_url": "https://api.supertoken.cc/image-wrapper/v1",
                    "model": "gpt-image-2-count",
                }),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {config.CONFIG_DIR_ENV: temp_dir}):
                value = config.load_config()
            self.assertEqual(value, config.build_config())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), value)
            self.assertFalse(Path(f"{path}.part").exists())

    def test_load_config_preserves_an_unversioned_custom_base(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps({"base_url": "https://proxy.example/v1", "model": "custom"}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {config.CONFIG_DIR_ENV: temp_dir}):
                value = config.load_config()
            self.assertEqual(
                value,
                {"version": 2, "base_url": "https://proxy.example/v1", "model": "custom"},
            )

    def test_resource_key_uses_its_own_environment_variable(self):
        environment = {
            config.API_KEY_ENV: "model-secret",
            config.RESOURCE_API_KEY_ENV: "resource-secret",
        }
        with patch.dict(os.environ, environment, clear=False):
            self.assertEqual(config.get_api_key(config.MODEL_KEY), "model-secret")
            self.assertEqual(config.get_api_key(config.RESOURCE_KEY), "resource-secret")

    def test_known_key_type_mismatches_are_rejected_without_exposing_values(self):
        cases = [
            (config.MODEL_KEY, config.API_KEY_ENV, "ak_resource123456", "模型 API Token"),
            (config.RESOURCE_KEY, config.RESOURCE_API_KEY_ENV, "sk-model123456", "资源 API Key"),
            (config.MODEL_KEY, config.API_KEY_ENV, "wk-webhook123456", "Webhook Key"),
        ]
        for kind, env_name, value, expected in cases:
            with self.subTest(kind=kind, value=value[:3]):
                with patch.dict(os.environ, {env_name: value}, clear=True):
                    with self.assertRaisesRegex(config.ConfigError, expected) as raised:
                        config.get_api_key(kind)
                self.assertNotIn(value, str(raised.exception))

    def test_load_config_rejects_invalid_v2_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps({
                    "version": 2,
                    "base_url": 7,
                    "model": "gpt-image-2-count",
                }),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {config.CONFIG_DIR_ENV: temp_dir}):
                with self.assertRaisesRegex(config.ConfigError, "base_url"):
                    config.load_config()

    def test_plaintext_credentials_keep_both_key_types(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.DISABLE_SECURE_STORE_ENV: "1",
            }
            with patch.dict(os.environ, environment, clear=False):
                config.save_api_key("model-secret", True, config.MODEL_KEY)
                config.save_api_key("resource-secret", True, config.RESOURCE_KEY)
                stored = json.loads((Path(temp_dir) / "credentials.json").read_text())
                self.assertEqual(
                    stored,
                    {"api_key": "model-secret", "resource_api_key": "resource-secret"},
                )

    def test_resource_dpapi_corruption_is_a_controlled_config_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "resource-credentials.dpapi"
            path.write_bytes(b"not-base64%%")
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.RESOURCE_API_KEY_ENV: "",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(config.platform, "system", return_value="Windows"):
                    with self.assertRaisesRegex(config.ConfigError, "DPAPI 凭据文件格式无效"):
                        config.get_api_key(config.RESOURCE_KEY)

    def test_get_api_key_rejects_malformed_plaintext_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "credentials.json"
            path.write_text("{", encoding="utf-8")
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.API_KEY_ENV: "",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(config.platform, "system", return_value="Linux"):
                    with patch.object(config.shutil, "which", return_value=None):
                        with self.assertRaisesRegex(config.ConfigError, "凭据文件格式无效"):
                            config.get_api_key()

    def test_get_api_key_rejects_non_object_plaintext_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "credentials.json"
            path.write_text("[]", encoding="utf-8")
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.API_KEY_ENV: "",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(config.platform, "system", return_value="Linux"):
                    with patch.object(config.shutil, "which", return_value=None):
                        with self.assertRaisesRegex(config.ConfigError, "凭据文件格式无效"):
                            config.get_api_key()

    def test_main_reports_invalid_config_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text("{", encoding="utf-8")
            stderr = io.StringIO()
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.API_KEY_ENV: "test-key",
            }
            with patch.dict(os.environ, environment, clear=False):
                with contextlib.redirect_stderr(stderr):
                    code = generator.main(
                        [
                            "--prompt",
                            "一只坐在阳光里的小猫",
                            "--output",
                            str(Path(temp_dir) / "image.png"),
                        ]
                    )

            self.assertEqual(code, 2)
            self.assertIn("配置文件格式无效", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_invalid_dpapi_credentials_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "credentials.dpapi"
            path.write_bytes(b"not-valid-base64%%")
            stderr = io.StringIO()
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.API_KEY_ENV: "",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(config.platform, "system", return_value="Windows"):
                    with contextlib.redirect_stderr(stderr):
                        code = generator.main(
                            [
                                "--prompt",
                                "一只坐在阳光里的小猫",
                                "--output",
                                str(Path(temp_dir) / "image.png"),
                            ]
                        )

            self.assertEqual(code, 2)
            self.assertIn("DPAPI 凭据文件格式无效", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn("Incorrect padding", stderr.getvalue())

    def test_main_reports_invalid_dpapi_plaintext_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "credentials.dpapi"
            path.write_bytes(base64.b64encode(b"encrypted"))
            stderr = io.StringIO()
            environment = {
                config.CONFIG_DIR_ENV: temp_dir,
                config.API_KEY_ENV: "",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(config.platform, "system", return_value="Windows"):
                    with patch.object(config, "_windows_unprotect", return_value=b"\xff"):
                        with contextlib.redirect_stderr(stderr):
                            code = generator.main(
                                [
                                    "--prompt",
                                    "一只坐在阳光里的小猫",
                                    "--output",
                                    str(Path(temp_dir) / "image.png"),
                                ]
                            )

            self.assertEqual(code, 2)
            self.assertIn("DPAPI 凭据文件格式无效", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn("UnicodeDecodeError", stderr.getvalue())


class LegacyGeneratorCompatibilityTests(unittest.TestCase):
    def test_legacy_help_exposes_only_v01_options(self):
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp1252"
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "generate_image.py"), "--help"],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--prompt", result.stdout)
        self.assertIn("--json-params", result.stdout)
        for modern in (
            " generate ", "--async", "--wait", "--idempotency-key", "--n",
            "--metadata-json", "--resource-api-key",
        ):
            self.assertNotIn(modern, result.stdout)

    def test_legacy_wrapper_rejects_modern_only_options(self):
        for option in (
            "--async", "--wait", "--idempotency-key", "--n", "--metadata-json",
        ):
            with self.subTest(option=option):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS_DIR / "generate_image.py"),
                        "--prompt", "cat", "--output", "cat.png", option,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("generate_image.py generate", result.stderr)

    def test_legacy_json_param_file_errors_exit_two_before_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid_utf8 = root / "invalid-utf8.json"
            invalid_utf8.write_bytes(b"\xff")
            invalid_json = root / "invalid-json.json"
            invalid_json.write_text("{", encoding="utf-8")
            unreadable = root / "directory.json"
            unreadable.mkdir()
            cases = [root / "missing.json", invalid_utf8, invalid_json, unreadable]
            environment = {
                **os.environ,
                config.CONFIG_DIR_ENV: str(root / "config"),
                config.API_KEY_ENV: "test-key",
            }
            for path in cases:
                with self.subTest(path=path.name):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPTS_DIR / "generate_image.py"),
                            "--prompt", "cat",
                            "--output", str(root / "out.png"),
                            "--json-params", str(path),
                        ],
                        text=True,
                        capture_output=True,
                        env=environment,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_main_uses_legacy_timeout_when_omitted(self):
        with patch.object(generator, "image_main", return_value=0) as image_main:
            code = generator.main(["--prompt", "cat", "--output", "cat.png"])

        self.assertEqual(code, 0)
        image_main.assert_called_once_with(
            ["generate", "--prompt", "cat", "--output", "cat.png", "--timeout", "180"],
            legacy_output=True,
        )

    def test_main_preserves_explicit_legacy_timeout(self):
        for timeout_args in (["--timeout", "45"], ["--timeout=60"]):
            with self.subTest(timeout_args=timeout_args):
                arguments = ["--prompt", "cat", "--output", "cat.png", *timeout_args]
                with patch.object(generator, "image_main", return_value=0) as image_main:
                    code = generator.main(arguments)

                self.assertEqual(code, 0)
                image_main.assert_called_once_with(
                    ["generate", *arguments], legacy_output=True
                )

    def test_main_preserves_legacy_stdout_and_exact_output_path(self):
        image_bytes = b"\x89PNG\r\n\x1a\nlegacy"
        response = api.ApiResponse(
            201,
            {"Content-Type": "image/png"},
            json.dumps({
                "data": [{
                    "b64_json": base64.b64encode(image_bytes).decode("ascii")
                }]
            }).encode("utf-8"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "legacy-result.jpg"
            environment = {
                config.API_KEY_ENV: "test-key",
                config.CONFIG_DIR_ENV: str(Path(temp_dir) / "config"),
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(cli.api, "request_json", return_value=response):
                    with contextlib.redirect_stdout(stdout):
                        with contextlib.redirect_stderr(stderr):
                            code = generator.main([
                                "--prompt", "cat", "--output", str(output),
                            ])

            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(output.read_bytes(), image_bytes)
            self.assertFalse(output.with_suffix(".png").exists())
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
                    "status": 201,
                    "base_url": config.DEFAULT_BASE_URL,
                    "model": config.DEFAULT_MODEL,
                    "output": str(output),
                    "bytes": len(image_bytes),
                    "content_type": "image/png",
                },
            )

    def test_legacy_stdout_redacts_server_controlled_content_type(self):
        image_bytes = b"\x89PNG\r\n\x1a\nlegacy"
        response = api.ApiResponse(
            201,
            {"Content-Type": "image/png; note=sk-serversecret123"},
            json.dumps({
                "data": [{
                    "b64_json": base64.b64encode(image_bytes).decode("ascii")
                }]
            }).encode("utf-8"),
        )
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "legacy.png"
            environment = {
                config.API_KEY_ENV: "test-key",
                config.CONFIG_DIR_ENV: str(Path(temp_dir) / "config"),
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(cli.api, "request_json", return_value=response):
                    with contextlib.redirect_stdout(stdout):
                        code = generator.main([
                            "--prompt", "cat", "--output", str(output),
                        ])

        self.assertEqual(code, 0)
        value = json.loads(stdout.getvalue())
        self.assertEqual(value["content_type"], "image/png; note=[REDACTED]")
        self.assertNotIn("sk-serversecret123", stdout.getvalue())

    def test_legacy_stdout_redacts_credential_shaped_base_url(self):
        image_bytes = b"\x89PNG\r\n\x1a\nlegacy"
        response = api.ApiResponse(
            201,
            {"Content-Type": "image/png"},
            json.dumps({
                "data": [{
                    "b64_json": base64.b64encode(image_bytes).decode("ascii")
                }]
            }).encode("utf-8"),
        )
        secret = "sk-baseurlsecret123"
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "legacy.png"
            environment = {
                config.API_KEY_ENV: "test-key",
                config.CONFIG_DIR_ENV: str(Path(temp_dir) / "config"),
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(cli.api, "request_json", return_value=response):
                    with contextlib.redirect_stdout(stdout):
                        code = generator.main([
                            "--prompt", "cat", "--output", str(output),
                            "--base-url", f"https://proxy.example/{secret}",
                        ])

        self.assertEqual(code, 0)
        self.assertNotIn(secret, stdout.getvalue())
        self.assertEqual(
            json.loads(stdout.getvalue())["base_url"],
            "https://proxy.example/[REDACTED]",
        )

    def test_legacy_sync_rejects_more_results_than_requested(self):
        image_bytes = b"\x89PNG\r\n\x1a\nlegacy"
        item = {"b64_json": base64.b64encode(image_bytes).decode("ascii")}
        response = api.ApiResponse(
            200,
            {"Content-Type": "application/json"},
            json.dumps({"data": [item, item]}).encode("utf-8"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "legacy.png"
            environment = {
                config.API_KEY_ENV: "test-key",
                config.CONFIG_DIR_ENV: str(Path(temp_dir) / "config"),
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(cli.api, "request_json", return_value=response):
                    with contextlib.redirect_stdout(stdout):
                        with contextlib.redirect_stderr(stderr):
                            code = generator.main([
                                "--prompt", "cat", "--output", str(output),
                            ])

            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("预期 1", stderr.getvalue())
            self.assertFalse(output.exists())


class SupertokenSetupTests(unittest.TestCase):
    def test_setup_rejects_removed_profile_argument(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                setup_script.parse_args(["--profile", "old-provider"])

        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(stderr.getvalue(), "参数错误：请使用 --help 查看可用参数。\n")
        self.assertNotIn("unrecognized arguments", stderr.getvalue())

    def test_setup_saves_supertoken_config_and_key(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            with contextlib.redirect_stderr(stderr):
                with patch.object(setup_script, "config_path", return_value="/portable/config.json"):
                    with patch.object(setup_script, "save_api_key", return_value="macos-keychain") as save_key:
                        with patch.object(setup_script, "save_config") as save_config:
                            code = setup_script.main(["--api-key", "test-key"])

        self.assertEqual(code, 0)
        save_key.assert_called_once_with("test-key", False, config.MODEL_KEY)
        save_config.assert_called_once_with(config.build_config())
        self.assertEqual(
            stdout.getvalue(),
            "配置已保存到：/portable/config.json\n"
            "模型 API Key 已保存到：macos-keychain\n"
            "默认模型：gpt-image-2-count\n",
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("test-key", stdout.getvalue())
        self.assertNotIn("test-key", stderr.getvalue())

    def test_setup_saves_optional_resource_key_separately(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            with contextlib.redirect_stderr(stderr):
                with patch.object(setup_script, "config_path", return_value="/portable/config.json"):
                    with patch.object(setup_script, "save_api_key", return_value="macos-keychain") as save_key:
                        with patch.object(setup_script, "save_config") as save_config:
                            code = setup_script.main(
                                [
                                    "--api-key",
                                    "model-secret",
                                    "--resource-api-key",
                                    "resource-secret",
                                ]
                            )

        self.assertEqual(code, 0)
        self.assertEqual(
            save_key.call_args_list,
            [
                unittest.mock.call("model-secret", False, config.MODEL_KEY),
                unittest.mock.call("resource-secret", False, config.RESOURCE_KEY),
            ],
        )
        save_config.assert_called_once_with(config.build_config())
        self.assertNotIn("model-secret", stdout.getvalue())
        self.assertNotIn("model-secret", stderr.getvalue())
        self.assertNotIn("resource-secret", stdout.getvalue())
        self.assertNotIn("resource-secret", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
