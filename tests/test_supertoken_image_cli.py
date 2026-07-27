import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
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

    def test_mode_specific_options_are_rejected_before_requests(self):
        sync_base = [
            "generate", "--prompt", "cat", "--output", "unused.png",
        ]
        async_base = ["generate", "--prompt", "cat", "--async"]
        cases = [
            [*sync_base, "--idempotency-key", "request-key"],
            [*sync_base, "--output-compression", "50"],
            [*sync_base, "--client-reference-id", "client-1"],
            [*sync_base, "--metadata-json", "{}"],
            [*sync_base, "--resource-api-key", "custom-resource-key"],
            [*sync_base, "--wait-timeout", "900"],
            [*async_base, "--param", "style=vivid"],
            [*async_base, "--json-params", "unused.json"],
            [*async_base, "--raw-json", "unused.json"],
            [*async_base, "--resource-api-key", "unused-resource-key"],
            [*async_base, "--wait-timeout", "900"],
        ]
        unexpected = api_response({"id": "task_unexpected", "status": "queued"})
        environment = {config.API_KEY_ENV: "test-key"}
        with patch.object(
            cli.api, "request_json", return_value=unexpected,
        ) as json_request:
            with patch.object(cli.api, "request_multipart") as multipart_request:
                for argv in cases:
                    with self.subTest(argv=argv):
                        code, _stdout, _stderr = run_cli(argv, environment)
                        self.assertEqual(code, 2)
                json_request.assert_not_called()
                multipart_request.assert_not_called()


