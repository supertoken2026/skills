import base64
import contextlib
import io
import json
import os
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

import supertoken_api as api  # noqa: E402
import supertoken_config as config  # noqa: E402
import supertoken_image as cli  # noqa: E402


PNG_BYTES = b"\x89PNG\r\n\x1a\nimage"


def api_response(payload, status=200, headers=None):
    return api.ApiResponse(
        status,
        headers or {"Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
    )


def run_cli(argv, environment=None):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with tempfile.TemporaryDirectory() as config_dir:
        runtime_environment = {
            config.CONFIG_DIR_ENV: config_dir,
            **(environment or {}),
        }
        with patch.dict(os.environ, runtime_environment, clear=False):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class ParserTests(unittest.TestCase):
    def test_generate_defaults(self):
        args = cli.parse_args([
            "generate", "--prompt", "cat", "--output", "cat.png",
        ])
        self.assertEqual(args.command, "generate")
        self.assertEqual(args.timeout, 300)
        self.assertEqual(args.count, 1)

    def test_invalid_generation_combinations_fail_before_requests(self):
        cases = [
            ["generate", "--prompt", "cat"],
            ["generate", "--prompt", "cat", "--output", "cat.png", "--wait"],
            [
                "generate", "--prompt", "cat", "--output", "cat.png", "--async",
            ],
        ]
        environment = {config.API_KEY_ENV: "test-key"}
        with patch.object(cli.api, "request_json") as request_json:
            for argv in cases:
                with self.subTest(argv=argv):
                    code, _stdout, _stderr = run_cli(argv, environment)
                    self.assertEqual(code, 2)
            request_json.assert_not_called()


class ModelListingTests(unittest.TestCase):
    def test_explicit_base_url_refreshes_existing_config_without_losing_model(self):
        current = config.build_config(
            base_url="https://old-proxy.example/v1", model="custom-image-model",
        )
        refreshed = config.build_config(
            base_url="https://new-proxy.example/v1", model="custom-image-model",
        )
        with tempfile.TemporaryDirectory() as config_dir:
            with patch.dict(os.environ, {
                config.CONFIG_DIR_ENV: config_dir,
                config.API_KEY_ENV: "test-key",
            }, clear=False):
                config.save_config(current)
                args = cli.parse_args([
                    "models", "--base-url", "https://new-proxy.example/v1/",
                ])
                with patch.object(cli, "save_config", wraps=config.save_config) as save_config:
                    _current, base_url, key = cli.resolve_runtime(args)

                self.assertEqual(base_url, "https://new-proxy.example/v1")
                self.assertEqual(key, "test-key")
                save_config.assert_called_once_with(refreshed)
                self.assertEqual(config.load_config(), refreshed)

    def test_models_filters_to_gpt_image_2_unless_all_is_requested(self):
        response = api_response({
            "data": [
                {"id": "gpt-image-2"},
                {"id": "gpt-image-2-count"},
                {"id": "gpt-4.1"},
                {"id": 7},
            ],
        })
        environment = {config.API_KEY_ENV: "test-key"}
        with patch.object(cli.api, "request_json", return_value=response) as request_json:
            code, stdout, stderr = run_cli(["models"], environment)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {"models": ["gpt-image-2", "gpt-image-2-count"]})
        request_json.assert_called_once_with(
            "GET", "https://api.supertoken.cc/v1/models", "test-key", 300,
        )

        with patch.object(cli.api, "request_json", return_value=response):
            code, stdout, stderr = run_cli(["models", "--all"], environment)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {"models": ["gpt-image-2", "gpt-image-2-count", "gpt-4.1"]},
        )


