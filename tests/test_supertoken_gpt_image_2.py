import base64
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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


def generation_args(**overrides):
    values = {
        "prompt": "一只坐在阳光里的小猫",
        "model": None,
        "size": "1024x1024",
        "quality": "low",
        "output_format": None,
        "background": None,
        "param": [],
        "json_params": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


class GenerateImagePayloadTests(unittest.TestCase):
    def test_resolve_model_uses_default_model(self):
        self.assertEqual(generator.resolve_model(generation_args()), "gpt-image-2-count")

    def test_resolve_model_allows_explicit_override(self):
        args = generation_args(model="gpt-image-2")
        self.assertEqual(generator.resolve_model(args), "gpt-image-2")

    def test_build_payload_sends_only_required_defaults(self):
        payload = generator.build_payload(generation_args())

        self.assertEqual(
            payload,
            {
                "model": "gpt-image-2-count",
                "prompt": "一只坐在阳光里的小猫",
                "size": "1024x1024",
                "quality": "low",
            },
        )

    def test_build_payload_includes_optional_fields_only_when_explicit(self):
        args = generation_args(output_format="webp", background="opaque")

        payload = generator.build_payload(args)

        self.assertEqual(payload["output_format"], "webp")
        self.assertEqual(payload["background"], "opaque")

    def test_merge_extra_params_rejects_non_object_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            params_path = Path(temp_dir) / "params.json"
            params_path.write_text("[]", encoding="utf-8")
            args = generation_args(json_params=str(params_path))

            with self.assertRaisesRegex(ValueError, "JSON 对象"):
                generator.build_payload(args)


class GenerateImageOutputTests(unittest.TestCase):
    def test_b64_json_response_is_written_atomically(self):
        image_bytes = b"\x89PNG\r\n\x1a\nimage-data"
        item = {"b64_json": base64.b64encode(image_bytes).decode("ascii")}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"

            written = generator.save_response_image(item, output, timeout=5)

            self.assertEqual(written, len(image_bytes))
            self.assertEqual(output.read_bytes(), image_bytes)
            self.assertFalse(Path(f"{output}.part").exists())

    def test_url_response_is_downloaded_to_part_then_replaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"

            def fake_download(url, part_path, timeout):
                self.assertEqual(url, "https://cdn.example.test/image.png")
                self.assertTrue(str(part_path).endswith(".part"))
                part_path.write_bytes(b"downloaded-image")

            with patch.object(generator, "download_url", side_effect=fake_download):
                written = generator.save_response_image(
                    {"url": "https://cdn.example.test/image.png"},
                    output,
                    timeout=5,
                )

            self.assertEqual(written, len(b"downloaded-image"))
            self.assertEqual(output.read_bytes(), b"downloaded-image")
            self.assertFalse(Path(f"{output}.part").exists())

    def test_missing_image_fields_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"

            with self.assertRaisesRegex(generator.GenerationError, "url.*b64_json"):
                generator.save_response_image({}, output, timeout=5)

            self.assertFalse(output.exists())
            self.assertFalse(Path(f"{output}.part").exists())

    def test_invalid_base64_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"

            with self.assertRaises(Exception):
                generator.save_response_image({"b64_json": "%%%"}, output, timeout=5)

            self.assertFalse(output.exists())
            self.assertFalse(Path(f"{output}.part").exists())


class GenerateImageErrorTests(unittest.TestCase):
    def test_request_json_sets_bearer_and_json_headers(self):
        response = unittest.mock.MagicMock()
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{"data": []}'
        response.__enter__.return_value = response
        with patch.object(generator.urllib.request, "urlopen", return_value=response) as urlopen:
            status, headers, body = generator.request_json(
                "https://api.example.test/images/generations",
                "test-key",
                {"model": "gpt-image-2-count"},
                30,
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body, b'{"data": []}')

    def test_http_statuses_map_to_specific_messages(self):
        self.assertIn("无效", generator.classify_http_error(401, "model", {}))
        self.assertIn("访问权限", generator.classify_http_error(403, "model", {}))
        self.assertIn("频率", generator.classify_http_error(429, "model", {}))
        self.assertIn(
            "request-123",
            generator.classify_http_error(503, "model", {"x-request-id": "request-123"}),
        )

    def test_non_json_diagnostic_is_truncated_and_redacted(self):
        api_key = "sk-secret-value-123456"
        body = (api_key + " " + "x" * 2000).encode("utf-8")

        text = generator.sanitize_diagnostic(body, api_key)

        self.assertNotIn(api_key, text)
        self.assertIn("[REDACTED]", text)
        self.assertLessEqual(len(text), 1000)

    def test_raw_diagnostic_file_does_not_contain_api_key(self):
        api_key = "sk-secret-value-123456"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "response.txt"

            generator.write_raw_diagnostics(path, api_key.encode("utf-8"), api_key)

            self.assertNotIn(api_key, path.read_text(encoding="utf-8"))


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