class ModelListingTests(unittest.TestCase):
    def test_explicit_api_base_is_validated_before_requests_or_persistence(self):
        with patch.object(cli.api, "request_json") as request:
            with patch.object(cli, "save_config") as save:
                code, _stdout, stderr = run_cli(
                    ["models", "--base-url", "http://user:secret@example.test/v1"],
                    {config.API_KEY_ENV: "test-key"},
                )

        self.assertEqual(code, 2)
        self.assertIn("base_url", stderr)
        self.assertNotIn("secret", stderr)
        request.assert_not_called()
        save.assert_not_called()

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

    def test_models_rejects_malformed_success_schemas_without_traceback(self):
        malformed = [
            {"data": None},
            {"data": [{}]},
            {"data": [{"id": 7}]},
            {"data": ["gpt-image-2"]},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                with patch.object(
                    cli.api, "request_json", return_value=api_response(payload)
                ) as request:
                    code, stdout, stderr = run_cli(
                        ["models"], {config.API_KEY_ENV: "test-key"}
                    )
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("模型列表", stderr)
                self.assertNotIn("Traceback", stderr)
                request.assert_called_once()

    def test_models_redacts_server_controlled_credentials_and_signed_urls(self):
        response = api_response({
            "data": [
                {"id": "gpt-image-2-sk-serversecret123"},
                {
                    "id": (
                        "https://user:pass@example.test/gpt-image-2"
                        "?token=signed-secret#fragment"
                    )
                },
            ],
        })
        with patch.object(cli.api, "request_json", return_value=response):
            code, stdout, stderr = run_cli(
                ["models", "--all"], {config.API_KEY_ENV: "test-key"}
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            json.loads(stdout),
            {
                "models": [
                    "gpt-image-2-[REDACTED]",
                    "https://example.test/gpt-image-2",
                ]
            },
        )
        for secret in (
            "sk-serversecret123", "user:pass", "signed-secret", "fragment"
        ):
            self.assertNotIn(secret, stdout)


class ExplicitCliKeyTests(unittest.TestCase):
    def test_explicit_model_key_rejects_resource_and_webhook_keys_before_requests(self):
        cases = {
            "models": ["models"],
            "sync-generate": [
                "generate", "--prompt", "cat", "--output", "cat.png",
            ],
            "async-generate": ["generate", "--prompt", "cat", "--async"],
            "sync-edit": [
                "edit", "--prompt", "combine", "--image", "https://img.example/one.png",
                "--output", "result.png",
            ],
            "async-edit": [
                "edit", "--prompt", "combine", "--image", "https://img.example/one.png",
                "--async",
            ],
        }
        unexpected_response = api_response({"error": "unexpected"}, status=400)
        with patch.object(
            cli.api, "request_json", return_value=unexpected_response,
        ) as json_request:
            with patch.object(cli.api, "request_multipart") as multipart_request:
                for name, argv in cases.items():
                    for wrong_key in ("ak_explicit_test", "wk-explicit-test"):
                        with self.subTest(command=name, key=wrong_key):
                            code, _stdout, stderr = run_cli(
                                [*argv, "--api-key", wrong_key]
                            )
                            self.assertEqual(code, 2)
                            self.assertIn("SUPERTOKEN_API_KEY", stderr)
                json_request.assert_not_called()
                multipart_request.assert_not_called()

    def test_explicit_resource_key_rejects_model_and_webhook_keys_before_requests(self):
        cases = {
            "task": ["task", "task_test"],
            "wait": ["wait", "task_test", "--output", "result.png"],
            "async-generate-wait": [
                "generate", "--prompt", "cat", "--async", "--wait", "--output", "result.png",
            ],
            "async-edit-wait": [
                "edit", "--prompt", "combine", "--image", "https://img.example/one.png",
                "--async", "--wait", "--output", "result.png",
            ],
        }
        environment = {config.API_KEY_ENV: "custom-model-key"}
        unexpected_response = api_response({"error": "unexpected"}, status=400)
        with patch.object(
            cli.api, "request_json", return_value=unexpected_response,
        ) as json_request:
            with patch.object(cli.api, "request_multipart") as multipart_request:
                for name, argv in cases.items():
                    for wrong_key in ("sk-explicit-test", "wk-explicit-test"):
                        with self.subTest(command=name, key=wrong_key):
                            code, _stdout, stderr = run_cli(
                                [*argv, "--resource-api-key", wrong_key], environment,
                            )
                            self.assertEqual(code, 2)
                            self.assertIn("SUPERTOKEN_RESOURCE_API_KEY", stderr)
                json_request.assert_not_called()
                multipart_request.assert_not_called()

    def test_explicit_unknown_key_prefixes_remain_compatible(self):
        models_response = api_response({"data": []})
        task_response = api_response({"id": "task_test", "status": "queued"})
        with patch.object(
            cli.api, "request_json", side_effect=[models_response, task_response],
        ) as request:
            models_code, _stdout, models_stderr = run_cli([
                "models", "--api-key", "custom-model-key",
            ])
            task_code, _stdout, task_stderr = run_cli([
                "task", "task_test", "--resource-api-key", "custom-resource-key",
            ])

        self.assertEqual(models_code, 0, models_stderr)
        self.assertEqual(task_code, 0, task_stderr)
        self.assertEqual(request.call_count, 2)


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
                            "path": str(Path(temp_dir, "output-1.png").absolute()),
                            "bytes": len(PNG_BYTES),
                            "format": "png",
                        },
                        {
                            "path": str(Path(temp_dir, "output-2.png").absolute()),
                            "bytes": len(PNG_BYTES),
                            "format": "png",
                        },
                    ],
                },
            )

    def test_signed_result_url_is_not_reported(self):
        signed_url = "https://cdn.example.test/image.png?token=secret-signed-value"
        response = api_response({"data": [{"url": signed_url}]})
        environment = {config.API_KEY_ENV: "test-key"}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output.png"
            with patch.object(cli.api, "request_json", return_value=response):
                with patch.object(cli.api, "download_image", return_value=PNG_BYTES):
                    code, stdout, stderr = run_cli([
                        "generate", "--prompt", "cat", "--output", str(output),
                    ], environment)

            self.assertEqual(code, 0, stderr)
            self.assertNotIn(signed_url, stdout)
            self.assertNotIn("secret-signed-value", stdout)
            self.assertNotIn("secret-signed-value", stderr)
            self.assertEqual(
                set(json.loads(stdout)["outputs"][0]),
                {"path", "bytes", "format"},
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
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        response = api_response({
            "data": [{
                "b64_json": encoded,
                "url": "https://user:pass@cdn.example/image.png?token=signed#part",
            }],
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
            for secret in (encoded, "user:pass", "signed", "#part"):
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


class SyncEditTests(unittest.TestCase):
    def test_url_edit_uses_the_sync_json_shape(self):
        response = api_response({"data": [
            {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}
        ]})
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(cli.api, "request_json", return_value=response) as request:
                code, stdout, stderr = run_cli([
                    "edit", "--prompt", "combine",
                    "--image", "https://img.example/one.png",
                    "--image", "https://img.example/two.png",
                    "--output", str(Path(temp_dir) / "result.png"),
                ], {config.API_KEY_ENV: "test-key"})
        self.assertEqual(code, 0, stderr)
        request.assert_called_once()
        payload = request.call_args.args[4]
        self.assertEqual(payload["image"], [
            "https://img.example/one.png", "https://img.example/two.png"
        ])
        self.assertEqual(payload["n"], 1)
        self.assertEqual(json.loads(stdout)["operation"], "edit")

    def test_base64_file_edit_uses_json_objects(self):
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        response = api_response({"data": [{"b64_json": encoded}]})
        with tempfile.TemporaryDirectory() as temp_dir:
            base64_file = Path(temp_dir) / "source.txt"
            base64_file.write_text(encoded, encoding="utf-8")
            with patch.object(cli.api, "request_json", return_value=response) as request:
                code, _stdout, stderr = run_cli([
                    "edit", "--prompt", "combine",
                    "--image", f"data:image/png;base64,{encoded}",
                    "--image-base64-file", str(base64_file),
                    "--output", str(Path(temp_dir) / "result.png"),
                ], {config.API_KEY_ENV: "test-key"})

        self.assertEqual(code, 0, stderr)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[4]["image"], [
            {"b64_json": encoded}, {"b64_json": encoded},
        ])

    def test_long_data_url_is_classified_before_path_exists(self):
        encoded = base64.b64encode(PNG_BYTES + (b"x" * 4096)).decode("ascii")
        with patch.object(cli.Path, "exists", side_effect=AssertionError("path check")):
            inputs = cli.classify_edit_inputs(
                [f"data:image/png;base64,{encoded}"], [], None, False,
            )

        self.assertEqual(inputs.kind, "base64")
        self.assertEqual(inputs.values, [encoded])

    def test_invalid_base64_file_is_a_usage_error_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.txt"
            source.write_text("not-base64%%", encoding="utf-8")
            with patch.object(cli.api, "request_json") as json_request:
                with patch.object(cli.api, "request_multipart") as multipart_request:
                    code, _stdout, stderr = run_cli([
                        "edit", "--prompt", "combine", "--image-base64-file", str(source),
                        "--output", str(Path(temp_dir) / "result.png"),
                    ], {config.API_KEY_ENV: "test-key"})

        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", stderr)
        json_request.assert_not_called()
        multipart_request.assert_not_called()

    def test_async_base64_is_rejected_by_the_shared_classifier(self):
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")

        with self.assertRaisesRegex(api.ApiUsageError, "异步编辑暂不支持 Base64"):
            cli.classify_edit_inputs(
                [f"data:image/png;base64,{encoded}"], [], None, True,
            )

    def test_local_edit_repeats_multipart_image_fields(self):
        response = api_response({"data": [
            {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}
        ]})
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "one.png"
            second = Path(temp_dir) / "two.png"
            first.write_bytes(PNG_BYTES)
            second.write_bytes(PNG_BYTES)
            with patch.object(cli.api, "request_multipart", return_value=response) as request:
                code, _stdout, stderr = run_cli([
                    "edit", "--prompt", "combine", "--image", str(first),
                    "--image", str(second), "--output", str(Path(temp_dir) / "result.png"),
                ], {config.API_KEY_ENV: "test-key"})

        self.assertEqual(code, 0, stderr)
        request.assert_called_once()
        method, url, key, timeout, fields, files = request.call_args.args
        self.assertEqual((method, url, key, timeout), (
            "POST", "https://api.supertoken.cc/v1/images/edits", "test-key", 300,
        ))
        self.assertEqual(
            [field for field, _value in fields],
            ["model", "prompt", "n", "size", "quality"],
        )
        self.assertEqual([(item.field, item.content_type) for item in files], [
            ("image", "image/png"), ("image", "image/png"),
        ])
        self.assertNotIn("Content-Type", request.call_args.kwargs)

    def test_local_mask_is_sent_once(self):
        response = api_response({"data": [
            {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}
        ]})
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            mask = Path(temp_dir) / "mask.png"
            source.write_bytes(PNG_BYTES)
            mask.write_bytes(PNG_BYTES)
            with patch.object(cli.api, "request_multipart", return_value=response) as request:
                code, _stdout, stderr = run_cli([
                    "edit", "--prompt", "combine", "--image", str(source),
                    "--mask", str(mask), "--output", str(Path(temp_dir) / "result.png"),
                ], {config.API_KEY_ENV: "test-key"})

        self.assertEqual(code, 0, stderr)
        request.assert_called_once()
        files = request.call_args.args[5]
        self.assertEqual([item.field for item in files], ["image", "mask"])
        self.assertEqual(files[1].content_type, "image/png")

    def test_mixed_local_and_url_inputs_fail_before_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local = Path(temp_dir) / "source.png"
            local.write_bytes(PNG_BYTES)
            with patch.object(cli.api, "request_json") as json_request:
                with patch.object(cli.api, "request_multipart") as multipart_request:
                    code, _, stderr = run_cli([
                        "edit", "--prompt", "combine",
                        "--image", str(local),
                        "--image", "https://img.example/two.png",
                        "--output", str(Path(temp_dir) / "result.png"),
                    ], {config.API_KEY_ENV: "test-key"})
        self.assertEqual(code, 2)
        self.assertIn("一种", stderr)
        json_request.assert_not_called()
        multipart_request.assert_not_called()

    def test_url_and_base64_inputs_fail_before_request(self):
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        with patch.object(cli.api, "request_json") as json_request:
            with patch.object(cli.api, "request_multipart") as multipart_request:
                code, _stdout, stderr = run_cli([
                    "edit", "--prompt", "combine", "--image", "https://img.example/one.png",
                    "--image", f"data:image/png;base64,{encoded}", "--output", "result.png",
                ], {config.API_KEY_ENV: "test-key"})

        self.assertEqual(code, 2)
        self.assertIn("一种", stderr)
        json_request.assert_not_called()
        multipart_request.assert_not_called()

    def test_sync_url_mask_is_rejected(self):
        with patch.object(cli.api, "request_json") as json_request:
            with patch.object(cli.api, "request_multipart") as multipart_request:
                code, _stdout, stderr = run_cli([
                    "edit", "--prompt", "combine", "--image", "https://img.example/one.png",
                    "--mask", "https://img.example/mask.png", "--output", "result.png",
                ], {config.API_KEY_ENV: "test-key"})

        self.assertEqual(code, 2)
        self.assertIn("Mask", stderr)
        json_request.assert_not_called()
        multipart_request.assert_not_called()

    def test_edit_rejects_more_than_ten_images(self):
        images = ["https://img.example/%s.png" % index for index in range(11)]
        with patch.object(cli.api, "request_json") as json_request:
            with patch.object(cli.api, "request_multipart") as multipart_request:
                code, _stdout, stderr = run_cli([
                    "edit", "--prompt", "combine", *sum((["--image", item] for item in images), []),
                    "--output", "result.png",
                ], {config.API_KEY_ENV: "test-key"})

        self.assertEqual(code, 2)
        self.assertIn("10", stderr)
        json_request.assert_not_called()
        multipart_request.assert_not_called()

    def test_edit_rejects_oversized_local_file_before_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "large.png"
            source.write_bytes(PNG_BYTES)
            source.touch()
            with source.open("r+b") as stream:
                stream.truncate(api.MAX_FILE_BYTES + 1)
            with patch.object(cli.api, "request_json") as json_request:
                with patch.object(cli.api, "request_multipart") as multipart_request:
                    code, _stdout, stderr = run_cli([
                        "edit", "--prompt", "combine", "--image", str(source),
                        "--output", str(Path(temp_dir) / "result.png"),
                    ], {config.API_KEY_ENV: "test-key"})

        self.assertEqual(code, 2)
        self.assertIn("20 MiB", stderr)
        json_request.assert_not_called()
        multipart_request.assert_not_called()

    def test_edit_rejects_unknown_local_signature_before_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.bin"
            source.write_bytes(b"not an image")
            with patch.object(cli.api, "request_json") as json_request:
                with patch.object(cli.api, "request_multipart") as multipart_request:
                    code, _stdout, stderr = run_cli([
                        "edit", "--prompt", "combine", "--image", str(source),
                        "--output", str(Path(temp_dir) / "result.png"),
                    ], {config.API_KEY_ENV: "test-key"})

        self.assertEqual(code, 2)
        self.assertIn("PNG", stderr)
        json_request.assert_not_called()
        multipart_request.assert_not_called()

    def test_local_images_and_mask_over_multipart_limit_fail_before_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            images = []
            for index in range(5):
                image = Path(temp_dir) / f"image-{index}.png"
                image.write_bytes(PNG_BYTES)
                with image.open("r+b") as stream:
                    stream.truncate(api.MAX_FILE_BYTES)
                images.append(image)
            mask = Path(temp_dir) / "mask.png"
            mask.write_bytes(PNG_BYTES)
            with mask.open("r+b") as stream:
                stream.truncate(api.MAX_FILE_BYTES)
            argv = ["edit", "--prompt", "combine", "--output", str(Path(temp_dir) / "result.png")]
            for image in images:
                argv.extend(["--image", str(image)])
            argv.extend(["--mask", str(mask)])
            with patch.object(cli.api, "request_json") as json_request:
                with patch.object(cli.api, "request_multipart") as multipart_request:
                    code, _stdout, stderr = run_cli(argv, {config.API_KEY_ENV: "test-key"})

        self.assertEqual(code, 2)
        self.assertIn("100 MiB", stderr)
        json_request.assert_not_called()
        multipart_request.assert_not_called()

    def test_successful_edit_saves_every_item(self):
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        response = api_response({"data": [{"b64_json": encoded}, {"b64_json": encoded}]})
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            with patch.object(cli.api, "request_json", return_value=response) as request:
                code, stdout, stderr = run_cli([
                    "edit", "--prompt", "combine", "--image", "https://img.example/one.png",
                    "--model", "gpt-image-2", "--n", "2", "--output", str(output),
                ], {config.API_KEY_ENV: "test-key"})

            result = json.loads(stdout)
            self.assertEqual(code, 0, stderr)
            request.assert_called_once()
            self.assertEqual(request.call_args.args[0:4], (
                "POST", "https://api.supertoken.cc/v1/images/edits", "test-key", 300,
            ))
            self.assertEqual(result["operation"], "edit")
            self.assertEqual(len(result["outputs"]), 2)
            self.assertEqual(Path(result["outputs"][0]["path"]).read_bytes(), PNG_BYTES)
            self.assertEqual(Path(result["outputs"][1]["path"]).read_bytes(), PNG_BYTES)

    def test_count_model_rejects_multiple_sync_edit_results_before_request(self):
        cases = [
            ["--image", "https://img.example/one.png"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(PNG_BYTES)
            cases.append(["--image", str(source)])
            for input_args in cases:
                with self.subTest(input_args=input_args):
                    with patch.object(cli.api, "request_json") as json_request:
                        with patch.object(cli.api, "request_multipart") as multipart_request:
                            code, _stdout, stderr = run_cli([
                                "edit", "--prompt", "combine", *input_args,
                                "--n", "2", "--output", str(Path(temp_dir) / "result.png"),
                            ], {config.API_KEY_ENV: "test-key"})
                    self.assertEqual(code, 2)
                    self.assertIn("gpt-image-2-count", stderr)
                    json_request.assert_not_called()
                    multipart_request.assert_not_called()

    def test_sync_command_rejects_a_result_count_different_from_requested(self):
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        response = api_response({"data": [{"b64_json": encoded}]})
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            with patch.object(cli.api, "request_json", return_value=response):
                code, stdout, stderr = run_cli([
                    "generate", "--prompt", "cat", "--model", "gpt-image-2",
                    "--n", "2", "--output", str(output),
                ], {config.API_KEY_ENV: "test-key"})

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("2", stderr)
            self.assertFalse(output.exists())


class AsyncTaskTests(unittest.TestCase):
    def test_async_url_mask_requires_a_nonempty_network_location(self):
        with self.assertRaisesRegex(api.ApiUsageError, "URL Mask"):
            cli.classify_edit_inputs(
                ["https://img.example/one.png"], [], "https:///mask.png", True
            )

    def test_async_payloads_use_documented_json_field_locations(self):
        args = SimpleNamespace(
            model="gpt-image-2", prompt="rainy street", count=2,
            size="1024x1024", quality="low", output_format="png",
            output_compression=None, background=None,
            client_reference_id=None, metadata=None,
        )
        self.assertEqual(
            cli.build_async_generation_payload(args),
            {
                "model": "gpt-image-2",
                "operation": "generation",
                "input": {"prompt": "rainy street"},
                "output": {
                    "count": 2,
                    "size": "1024x1024",
                    "quality": "low",
                    "format": "png",
                },
            },
        )

        args.output_compression = 80
        args.background = "transparent"
        args.client_reference_id = "client-7"
        args.metadata = {"campaign": "launch"}
        inputs = cli.EditInputs(
            "url",
            ["https://img.example/one.png", "https://img.example/two.png"],
            "https://img.example/mask.png",
        )
        self.assertEqual(
            cli.build_async_url_edit_payload(args, inputs),
            {
                "model": "gpt-image-2",
                "operation": "edit",
                "input": {
                    "prompt": "rainy street",
                    "images": [
                        {"url": "https://img.example/one.png"},
                        {"url": "https://img.example/two.png"},
                    ],
                    "mask": {"url": "https://img.example/mask.png"},
                },
                "output": {
                    "count": 2,
                    "size": "1024x1024",
                    "quality": "low",
                    "format": "png",
                    "compression": 80,
                    "background": "transparent",
                },
                "client_reference_id": "client-7",
                "metadata": {"campaign": "launch"},
            },
        )

    def test_local_async_edit_uses_documented_multipart_fields(self):
        response = api_response({"id": "task_local", "status": "queued"}, 202)
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.png"
            second = Path(temp_dir) / "second.png"
            mask = Path(temp_dir) / "mask.png"
            first.write_bytes(PNG_BYTES)
            second.write_bytes(PNG_BYTES)
            mask.write_bytes(PNG_BYTES)
            with patch.object(cli.api, "request_multipart", return_value=response) as request:
                code, stdout, stderr = run_cli([
                    "edit", "--async", "--prompt", "combine",
                    "--image", str(first), "--image", str(second),
                    "--mask", str(mask),
                    "--model", "gpt-image-2", "--n", "2",
                    "--format", "webp", "--background", "opaque",
                    "--output-compression", "72",
                    "--client-reference-id", "client-local",
                    "--metadata-json", '{"source":"studio"}',
                    "--idempotency-key", "idem-local",
                ], {config.API_KEY_ENV: "model-key"})

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["task_id"], "task_local")
        method, url, key, timeout, fields, files = request.call_args.args
        self.assertEqual((method, url, key, timeout), (
            "POST", "https://api.supertoken.cc/v1/image/tasks", "model-key", 300,
        ))
        self.assertEqual(fields, [
            ("model", "gpt-image-2"), ("operation", "edit"),
            ("prompt", "combine"), ("n", 2), ("size", "1024x1024"),
            ("quality", "low"), ("output_format", "webp"),
            ("output_compression", 72), ("background", "opaque"),
            ("client_reference_id", "client-local"),
            ("metadata", '{"source": "studio"}'),
        ])
        self.assertEqual(
            [item.field for item in files], ["image", "image", "mask"]
        )
        self.assertEqual(request.call_args.kwargs["headers"], {
            "Idempotency-Key": "idem-local",
        })

    def test_create_only_reports_task_headers_and_does_not_read_resource_key(self):
        response = api_response(
            {
                "id": "task_create", "status": "queued", "progress": 0,
                "result": {"images": [{"b64_json": "server-base64-secret"}]},
                "arbitrary": "sk-serversecret123",
            },
            202,
            {
                "Location": (
                    "https://user:pass@api.example.test/v1/image/tasks/"
                    "sk-serversecret123/task_create"
                    "?signature=signed-secret#fragment"
                ),
                "Retry-After": "7",
            },
        )
        with patch.object(cli.uuid, "uuid4", return_value=SimpleNamespace(hex="generated-key")):
            with patch.object(cli.api, "request_json", return_value=response) as request:
                with patch.object(cli, "get_api_key", wraps=cli.get_api_key) as get_key:
                    code, stdout, stderr = run_cli([
                        "generate", "--async", "--prompt", "cat",
                    ], {config.API_KEY_ENV: "model-key"})

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {
            "mode": "async",
            "operation": "generation",
            "model": "gpt-image-2-count",
            "task_id": "task_create",
            "status": "queued",
            "progress": 0,
            "idempotency_key": "generated-key",
            "location": (
                "https://api.example.test/v1/image/tasks/[REDACTED]/task_create"
            ),
            "retry_after": 7,
        })
        self.assertNotIn("server-base64-secret", stdout)
        self.assertNotIn("sk-serversecret123", stdout)
        self.assertNotIn("signed-secret", stdout)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["headers"], {
            "Idempotency-Key": "generated-key",
        })
        self.assertTrue(all(
            call.args[0] != cli.RESOURCE_KEY for call in get_key.call_args_list
        ))

    def test_async_create_accepts_documented_null_result_and_error(self):
        response = api_response({
            "id": "task_documented_create",
            "object": "image.task",
            "model": "gpt-image-2-count",
            "operation": "generation",
            "status": "queued",
            "progress": 0,
            "result": None,
            "error": None,
            "usage": {},
        }, 202)
        with patch.object(cli.api, "request_json", return_value=response):
            code, stdout, stderr = run_cli([
                "generate", "--async", "--prompt", "cat",
                "--idempotency-key", "documented-create",
            ], {config.API_KEY_ENV: "model-key"})

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {
            "mode": "async",
            "operation": "generation",
            "model": "gpt-image-2-count",
            "task_id": "task_documented_create",
            "status": "queued",
            "progress": 0,
            "idempotency_key": "documented-create",
        })

    def test_create_rejects_malformed_task_id_without_reporting_it(self):
        for malformed_id in ("bad/sk-model123456", "task_sk-serversecret123"):
            with self.subTest(malformed_id=malformed_id):
                response = api_response(
                    {"id": malformed_id, "status": "queued"}, 202
                )
                with patch.object(
                    cli.api, "request_json", return_value=response
                ) as request:
                    code, stdout, stderr = run_cli([
                        "generate", "--async", "--prompt", "cat",
                        "--idempotency-key", "request-key",
                    ], {config.API_KEY_ENV: "model-key"})

                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(request.call_count, 1)
                self.assertIn("任务 ID", stderr)
                self.assertNotIn(malformed_id, stderr)

    def test_idempotency_key_accepts_only_ascii_http_vchar(self):
        invalid = [
            "", " ", "has space", "tab\tvalue", "line\nvalue", "nul\x00value",
            "delete\x7fvalue", "unicode-\u96ea", "x" * 129,
        ]
        for value in invalid:
            with self.subTest(value=repr(value)):
                with patch.object(cli.api, "request_json") as request:
                    code, _stdout, stderr = run_cli([
                        "generate", "--async", "--prompt", "cat",
                        "--idempotency-key", value,
                    ], {config.API_KEY_ENV: "model-key"})
                self.assertEqual(code, 2)
                self.assertIn("Idempotency-Key", stderr)
                request.assert_not_called()

        valid = "!request-key~"
        response = api_response({"id": "task_valid_key", "status": "queued"}, 202)
        with patch.object(cli.api, "request_json", return_value=response) as request:
            code, stdout, stderr = run_cli([
                "generate", "--async", "--prompt", "cat",
                "--idempotency-key", valid,
            ], {config.API_KEY_ENV: "model-key"})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["idempotency_key"], valid)
        self.assertEqual(request.call_args.kwargs["headers"], {
            "Idempotency-Key": valid,
        })

    def test_active_api_key_is_redacted_when_reused_as_idempotency_key(self):
        active_key = "sk-modelsecret123"
        success = api_response(
            {"id": "task_safe_idempotency", "status": "queued"}, 202
        )
        with patch.object(cli.api, "request_json", return_value=success):
            code, stdout, stderr = run_cli([
                "generate", "--async", "--prompt", "cat",
                "--idempotency-key", active_key,
            ], {config.API_KEY_ENV: active_key})

        self.assertEqual(code, 0, stderr)
        self.assertNotIn(active_key, stdout)
        self.assertEqual(json.loads(stdout)["idempotency_key"], "[REDACTED]")

        with patch.object(
            cli.api, "request_json", side_effect=urllib.error.URLError("offline")
        ):
            code, stdout, stderr = run_cli([
                "generate", "--async", "--prompt", "cat",
                "--idempotency-key", active_key,
            ], {config.API_KEY_ENV: active_key})

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertNotIn(active_key, stderr)
        self.assertIn("[REDACTED]", stderr)

    def test_async_create_rejects_malformed_summary_fields(self):
        malformed = [
            {"id": "task_bad", "status": "unknown"},
            {"id": "task_bad", "status": "queued", "progress": True},
            {"id": "task_bad", "status": "queued", "progress": 101},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                with patch.object(
                    cli.api, "request_json", return_value=api_response(payload, 202)
                ) as request:
                    code, stdout, stderr = run_cli([
                        "generate", "--async", "--prompt", "cat",
                        "--idempotency-key", "request-key",
                    ], {config.API_KEY_ENV: "model-key"})
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("异步创建响应", stderr)
                self.assertNotIn("Traceback", stderr)
                request.assert_called_once()

    def test_async_wait_uses_model_key_for_post_resource_key_for_gets_and_saves_all(self):
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        responses = [
            api_response(
                {"id": "task_wait", "status": "queued", "progress": 0}, 202,
                {"Retry-After": "2"},
            ),
            api_response({"id": "task_wait", "status": "queued", "progress": 5},
                         headers={"Retry-After": "3"}),
            api_response({"id": "task_wait", "status": "in_progress", "progress": 60},
                         headers={"Retry-After": "4"}),
            api_response({
                "id": "task_wait", "status": "succeeded", "progress": 100,
                "model": "gpt-image-2", "operation": "generation",
                "result": {"images": [
                    {"b64_json": encoded}, {"b64_json": encoded},
                ]},
            }),
        ]
        sleeps = []
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            with patch.object(cli.api, "request_json", side_effect=responses) as request:
                defaults = (2, sleeps.append, cli.time.monotonic)
                with patch.object(cli.poll_task, "__defaults__", defaults):
                    code, stdout, stderr = run_cli([
                        "generate", "--async", "--wait", "--prompt", "cat",
                        "--model", "gpt-image-2", "--n", "2",
                        "--output", str(output),
                        "--idempotency-key", "provided-key",
                    ], {
                        config.API_KEY_ENV: "model-key",
                        config.RESOURCE_API_KEY_ENV: "resource-key",
                    })

            result = json.loads(stdout)
            self.assertEqual(code, 0, stderr)
            self.assertEqual([call.args[0] for call in request.call_args_list], [
                "POST", "GET", "GET", "GET",
            ])
            self.assertEqual([call.args[2] for call in request.call_args_list], [
                "model-key", "resource-key", "resource-key", "resource-key",
            ])
            self.assertEqual(sleeps, [3, 4])
            self.assertEqual(result["mode"], "async")
            self.assertEqual(result["task_id"], "task_wait")
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["idempotency_key"], "provided-key")
            self.assertEqual(len(result["outputs"]), 2)
            self.assertEqual(Path(result["outputs"][0]["path"]).read_bytes(), PNG_BYTES)
            self.assertEqual(Path(result["outputs"][1]["path"]).read_bytes(), PNG_BYTES)

    def test_retry_delay_falls_back_and_clamps_without_traceback(self):
        self.assertEqual(cli.retry_delay(None, 9), 9)
        self.assertEqual(cli.retry_delay("not-an-int", 11), 11)
        self.assertEqual(cli.retry_delay("1"), 2)
        self.assertEqual(cli.retry_delay("60"), 30)

    def test_polling_missing_or_invalid_retry_after_keeps_current_interval(self):
        sleeps = []
        sequence = [
            ({"id": "task_interval", "status": "queued"}, {"Retry-After": "7"}),
            ({"id": "task_interval", "status": "in_progress"}, {}),
            ({"id": "task_interval", "status": "in_progress"},
             {"Retry-After": "not-an-int"}),
            ({"id": "task_interval", "status": "succeeded"}, {}),
        ]
        with patch.object(cli, "query_task", side_effect=sequence):
            result = cli.poll_task(
                "https://api.example", "resource-key", "task_interval", 5, 100,
                sleep=sleeps.append, monotonic=lambda: 0,
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(sleeps, [7, 7, 7])

    def test_polling_limits_get_and_sleep_to_one_deadline(self):
        time_values = iter([0.0, 0.0, 7.0, 9.0, 10.0])
        timeouts = []
        sleeps = []

        def query(_base, _key, _task_id, timeout, **_deadline_options):
            timeouts.append(timeout)
            return {"id": "task_deadline", "status": "queued"}, {}

        with patch.object(cli, "query_task", side_effect=query):
            with self.assertRaisesRegex(api.ApiResponseError, "task_deadline"):
                cli.poll_task(
                    "https://api.example", "resource-key", "task_deadline",
                    timeout=30, wait_timeout=10, initial_retry_after=5,
                    sleep=sleeps.append, monotonic=lambda: next(time_values),
                )

        self.assertEqual(timeouts, [10.0, 1.0])
        self.assertEqual(sleeps, [3.0])

    def test_task_body_slow_drip_honors_polling_absolute_deadline(self):
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        class SlowDripResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __init__(self, body, clock):
                self._stream = io.BytesIO(body)
                self.clock = clock

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                return self._stream.read(size)

            def read1(self, size=-1):
                self.clock.now += 0.6
                return self._stream.read(min(size, 1))

        clock = Clock()
        body = json.dumps({
            "id": "task_slow_body", "status": "succeeded"
        }).encode("utf-8")
        response = SlowDripResponse(body, clock)
        with patch.object(cli.api, "_open_url", return_value=response):
            with self.assertRaises(api.ApiResponseError) as raised:
                cli.poll_task(
                    "https://api.example.test",
                    "resource-key",
                    "task_slow_body",
                    timeout=30,
                    wait_timeout=1,
                    monotonic=clock,
                    deadline=1.0,
                )

        self.assertEqual(
            str(raised.exception), "等待任务 task_slow_body 超过 1 秒。"
        )

    def test_wait_deadline_also_limits_result_downloads_and_rolls_back(self):
        task = {
            "id": "task_download_deadline",
            "status": "succeeded",
            "result": {
                "images": [
                    {"url": "https://images.example.test/one.png"},
                    {"url": "https://images.example.test/two.png"},
                ]
            },
        }
        time_values = iter([0.0, 8.0, 9.0, 9.5, 10.1])
        download_timeouts = []

        def download(_url, timeout, **_deadline_options):
            download_timeouts.append(timeout)
            return PNG_BYTES

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            with patch.object(cli, "poll_task", return_value=task):
                with patch.object(cli.api, "download_image", side_effect=download):
                    with patch.object(
                        cli.time, "monotonic", side_effect=lambda: next(time_values)
                    ):
                        code, stdout, stderr = run_cli([
                            "wait", "task_download_deadline",
                            "--output", str(output),
                            "--timeout", "30", "--wait-timeout", "10",
                        ], {config.RESOURCE_API_KEY_ENV: "resource-key"})

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(
                stderr.strip(),
                "等待任务 task_download_deadline 超过 10 秒。",
            )
            self.assertEqual(download_timeouts, [2.0, 0.5])
            self.assertFalse(output.exists())
            self.assertFalse(output.with_name("result-1.png").exists())
            self.assertFalse(output.with_name("result-2.png").exists())

    def test_nonpositive_wait_timeout_is_usage_error_before_request(self):
        commands = [
            ["wait", "task_deadline", "--output", "unused.png", "--wait-timeout", "0"],
            [
                "generate", "--async", "--wait", "--prompt", "cat",
                "--output", "unused.png", "--wait-timeout", "-1",
            ],
        ]
        environment = {
            config.API_KEY_ENV: "model-key",
            config.RESOURCE_API_KEY_ENV: "resource-key",
        }
        for argv in commands:
            with self.subTest(argv=argv):
                with patch.object(cli.api, "request_json") as request:
                    code, _stdout, stderr = run_cli(argv, environment)
                self.assertEqual(code, 2)
                self.assertIn("--wait-timeout", stderr)
                request.assert_not_called()

    def test_task_failed_redacts_server_supplied_credentials(self):
        error = cli.TaskFailed(
            "task_failed",
            {
                "id": "task_failed",
                "error": {
                    "code": "blocked",
                    "message": (
                        "sk-model123456 ak_resource123456 "
                        "wk-webhook123456 explicit-resource-secret"
                    ),
                    "retryable": True,
                },
            },
            ("explicit-resource-secret",),
        )
        text = str(error)
        for secret in (
            "sk-model123456",
            "ak_resource123456",
            "wk-webhook123456",
            "explicit-resource-secret",
        ):
            self.assertNotIn(secret, text)
        self.assertIn("blocked", text)
        self.assertIn("retryable=True", text)

    def test_failed_task_uses_requested_id_and_redacts_every_error_field(self):
        secrets = (
            "sk-model123456",
            "ak_resource123456",
            "wk-webhook123456",
            "explicit-resource-secret",
        )
        task = {
            "id": secrets[0],
            "status": "failed",
            "error": {
                "code": secrets[1],
                "message": secrets[2],
                "retryable": secrets[3],
            },
        }
        with patch.object(cli, "query_task", return_value=(task, {})):
            with self.assertRaises(api.ApiResponseError) as raised:
                cli.poll_task(
                    "https://api.example", secrets[3], "task_requested", 5, 100,
                    sleep=lambda _seconds: None, monotonic=lambda: 0,
                )

        text = str(raised.exception)
        self.assertIn("task_requested", text)
        for secret in secrets:
            self.assertNotIn(secret, text)

    def test_failed_task_with_non_object_error_is_controlled(self):
        task = {"id": "task_server", "status": "failed", "error": ["bad"]}
        with patch.object(cli, "query_task", return_value=(task, {})):
            with self.assertRaisesRegex(api.ApiResponseError, "task_requested"):
                cli.poll_task(
                    "https://api.example", "resource-key", "task_requested", 5, 100,
                    sleep=lambda _seconds: None, monotonic=lambda: 0,
                )

    def test_wait_rejects_non_object_result_without_a_traceback(self):
        response = api_response({
            "id": "task_requested", "status": "succeeded", "result": [],
        })
        with patch.object(cli.api, "request_json", return_value=response) as request:
            code, _stdout, stderr = run_cli([
                "wait", "task_requested", "--output", "unused.png",
            ], {config.RESOURCE_API_KEY_ENV: "resource-key"})

        self.assertEqual(code, 1)
        self.assertEqual(request.call_count, 1)
        self.assertIn("task_requested", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_wait_rejects_a_mismatched_server_task_id(self):
        server_id = "task_server_secret"
        response = api_response({
            "id": server_id,
            "status": "succeeded",
            "result": {
                "images": [{
                    "b64_json": base64.b64encode(PNG_BYTES).decode("ascii"),
                }],
            },
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            with patch.object(cli.api, "request_json", return_value=response):
                code, stdout, stderr = run_cli([
                    "wait", "task_requested", "--output", str(output),
                ], {config.RESOURCE_API_KEY_ENV: "resource-key"})

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("task_requested", stderr)
        self.assertNotIn(server_id, stdout)
        self.assertNotIn(server_id, stderr)

    def test_wait_accepts_documented_nullable_task_fields(self):
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        responses = [
            api_response({
                "id": "task_documented_wait",
                "status": "queued",
                "progress": 0,
                "result": None,
                "error": None,
            }, headers={"Retry-After": "2"}),
            api_response({
                "id": "task_documented_wait",
                "status": "succeeded",
                "progress": 100,
                "result": {"images": [{"b64_json": encoded}]},
                "error": None,
            }),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            with patch.object(cli.api, "request_json", side_effect=responses):
                defaults = (2, lambda _seconds: None, cli.time.monotonic)
                with patch.object(cli.poll_task, "__defaults__", defaults):
                    code, stdout, stderr = run_cli([
                        "wait", "task_documented_wait",
                        "--output", str(output),
                    ], {config.RESOURCE_API_KEY_ENV: "resource-key"})

            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "succeeded")
            self.assertEqual(output.read_bytes(), PNG_BYTES)

    def test_task_stdout_is_a_fixed_allowlist_summary(self):
        secret = "sk-serversecret123"
        response = api_response({
            "id": "task_requested",
            "status": "succeeded",
            "progress": 100,
            "result": {
                "images": [{
                    "url": "https://cdn.example/image.png?signature=signed-secret",
                    "b64_json": "base64-secret",
                }],
            },
            "unknown": secret,
        })
        with patch.object(cli.api, "request_json", return_value=response):
            code, stdout, stderr = run_cli(
                ["task", "task_requested"],
                {config.RESOURCE_API_KEY_ENV: "resource-key"},
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {
            "task_id": "task_requested", "status": "succeeded", "progress": 100,
        })
        for value in (secret, "signed-secret", "base64-secret"):
            self.assertNotIn(value, stdout)

    def test_task_query_accepts_documented_null_error(self):
        response = api_response({
            "id": "task_documented_query",
            "status": "succeeded",
            "progress": 100,
            "result": {"images": []},
            "error": None,
        })
        with patch.object(cli.api, "request_json", return_value=response):
            code, stdout, stderr = run_cli(
                ["task", "task_documented_query"],
                {config.RESOURCE_API_KEY_ENV: "resource-key"},
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {
            "task_id": "task_documented_query",
            "status": "succeeded",
            "progress": 100,
        })

    def test_task_failed_stdout_contains_only_sanitized_error_summary(self):
        response = api_response({
            "id": "task_failed",
            "status": "failed",
            "error": {
                "code": "blocked-sk-serversecret123",
                "message": "policy ak_resource123456",
                "retryable": False,
            },
        })
        with patch.object(cli.api, "request_json", return_value=response):
            code, stdout, stderr = run_cli(
                ["task", "task_failed"],
                {config.RESOURCE_API_KEY_ENV: "resource-key"},
            )

        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(set(summary), {"task_id", "status", "error"})
        self.assertEqual(set(summary["error"]), {"code", "message", "retryable"})
        self.assertNotIn("sk-serversecret123", stdout)
        self.assertNotIn("ak_resource123456", stdout)

    def test_task_query_rejects_missing_id_and_malformed_status_fields(self):
        malformed = [
            {"status": "queued"},
            {"id": "task_requested", "status": "other"},
            {"id": "task_requested", "status": "queued", "progress": False},
            {
                "id": "task_requested", "status": "failed",
                "error": {"code": 7, "message": "bad", "retryable": False},
            },
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                with patch.object(
                    cli.api, "request_json", return_value=api_response(payload)
                ):
                    code, stdout, stderr = run_cli(
                        ["task", "task_requested"],
                        {config.RESOURCE_API_KEY_ENV: "resource-key"},
                    )
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("task_requested", stderr)
                self.assertNotIn("Traceback", stderr)

    def test_wait_reports_failed_task_without_resubmitting(self):
        failed = api_response({
            "id": "task_failed", "status": "failed",
            "error": {"code": "blocked", "message": "policy", "retryable": False},
        })
        with patch.object(cli.api, "request_json", return_value=failed) as request:
            code, _stdout, stderr = run_cli([
                "wait", "task_failed", "--output", "unused.png",
            ], {config.RESOURCE_API_KEY_ENV: "resource-key"})

        self.assertEqual(code, 1)
        self.assertIn("task_failed", stderr)
        self.assertIn("blocked", stderr)
        self.assertIn("policy", stderr)
        self.assertIn("retryable=False", stderr)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET"])

    def test_polling_retries_only_bounded_transient_failures_and_resets_after_success(self):
        transient = api.ApiResponseError("transient")
        transient.status = 503
        transient.headers = {"Retry-After": "2"}
        sequence = [
            urllib.error.URLError("offline"),
            urllib.error.URLError("offline"),
            urllib.error.URLError("offline"),
            ({"id": "task_retry", "status": "queued"}, {"Retry-After": "2"}),
            transient, transient, transient,
            ({"id": "task_retry", "status": "succeeded"}, {}),
        ]
        with patch.object(cli, "query_task", side_effect=sequence) as query:
            result = cli.poll_task(
                "https://api.example", "resource-key", "task_retry", 5, 100,
                sleep=lambda _seconds: None, monotonic=lambda: 0,
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(query.call_count, 8)

        cases = [urllib.error.URLError("offline")]
        for status in (429, 502, 503):
            error = api.ApiResponseError("transient")
            error.status = status
            error.headers = {}
            cases.append(error)
        for error in cases:
            with self.subTest(error=error):
                with patch.object(cli, "query_task", side_effect=[error] * 4) as query:
                    with self.assertRaises(api.ApiResponseError) as raised:
                        cli.poll_task(
                            "https://api.example", "resource-key", "task_retry", 5, 100,
                            sleep=lambda _seconds: None, monotonic=lambda: 0,
                        )
                self.assertEqual(query.call_count, 4)
                self.assertIn("task_retry", str(raised.exception))

    def test_malformed_successful_task_response_retains_task_id(self):
        response = api.ApiResponse(200, {}, b"not-json")
        with patch.object(cli.api, "request_json", return_value=response) as request:
            with self.assertRaises(api.ApiResponseError) as raised:
                cli.poll_task(
                    "https://api.example", "resource-key", "task_malformed", 5, 100,
                    sleep=lambda _seconds: None, monotonic=lambda: 0,
                )
        self.assertEqual(request.call_count, 1)
        self.assertIn("task_malformed", str(raised.exception))
        self.assertIn("非 JSON", str(raised.exception))

    def test_task_command_wraps_malformed_response_with_id_and_redaction(self):
        secrets = (
            "explicit-resource-secret",
            "sk-model123456",
            "ak_resource123456",
            "wk-webhook123456",
        )
        response = api.ApiResponse(
            200,
            {},
            ("not-json " + " ".join(secrets)).encode("utf-8"),
        )
        with patch.object(cli.api, "request_json", return_value=response) as request:
            code, _stdout, stderr = run_cli([
                "task", "task_requested",
                "--resource-api-key", secrets[0],
            ])

        self.assertEqual(code, 1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertIn("task_requested", stderr)
        self.assertIn("非 JSON", stderr)
        self.assertNotIn("Traceback", stderr)
        for secret in secrets:
            self.assertNotIn(secret, stderr)

    def test_task_command_wraps_url_error_with_id_and_redaction(self):
        secrets = (
            "explicit-resource-secret",
            "sk-model123456",
            "ak_resource123456",
            "wk-webhook123456",
        )
        failure = urllib.error.URLError("connection " + " ".join(secrets))
        with patch.object(cli.api, "request_json", side_effect=failure) as request:
            code, _stdout, stderr = run_cli([
                "task", "task_requested",
                "--resource-api-key", secrets[0],
            ])

        self.assertEqual(code, 1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertIn("task_requested", stderr)
        self.assertIn("connection", stderr)
        self.assertNotIn("Traceback", stderr)
        for secret in secrets:
            self.assertNotIn(secret, stderr)

    def test_query_rejects_invalid_ids_and_non_transient_http_fails_immediately(self):
        with patch.object(cli.api, "request_json") as request:
            with self.assertRaises(api.ApiUsageError):
                cli.query_task("https://api.example", "resource-key", "bad/id", 5)
        request.assert_not_called()

        response = api_response({"error": "forbidden"}, 403)
        with patch.object(cli.api, "request_json", return_value=response) as request:
            with self.assertRaises(api.ApiResponseError) as raised:
                cli.query_task(
                    "https://api.example", "explicit-resource-key", "task_valid", 5,
                )
        self.assertEqual(raised.exception.status, 403)
        self.assertNotIn("explicit-resource-key", str(raised.exception))
        self.assertEqual(request.call_count, 1)

    def test_task_query_rejects_a_schema_shaped_redirect_response(self):
        response = api_response(
            {"id": "task_requested", "status": "queued"},
            302,
            {"Location": "https://other.example/task_requested"},
        )
        with patch.object(cli.api, "request_json", return_value=response) as request:
            code, stdout, stderr = run_cli(
                ["task", "task_requested"],
                {config.RESOURCE_API_KEY_ENV: "resource-key"},
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("HTTP 302", stderr)
        request.assert_called_once()

    def test_post_failure_is_not_retried_and_prints_recovery_key(self):
        with patch.object(cli.api, "request_json", side_effect=urllib.error.URLError("offline")) as request:
            code, _stdout, stderr = run_cli([
                "generate", "--async", "--prompt", "cat",
                "--idempotency-key", "recovery-key",
            ], {config.API_KEY_ENV: "model-key"})

        self.assertEqual(code, 1)
        self.assertEqual(request.call_count, 1)
        self.assertIn("recovery-key", stderr)

    def test_transport_failure_stderr_redacts_keys_and_url_secrets(self):
        explicit = "explicit-model-secret"
        signed_url = (
            "https://user:pass@example.test/path?signature=signed-secret#fragment"
        )
        failure = urllib.error.URLError(
            f"connection {explicit} sk-serversecret123 {signed_url}"
        )
        with patch.object(cli.api, "request_json", side_effect=failure) as request:
            code, stdout, stderr = run_cli([
                "generate", "--prompt", "cat", "--output", "unused.png",
                "--api-key", explicit,
            ])

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(request.call_count, 1)
        self.assertIn("https://example.test/path", stderr)
        for forbidden in (
            explicit, "sk-serversecret123", "user:pass", "signed-secret", "fragment",
        ):
            self.assertNotIn(forbidden, stderr)

    def test_resource_key_and_async_option_validation_happen_before_requests(self):
        cases = [
            ["task", "task_missing"],
            ["wait", "task_missing", "--output", "unused.png"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with patch.object(cli.api, "request_json") as request:
                    code, _stdout, stderr = run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn(config.RESOURCE_API_KEY_ENV, stderr)
                request.assert_not_called()

        invalid_async = [
            ["generate", "--async", "--prompt", "cat", "--idempotency-key", "x" * 129],
            ["generate", "--async", "--prompt", "cat", "--client-reference-id", "x" * 192],
            ["generate", "--async", "--prompt", "cat", "--output-compression", "101"],
            ["generate", "--async", "--prompt", "cat", "--metadata-json", "[]"],
        ]
        for argv in invalid_async:
            with self.subTest(argv=argv):
                with patch.object(cli.api, "request_json") as request:
                    code, _stdout, _stderr = run_cli(argv, {config.API_KEY_ENV: "model-key"})
                self.assertEqual(code, 2)
                request.assert_not_called()

        with patch.object(cli.api, "request_json") as request:
            code, _stdout, stderr = run_cli([
                "generate", "--async", "--wait", "--prompt", "cat",
                "--output", "unused.png",
            ], {config.API_KEY_ENV: "model-key"})
        self.assertEqual(code, 2)
        self.assertIn(config.RESOURCE_API_KEY_ENV, stderr)
        request.assert_not_called()

    def test_async_base64_edit_exits_two_before_any_request(self):
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        with patch.object(cli.api, "request_json") as json_request:
            with patch.object(cli.api, "request_multipart") as multipart_request:
                code, _stdout, stderr = run_cli([
                    "edit", "--async", "--prompt", "combine",
                    "--image", f"data:image/png;base64,{encoded}",
                ], {config.API_KEY_ENV: "model-key"})
        self.assertEqual(code, 2)
        self.assertIn("Base64", stderr)
        json_request.assert_not_called()
        multipart_request.assert_not_called()