class SyncGenerationTests(unittest.TestCase):
    def test_generation_saves_every_image_and_reports_all_outputs(self):
        items = [
            {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")},
            {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")},
        ]
        environment = {config.API_KEY_ENV: "test-key"}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output.png"
            with patch.object(cli.api, "request_json", return_value=api_response({"data": items})) as request_json:
                code, stdout, stderr = run_cli(
                    [
                        "generate", "--prompt", "cat", "--model", "gpt-image-2",
                        "--n", "2", "--output", str(output),
                    ],
                    environment,
                )

            result = json.loads(stdout)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertNotIn("test-key", stdout)
            self.assertNotIn("test-key", stderr)
            request_json.assert_called_once()
            method, url, key, timeout, payload = request_json.call_args.args
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://api.supertoken.cc/v1/images/generations")
            self.assertEqual(key, "test-key")
            self.assertEqual(timeout, 300)
            self.assertEqual(payload["n"], 2)
            self.assertEqual(
                result,
                {
                    "mode": "sync",
                    "operation": "generation",
                    "model": "gpt-image-2",
                    "outputs": [
                        {
                            "path": str(Path(temp_dir, "output-1.png").resolve()),
                            "bytes": len(PNG_BYTES),
                            "format": "png",
                            "source_url": None,
                        },
                        {
                            "path": str(Path(temp_dir, "output-2.png").resolve()),
                            "bytes": len(PNG_BYTES),
                            "format": "png",
                            "source_url": None,
                        },
                    ],
                },
            )

    def test_count_model_rejects_multiple_images_before_request(self):
        environment = {config.API_KEY_ENV: "test-key"}
        with patch.object(cli.api, "request_json") as request_json:
            code, _stdout, stderr = run_cli(
                [
                    "generate", "--prompt", "cat", "--model", "gpt-image-2-count",
                    "--n", "2", "--output", "cat.png",
                ],
                environment,
            )

        self.assertEqual(code, 2)
        self.assertIn("--n", stderr)
        request_json.assert_not_called()

    def test_raw_diagnostic_redacts_explicit_and_recognized_keys_atomically(self):
        explicit = "explicit-secret"
        response = api_response({
            "data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}],
            "detail": f"{explicit} sk-123456789 ak_123456789 wk-123456789",
        })
        environment = {config.API_KEY_ENV: explicit}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"
            raw_json = Path(temp_dir) / "response.txt"
            with patch.object(cli.api, "request_json", return_value=response):
                code, stdout, stderr = run_cli(
                    [
                        "generate", "--prompt", "cat", "--output", str(output),
                        "--raw-json", str(raw_json),
                    ],
                    environment,
                )

            diagnostic = raw_json.read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertTrue(stdout)
            self.assertFalse(Path(f"{raw_json}.part").exists())
            for secret in (explicit, "sk-123456789", "ak_123456789", "wk-123456789"):
                self.assertNotIn(secret, diagnostic)

    def test_generation_payload_keeps_legacy_optional_parameter_behavior(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            params_path = Path(temp_dir) / "params.json"
            params_path.write_text('{"style": "vivid"}', encoding="utf-8")
            args = cli.parse_args([
                "generate", "--prompt", "cat", "--output", "cat.png",
                "--format", "webp", "--background", "opaque",
                "--json-params", str(params_path), "--param", "seed=7",
            ])
            self.assertEqual(
                cli.build_generation_payload(args),
                {
                    "model": "gpt-image-2-count",
                    "prompt": "cat",
                    "n": 1,
                    "size": "1024x1024",
                    "quality": "low",
                    "output_format": "webp",
                    "background": "opaque",
                    "style": "vivid",
                    "seed": 7,
                },
            )

    def test_generation_payload_rejects_non_object_json_params(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            params_path = Path(temp_dir) / "params.json"
            params_path.write_text("[]", encoding="utf-8")
            args = cli.parse_args([
                "generate", "--prompt", "cat", "--output", "cat.png",
                "--json-params", str(params_path),
            ])

            with self.assertRaisesRegex(ValueError, "JSON 对象"):
                cli.build_generation_payload(args)

    def test_save_image_items_preserves_atomic_base64_output_behavior(self):
        item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"

            saved = api.save_image_items([item], output, timeout=5)

            self.assertEqual(saved[0].bytes_written, len(PNG_BYTES))
            self.assertEqual(output.read_bytes(), PNG_BYTES)
            self.assertFalse(Path(f"{output}.part").exists())

    def test_save_image_items_preserves_atomic_url_output_behavior(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"

            with patch.object(api, "download_image", return_value=PNG_BYTES) as download_image:
                saved = api.save_image_items(
                    [{"url": "https://cdn.example.test/image.png"}], output, timeout=5,
                )

            download_image.assert_called_once_with("https://cdn.example.test/image.png", 5)
            self.assertEqual(saved[0].bytes_written, len(PNG_BYTES))
            self.assertEqual(output.read_bytes(), PNG_BYTES)
            self.assertFalse(Path(f"{output}.part").exists())

    def test_save_image_items_rejects_missing_or_invalid_image_data_without_parts(self):
        cases = [{}, {"b64_json": "%%%"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            for index, item in enumerate(cases):
                with self.subTest(item=item):
                    output = Path(temp_dir) / f"image-{index}.png"
                    with self.assertRaises(api.ApiResponseError):
                        api.save_image_items([item], output, timeout=5)
                    self.assertFalse(output.exists())
                    self.assertFalse(Path(f"{output}.part").exists())
