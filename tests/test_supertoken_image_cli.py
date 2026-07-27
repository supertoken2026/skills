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
        payload = request.call_args.args[4]
        self.assertEqual(payload["image"], [
            "https://img.example/one.png", "https://img.example/two.png"
        ])
        self.assertEqual(json.loads(stdout)["operation"], "edit")

    def test_data_url_and_base64_file_edit_use_json_objects(self):
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
        method, url, key, timeout, fields, files = request.call_args.args
        self.assertEqual((method, url, key, timeout), (
            "POST", "https://api.supertoken.cc/v1/images/edits", "test-key", 300,
        ))
        self.assertEqual([field for field, _value in fields], ["model", "prompt", "size", "quality"])
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
                    "--output", str(output),
                ], {config.API_KEY_ENV: "test-key"})

            result = json.loads(stdout)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(request.call_args.args[0:4], (
                "POST", "https://api.supertoken.cc/v1/images/edits", "test-key", 300,
            ))
            self.assertEqual(result["operation"], "edit")
            self.assertEqual(len(result["outputs"]), 2)
            self.assertEqual(Path(result["outputs"][0]["path"]).read_bytes(), PNG_BYTES)
            self.assertEqual(Path(result["outputs"][1]["path"]).read_bytes(), PNG_BYTES)


class AsyncTaskTests(unittest.TestCase):
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
            source = Path(temp_dir) / "source.png"
            mask = Path(temp_dir) / "mask.png"
            source.write_bytes(PNG_BYTES)
            mask.write_bytes(PNG_BYTES)
            with patch.object(cli.api, "request_multipart", return_value=response) as request:
                code, stdout, stderr = run_cli([
                    "edit", "--async", "--prompt", "combine",
                    "--image", str(source), "--mask", str(mask),
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
        self.assertEqual([item.field for item in files], ["image", "mask"])
        self.assertEqual(request.call_args.kwargs["headers"], {
            "Idempotency-Key": "idem-local",
        })

    def test_create_only_reports_task_headers_and_does_not_read_resource_key(self):
        response = api_response(
            {"id": "task_create", "status": "queued", "progress": 0},
            202,
            {"Location": "/v1/image/tasks/task_create", "Retry-After": "7"},
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
            "location": "/v1/image/tasks/task_create",
            "retry_after": "7",
        })
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["headers"], {
            "Idempotency-Key": "generated-key",
        })
        self.assertTrue(all(
            call.args[0] != cli.RESOURCE_KEY for call in get_key.call_args_list
        ))

    def test_provided_empty_idempotency_key_is_not_replaced(self):
        response = api_response({"id": "task_empty_key", "status": "queued"}, 202)
        with patch.object(cli.uuid, "uuid4", side_effect=AssertionError("uuid")):
            with patch.object(cli.api, "request_json", return_value=response) as request:
                code, stdout, stderr = run_cli([
                    "generate", "--async", "--prompt", "cat",
                    "--idempotency-key", "",
                ], {config.API_KEY_ENV: "model-key"})

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["idempotency_key"], "")
        self.assertEqual(request.call_args.kwargs["headers"], {
            "Idempotency-Key": "",
        })

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
                        "--model", "gpt-image-2", "--output", str(output),
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

    def test_task_failed_redacts_server_supplied_credentials(self):
        error = cli.TaskFailed(
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
                    with self.assertRaises(type(error)):
                        cli.poll_task(
                            "https://api.example", "resource-key", "task_retry", 5, 100,
                            sleep=lambda _seconds: None, monotonic=lambda: 0,
                        )
                self.assertEqual(query.call_count, 4)

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

    def test_post_failure_is_not_retried_and_prints_recovery_key(self):
        with patch.object(cli.api, "request_json", side_effect=urllib.error.URLError("offline")) as request:
            code, _stdout, stderr = run_cli([
                "generate", "--async", "--prompt", "cat",
                "--idempotency-key", "recovery-key",
            ], {config.API_KEY_ENV: "model-key"})

        self.assertEqual(code, 1)
        self.assertEqual(request.call_count, 1)
        self.assertIn("recovery-key", stderr)

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
