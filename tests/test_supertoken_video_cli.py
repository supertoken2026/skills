import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "supertoken-video-generation" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import supertoken_video_api as api
import supertoken_video_config as config
import supertoken_video as cli


def run_cli(argv, environment=None):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.dict(os.environ, environment or {}, clear=True):
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def response(payload, headers=None, status=200):
    return api.ApiResponse(status, headers or {}, json.dumps(payload).encode("utf-8"))


class VideoCliTests(unittest.TestCase):
    def test_models_filters_known_video_families_and_uses_model_token(self):
        result = api.ApiResponse(200, {}, json.dumps({"data": [
            {"id": "adobe-kling-3.0-720p"},
            {"id": "leonardo-seedance-2.5-480p"},
            {"id": "gpt-image-2"},
        ]}).encode())
        with patch.object(cli.api, "request_json", return_value=result) as request:
            code, stdout, stderr = run_cli(["models"], {"SUPERTOKEN_API_KEY": "sk_test"})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {"models": ["adobe-kling-3.0-720p", "leonardo-seedance-2.5-480p"]})
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertEqual(request.call_args.args[2], "sk_test")

    def test_json_summaries_redact_key_shaped_server_and_client_values(self):
        listed = response({"data": [{"id": "adobe-sk_server_secret"}]})
        with patch.object(cli.api, "request_json", return_value=listed):
            code, stdout, stderr = run_cli(["models"], {"SUPERTOKEN_API_KEY": "sk_test"})
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("sk_server_secret", stdout)

        created = response({"id": "sk_server_secret", "status": "queued"}, status=202)
        with patch.object(cli.api, "request_json", return_value=created):
            code, stdout, stderr = run_cli(
                ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "sunrise", "--duration", "3", "--idempotency-key", "sk_client_secret"],
                {"SUPERTOKEN_API_KEY": "sk_test"},
            )
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("sk_server_secret", stdout)
        self.assertNotIn("sk_client_secret", stdout)

    def test_generate_uses_public_video_fields_and_requires_model_duration(self):
        args = cli.parse_args([
            "generate", "--model", "leonardo-seedance-2.5-480p", "--prompt", "a calm lake", "--duration", "4",
            "--reference-mode", "frame", "--image", "https://assets.example/frame.png",
        ])
        payload = cli.build_task_payload(args, [{"kind": "image", "url": "https://assets.example/frame.png"}])
        self.assertEqual(payload["operation"], "generation")
        self.assertEqual(payload["input"]["reference_mode"], "frame")
        self.assertNotIn("provider_options", payload)
        self.assertEqual(payload["output"], {"duration": 4, "aspect_ratio": "16:9", "generate_audio": True})

    def test_wait_polls_with_resource_key_and_downloads_only_protected_urls(self):
        queued = api.ApiResponse(200, {"Retry-After": "2"}, b'{"id":"task_1","status":"queued"}')
        succeeded = api.ApiResponse(200, {}, b'{"id":"task_1","status":"succeeded","result":{"videos":[{"url":"https://assets.example/a.mp4","url_auth":"resource_api_key","filename":"a.mp4"}]}}')
        with patch.object(cli.api, "request_json", side_effect=[queued, succeeded]) as request, patch.object(cli.api, "download_video_items", return_value=[{"path": "/tmp/a.mp4"}]) as download, patch.object(cli.time, "sleep"):
            code, stdout, stderr = run_cli(["wait", "task_1", "--output", "a.mp4"], {"SUPERTOKEN_RESOURCE_API_KEY": "ak_test"})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(request.call_args_list[0].args[2], "ak_test")
        download.assert_called_once()
        self.assertEqual(json.loads(stdout)["task_id"], "task_1")

    def test_invalid_generate_input_is_rejected_before_network_access(self):
        cases = [
            ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", " ", "--duration", "3"],
            ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "ok", "--duration", "2"],
            ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "ok", "--duration", "3", "--image", "https://assets.example/a.png"],
            ["generate", "--model", "leonardo-minimax-h3-1440p", "--prompt", "ok", "--duration", "5", "--no-audio"],
            ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "ok", "--duration", "3", "--output", "a.mp4"],
            ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "ok", "--duration", "3", "--wait"],
        ]
        with patch.object(cli.api, "request_json") as request:
            for argv in cases:
                with self.subTest(argv=argv):
                    code, _stdout, _stderr = run_cli(argv, {"SUPERTOKEN_API_KEY": "sk_test"})
                    self.assertEqual(code, 2)
        request.assert_not_called()

    def test_create_uses_model_key_and_sanitized_summary(self):
        task = response({"id": "task_42", "status": "queued"}, status=202)
        with patch.object(cli.api, "request_json", return_value=task) as request:
            code, stdout, stderr = run_cli(
                ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "sunrise", "--duration", "3"],
                {"SUPERTOKEN_API_KEY": "sk_test"},
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(request.call_args.args[0], "POST")
        self.assertEqual(request.call_args.args[2], "sk_test")
        self.assertEqual(request.call_args.args[4]["model"], "adobe-kling-3.0-720p")
        header = request.call_args.args[5]["Idempotency-Key"]
        self.assertTrue(header.isascii())
        self.assertTrue(header)
        summary = json.loads(stdout)
        self.assertEqual(summary["task_id"], "task_42")
        self.assertNotIn("sk_test", stdout)
        self.assertNotIn("url", stdout.lower())

    def test_generate_wait_requires_resource_key_before_creating_a_task(self):
        with patch.object(cli.api, "request_json") as request:
            code, _stdout, _stderr = run_cli(
                ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "sunrise", "--duration", "3", "--wait", "--output", "a.mp4"],
                {"SUPERTOKEN_API_KEY": "sk_test"},
            )
        self.assertEqual(code, 2)
        request.assert_not_called()

    def test_local_reference_uses_resource_upload_before_model_create(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            reference.write_bytes(b"image")
            prepared = response({"id": "media_1", "upload_url": "https://uploads.example/one"})
            completed = response({"url": "https://assets.example/reference.png"})
            created = response({"id": "task_1", "status": "queued"}, status=202)
            with patch.object(cli.api, "request_json", side_effect=[prepared, completed, created]) as request, patch.object(cli.api, "upload_media_files", return_value=[]):
                code, _stdout, stderr = run_cli(
                    ["generate", "--model", "leonardo-seedance-2.5-480p", "--prompt", "lake", "--duration", "4", "--reference-mode", "frame", "--image", str(reference)],
                    {"SUPERTOKEN_API_KEY": "sk_test", "SUPERTOKEN_RESOURCE_API_KEY": "ak_test"},
                )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(request.call_args_list[0].args[2], "ak_test")
        self.assertEqual(request.call_args_list[1].args[2], "ak_test")
        self.assertEqual(request.call_args_list[2].args[2], "sk_test")

    def test_upload_requires_resource_key_and_reports_no_temporary_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(b"image")
            prepared = response({"id": "media_1", "upload_url": "https://uploads.example/one"})
            completed = response({"url": "https://assets.example/reference.png"})
            with patch.object(cli.api, "request_json", side_effect=[prepared, completed]) as request, patch.object(cli.api, "upload_media_files", return_value=[]):
                code, stdout, stderr = run_cli(["upload", "--file", str(source), "--kind", "image"], {"SUPERTOKEN_RESOURCE_API_KEY": "ak_test"})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(request.call_args.args[2], "ak_test")
        self.assertEqual(json.loads(stdout), {"kind": "image", "media_id": "media_1"})
        self.assertNotIn("assets.example", stdout)

    def test_upload_summary_redacts_a_key_shaped_media_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(b"image")
            prepared = response({"id": "sk_server_secret", "upload_url": "https://uploads.example/one"})
            completed = response({"url": "https://assets.example/reference.png"})
            with patch.object(cli.api, "request_json", side_effect=[prepared, completed]), patch.object(cli.api, "upload_media_files", return_value=[]):
                code, stdout, stderr = run_cli(["upload", "--file", str(source), "--kind", "image"], {"SUPERTOKEN_RESOURCE_API_KEY": "ak_test"})
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("sk_server_secret", stdout)

    def test_models_excludes_non_video_vendor_models(self):
        listed = response({"data": [
            {"id": "adobe-kling-3.0-720p"},
            {"id": "adobe-firefly-image-4"},
            {"id": "leonardo-phoenix"},
        ]})
        with patch.object(cli.api, "request_json", return_value=listed):
            code, stdout, stderr = run_cli(["models"], {"SUPERTOKEN_API_KEY": "sk_test"})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {"models": ["adobe-kling-3.0-720p"]})

    def test_wait_rejects_non_finite_timeouts_before_requests(self):
        with patch.object(cli.api, "request_json") as request:
            for timeout in ("nan", "inf"):
                with self.subTest(timeout=timeout):
                    code, _stdout, _stderr = run_cli(
                        ["wait", "task_1", "--output", "a.mp4", "--wait-timeout", timeout],
                        {"SUPERTOKEN_RESOURCE_API_KEY": "ak_test"},
                    )
                    self.assertEqual(code, 2)
        request.assert_not_called()

    def test_parser_errors_do_not_echo_literal_or_escaped_key_shaped_arguments(self):
        values = ("sk_parser_sentinel", "sk\\u005fparser\\u005fsentinel")
        with patch.object(cli.api, "request_json") as request:
            for value in values:
                with self.subTest(value=value):
                    code, stdout, stderr = run_cli(["wait", "task_1", "--output", "a.mp4", "--api-key", value])
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertNotIn(value, stderr)
                    self.assertNotIn("sk_parser", stderr)
        request.assert_not_called()

    def test_task_rejects_non_finite_progress_before_json_output(self):
        with patch.object(cli.api, "request_json") as request:
            for progress in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(progress=progress):
                    request.return_value = response({"id": "task_1", "status": "queued", "progress": progress})
                    code, stdout, stderr = run_cli(["task", "task_1"], {"SUPERTOKEN_RESOURCE_API_KEY": "ak_test"})
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertNotIn("NaN", stderr)
                    self.assertNotIn("Infinity", stderr)

    def test_metadata_json_rejects_non_finite_values_before_requests(self):
        with patch.object(cli.api, "request_json") as request:
            code, stdout, stderr = run_cli(
                ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "sunrise", "--duration", "3", "--metadata-json", '{"value":NaN}'],
                {"SUPERTOKEN_API_KEY": "sk_test"},
            )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertNotIn("NaN", stderr)
        request.assert_not_called()

    def test_read_task_uses_resource_key_and_never_prints_result_urls(self):
        task = response({"id": "task_1", "status": "succeeded", "result": {"videos": [{"url": "https://assets.example/a.mp4?token=secret"}]}})
        with patch.object(cli.api, "request_json", return_value=task) as request:
            code, stdout, stderr = run_cli(["task", "task_1"], {"SUPERTOKEN_RESOURCE_API_KEY": "ak_test"})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(request.call_args.args[2], "ak_test")
        self.assertNotIn("token", stdout)
        self.assertEqual(json.loads(stdout), {"task_id": "task_1", "status": "succeeded"})


if __name__ == "__main__":
    unittest.main()
