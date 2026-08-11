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
    def build_payload(
        self, model, duration, mode=None, image_count=0, video_count=0,
        audio_count=0, aspect_ratio="16:9", no_audio=False,
    ):
        argv = [
            "generate", "--model", model, "--prompt", "a calm lake",
            "--duration", str(duration), "--aspect-ratio", aspect_ratio,
        ]
        if mode is not None:
            argv.extend(["--reference-mode", mode])
        if no_audio:
            argv.append("--no-audio")
        references = []
        for kind, count in (
            ("image", image_count),
            ("video", video_count),
            ("audio", audio_count),
        ):
            for index in range(count):
                url = f"https://assets.example/{kind}-{index}.bin"
                argv.extend([f"--{kind}", url])
                references.append({"kind": kind, "url": url})
        return cli.build_task_payload(cli.parse_args(argv), references)

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

    def test_models_all_keeps_live_video_ids_outside_static_known_families(self):
        live_unrecognized = "adobe-nextgen-video-2026"
        result = response({"data": [
            {"id": "adobe-kling-3.0-720p"},
            {"id": live_unrecognized},
        ]})
        with patch.object(cli.api, "request_json", return_value=result):
            code, stdout, stderr = run_cli(
                ["models", "--all"], {"SUPERTOKEN_API_KEY": "sk_test"}
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            json.loads(stdout),
            {"models": ["adobe-kling-3.0-720p", live_unrecognized]},
        )

        with patch.object(cli.api, "request_json", return_value=result):
            code, stdout, stderr = run_cli(
                ["models"], {"SUPERTOKEN_API_KEY": "sk_test"}
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {"models": ["adobe-kling-3.0-720p"]})

    def test_json_summaries_redact_key_shaped_server_and_client_values(self):
        listed = response({"data": [{"id": "adobe-sk_server_secret"}]})
        with patch.object(cli.api, "request_json", return_value=listed):
            code, stdout, stderr = run_cli(["models"], {"SUPERTOKEN_API_KEY": "sk_test"})
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("sk_server_secret", stdout)

        listed = response({"data": [{"id": "adobe-kling-3.0-720p"}]})
        created = response({"id": "sk_server_secret", "status": "queued"}, status=202)
        with patch.object(cli.api, "request_json", side_effect=[listed, created]):
            code, stdout, stderr = run_cli(
                ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "sunrise", "--duration", "3", "--idempotency-key", "sk_client_secret"],
                {"SUPERTOKEN_API_KEY": "sk_test"},
            )
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("sk_server_secret", stdout)
        self.assertNotIn("sk_client_secret", stdout)

    def test_success_summaries_redact_opaque_environment_credentials_echoed_by_server(self):
        model_key = "opaque-model-credential"
        model_response = api.ApiResponse(
            200, {}, b'{"data":[{"id":"opaque\\u002dmodel\\u002dcredential"}]}'
        )
        with patch.object(cli.api, "request_json", return_value=model_response):
            code, stdout, stderr = run_cli(
                ["models", "--all"], {"SUPERTOKEN_API_KEY": model_key}
            )
        self.assertEqual(code, 0, stderr)
        self.assertNotIn(model_key, stdout)
        self.assertNotIn("opaque\\u002dmodel", stdout)

        resource_key = "opaque-resource-credential"
        task_response = api.ApiResponse(
            200, {}, b'{"id":"opaque\\u002dresource\\u002dcredential","status":"queued"}'
        )
        with patch.object(cli.api, "request_json", return_value=task_response):
            code, stdout, stderr = run_cli(
                ["task", resource_key], {"SUPERTOKEN_RESOURCE_API_KEY": resource_key}
            )
        self.assertEqual(code, 0, stderr)
        self.assertNotIn(resource_key, stdout)
        self.assertNotIn("opaque\\u002dresource", stdout)

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

    def test_images_mode_uses_only_reference_images_for_veo_and_kling(self):
        image_urls = [
            "https://assets.example/first.png",
            "https://assets.example/second.png",
        ]
        for model, duration in (
            ("adobe-veo-3.1-standard-720p", "8"),
            ("adobe-kling-3.0-omni-720p", "3"),
        ):
            with self.subTest(model=model):
                args = cli.parse_args([
                    "generate", "--model", model, "--prompt", "a calm lake",
                    "--duration", duration, "--reference-mode", "images",
                    "--image", image_urls[0], "--image", image_urls[1],
                ])
                payload = cli.build_task_payload(
                    args, [{"kind": "image", "url": url} for url in image_urls]
                )
                self.assertEqual(
                    payload["input"]["reference_images"],
                    [{"url": url} for url in image_urls],
                )
                self.assertNotIn("image", payload["input"])

    def test_veo_text_generation_defaults_to_frame_reference_mode(self):
        for model, duration in (
            ("adobe-veo-3.1-fast-720p", "4"),
            ("adobe-veo-3.1-standard-720p", "6"),
        ):
            with self.subTest(model=model):
                args = cli.parse_args([
                    "generate", "--model", model, "--prompt", "a calm lake",
                    "--duration", duration,
                ])
                payload = cli.build_task_payload(args, [])
                self.assertEqual(payload["input"]["reference_mode"], "frame")

    def test_non_veo_text_generation_omits_reference_mode(self):
        args = cli.parse_args([
            "generate", "--model", "adobe-kling-3.0-720p", "--prompt", "a calm lake",
            "--duration", "3",
        ])
        payload = cli.build_task_payload(args, [])
        self.assertNotIn("reference_mode", payload["input"])

    def test_veo_frame_accepts_image_for_fast_and_standard(self):
        for model, duration in (
            ("adobe-veo-3.1-fast-720p", "4"),
            ("adobe-veo-3.1-standard-720p", "6"),
        ):
            with self.subTest(model=model):
                args = cli.parse_args([
                    "generate", "--model", model, "--prompt", "a calm lake",
                    "--duration", duration, "--reference-mode", "frame",
                    "--image", "https://assets.example/frame.png",
                ])
                payload = cli.build_task_payload(
                    args, [{"kind": "image", "url": "https://assets.example/frame.png"}]
                )
                self.assertEqual(payload["input"]["reference_mode"], "frame")

    def test_veo_rejects_unsupported_modes_and_invalid_standard_images(self):
        cases = (
            [
                "generate", "--model", "adobe-veo-3.1-fast-720p", "--prompt", "lake",
                "--duration", "4", "--reference-mode", "media",
                "--image", "https://assets.example/frame.png",
            ],
            [
                "generate", "--model", "adobe-veo-3.1-fast-720p", "--prompt", "lake",
                "--duration", "4", "--reference-mode", "images",
                "--image", "https://assets.example/frame.png",
            ],
            [
                "generate", "--model", "adobe-veo-3.1-standard-720p", "--prompt", "lake",
                "--duration", "6", "--reference-mode", "images",
                "--image", "https://assets.example/frame.png",
            ],
            [
                "generate", "--model", "adobe-veo-3.1-standard-720p", "--prompt", "lake",
                "--duration", "8", "--aspect-ratio", "9:16", "--reference-mode", "images",
                "--image", "https://assets.example/frame.png",
            ],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                args = cli.parse_args(argv)
                with self.assertRaises(api.ApiUsageError):
                    cli.build_task_payload(
                        args, [{"kind": "image", "url": "https://assets.example/frame.png"}]
                    )

    def test_veo_rejects_media_mode_for_text_only_fast_and_standard(self):
        for model, duration in (
            ("adobe-veo-3.1-fast-720p", "4"),
            ("adobe-veo-3.1-standard-720p", "6"),
        ):
            with self.subTest(model=model):
                args = cli.parse_args([
                    "generate", "--model", model, "--prompt", "lake",
                    "--duration", duration, "--reference-mode", "media",
                ])
                with self.assertRaises(api.ApiUsageError):
                    cli.build_task_payload(args, [])

    def test_media_payload_uses_only_plural_video_and_audio_fields(self):
        seedance = self.build_payload(
            "adobe-seedance-2.0-480p", 4, "media", 1, 1, 1
        )
        self.assertEqual(
            seedance["input"]["reference_videos"],
            [{"url": "https://assets.example/video-0.bin"}],
        )
        self.assertEqual(
            seedance["input"]["reference_audios"],
            [{"url": "https://assets.example/audio-0.bin"}],
        )
        self.assertNotIn("video", seedance["input"])
        self.assertNotIn("audio", seedance["input"])

        h3 = self.build_payload("leonardo-minimax-h3-1440p", 5, "media", 1, 0, 1)
        self.assertEqual(
            h3["input"]["reference_audios"],
            [{"url": "https://assets.example/audio-0.bin"}],
        )
        self.assertNotIn("audio", h3["input"])

    def test_h3_frame_and_images_preserve_model_specific_image_order(self):
        frame = self.build_payload("leonardo-minimax-h3-1440p", 5, "frame", 2)
        self.assertEqual(frame["input"]["image"], {"url": "https://assets.example/image-0.bin"})
        self.assertEqual(
            frame["input"]["reference_images"],
            [{"url": "https://assets.example/image-1.bin"}],
        )

        images = self.build_payload("leonardo-minimax-h3-1440p", 5, "images", 3)
        self.assertEqual(images["input"]["image"], {"url": "https://assets.example/image-0.bin"})
        self.assertEqual(
            images["input"]["reference_images"],
            [
                {"url": "https://assets.example/image-1.bin"},
                {"url": "https://assets.example/image-2.bin"},
            ],
        )

    def test_kling_known_model_contract(self):
        self.build_payload("adobe-kling-3.0-720p", 3, "frame")
        self.build_payload("adobe-kling-3.0-omni-720p", 3, "images", 3)
        for mode, images, videos, audios, aspect_ratio in (
            ("images", 1, 0, 0, "16:9"),
            ("media", 0, 0, 0, "16:9"),
            ("frame", 3, 0, 0, "16:9"),
            ("frame", 0, 1, 0, "16:9"),
            ("frame", 1, 0, 0, "1:1"),
        ):
            with self.subTest(mode=mode, images=images, videos=videos, audios=audios):
                with self.assertRaises(api.ApiUsageError):
                    self.build_payload(
                        "adobe-kling-3.0-720p", 3, mode, images, videos, audios,
                        aspect_ratio,
                    )
        with self.assertRaises(api.ApiUsageError):
            self.build_payload("adobe-kling-3.0-omni-720p", 3, "images", 4)

    def test_adobe_seedance_known_model_contract(self):
        self.build_payload("adobe-seedance-2.0-480p", 4, "frame", 2, aspect_ratio="21:9")
        self.build_payload("adobe-seedance-2.0-480p", 4, "media", 8, 3, 1)
        for mode, images, videos, audios, aspect_ratio in (
            ("images", 0, 0, 0, "16:9"),
            ("frame", 3, 0, 0, "16:9"),
            ("media", 10, 0, 0, "16:9"),
            ("media", 0, 4, 0, "16:9"),
            ("media", 0, 0, 4, "16:9"),
            ("media", 9, 3, 1, "16:9"),
            ("frame", 1, 0, 0, "2:1"),
        ):
            with self.subTest(mode=mode, images=images, videos=videos, audios=audios):
                with self.assertRaises(api.ApiUsageError):
                    self.build_payload(
                        "adobe-seedance-2.0-480p", 4, mode, images, videos, audios,
                        aspect_ratio,
                    )

    def test_leonardo_seedance_20_known_model_contract(self):
        self.build_payload("leonardo-seedance-2.0-480p", 4, "media", 4, 3, 1)
        for mode, images, videos, audios, aspect_ratio in (
            ("frame", 0, 0, 0, "16:9"),
            ("media", 5, 0, 0, "16:9"),
            ("media", 0, 4, 0, "16:9"),
            ("media", 0, 0, 2, "16:9"),
            ("media", 4, 3, 2, "16:9"),
            ("media", 0, 0, 1, "16:9"),
            ("media", 1, 0, 0, "2:1"),
        ):
            with self.subTest(mode=mode, images=images, videos=videos, audios=audios):
                with self.assertRaises(api.ApiUsageError):
                    self.build_payload(
                        "leonardo-seedance-2.0-480p", 4, mode, images, videos, audios,
                        aspect_ratio,
                    )

    def test_leonardo_seedance_25_known_model_contract(self):
        self.build_payload("leonardo-seedance-2.5-480p", 4, "frame", 2, aspect_ratio="21:9")
        self.build_payload("leonardo-seedance-2.5-480p", 4, "media", 30, 10, 10)
        for mode, images, videos, audios, aspect_ratio in (
            ("images", 0, 0, 0, "16:9"),
            ("frame", 0, 0, 0, "16:9"),
            ("frame", 3, 0, 0, "16:9"),
            ("media", 31, 0, 0, "16:9"),
            ("media", 0, 11, 0, "16:9"),
            ("media", 0, 0, 11, "16:9"),
            ("media", 0, 0, 1, "16:9"),
            ("media", 1, 0, 0, "2:1"),
        ):
            with self.subTest(mode=mode, images=images, videos=videos, audios=audios):
                with self.assertRaises(api.ApiUsageError):
                    self.build_payload(
                        "leonardo-seedance-2.5-480p", 4, mode, images, videos, audios,
                        aspect_ratio,
                    )

    def test_h3_known_model_contract(self):
        self.build_payload("leonardo-minimax-h3-1440p", 5, "frame", 2, aspect_ratio="21:9")
        self.build_payload("leonardo-minimax-h3-1440p", 5, "images", 5)
        self.build_payload("leonardo-minimax-h3-1440p", 5, "media", 5, 0, 3)
        for mode, images, videos, audios, aspect_ratio, no_audio in (
            ("frame", 0, 0, 0, "16:9", False),
            ("frame", 3, 0, 0, "16:9", False),
            ("images", 6, 0, 0, "16:9", False),
            ("media", 0, 0, 1, "16:9", False),
            ("media", 1, 0, 0, "16:9", False),
            ("media", 1, 1, 1, "16:9", False),
            ("media", 1, 0, 4, "16:9", False),
            ("frame", 1, 0, 0, "2:1", False),
            ("frame", 1, 0, 0, "16:9", True),
        ):
            with self.subTest(mode=mode, images=images, videos=videos, audios=audios):
                with self.assertRaises(api.ApiUsageError):
                    self.build_payload(
                        "leonardo-minimax-h3-1440p", 5, mode, images, videos, audios,
                        aspect_ratio, no_audio,
                    )

    def test_veo_rejects_unknown_aspect_ratio(self):
        with self.assertRaises(api.ApiUsageError):
            self.build_payload("adobe-veo-3.1-fast-720p", 4, aspect_ratio="1:1")

    def test_unknown_live_model_skips_static_reference_limits(self):
        self.build_payload("adobe-nextgen-video-2026", 1, "frame", 3)

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

    def test_generate_checks_live_inventory_before_creating_with_model_key(self):
        listed = response({"data": [{"id": "adobe-kling-3.0-720p"}]})
        task = response({"id": "task_42", "status": "queued"}, status=202)
        with patch.object(cli.api, "request_json", side_effect=[listed, task]) as request:
            code, stdout, stderr = run_cli(
                ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "sunrise", "--duration", "3"],
                {"SUPERTOKEN_API_KEY": "sk_test"},
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            [(call.args[0], call.args[1], call.args[2]) for call in request.call_args_list],
            [
                ("GET", "https://api.supertoken.cc/v1/models", "sk_test"),
                ("POST", "https://api.supertoken.cc/v1/video/tasks", "sk_test"),
            ],
        )
        self.assertEqual(request.call_count, 2)
        create = request.call_args_list[1]
        self.assertEqual(create.args[4]["model"], "adobe-kling-3.0-720p")
        header = create.args[5]["Idempotency-Key"]
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

    def test_local_reference_uses_inventory_before_resource_upload_and_model_create(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            reference.write_bytes(b"image")
            listed = response({"data": [{"id": "leonardo-seedance-2.5-480p"}]})
            prepared = response({"data": [{
                "id": "media_1", "method": "PATCH+SIGNED", "upload_url": "https://uploads.example/one",
                "headers": {"Content-Type": "image/png", "X-Upload-Token": "signed"},
            }]})
            completed = response({"data": [{"id": "media_1", "url": "https://assets.example/reference.png"}]})
            created = response({"id": "task_1", "status": "queued"}, status=202)
            with patch.object(cli.api, "request_json", side_effect=[listed, prepared, completed, created]) as request, patch.object(cli.api, "upload_media_files", return_value=[]) as uploaded:
                code, _stdout, stderr = run_cli(
                    ["generate", "--model", "leonardo-seedance-2.5-480p", "--prompt", "lake", "--duration", "4", "--reference-mode", "frame", "--image", str(reference)],
                    {"SUPERTOKEN_API_KEY": "sk_test", "SUPERTOKEN_RESOURCE_API_KEY": "ak_test"},
                )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(request.call_args_list[0].args[0], "GET")
        self.assertEqual(request.call_args_list[0].args[2], "sk_test")
        self.assertEqual(request.call_args_list[1].args[2], "ak_test")
        self.assertEqual(request.call_args_list[2].args[2], "ak_test")
        self.assertEqual(request.call_args_list[3].args[2], "sk_test")
        self.assertEqual(
            request.call_args_list[1].args[4],
            {"files": [{"kind": "image", "filename": "reference.png", "mime_type": "image/png", "size_bytes": 5}]},
        )
        self.assertEqual(request.call_args_list[2].args[4], {"upload_ids": ["media_1"]})
        uploaded.assert_called_once_with(
            "https://uploads.example/one", [reference], cli.HTTP_TIMEOUT,
            headers={"Content-Type": "image/png", "X-Upload-Token": "signed"}, method="PATCH+SIGNED",
        )

    def test_completion_with_query_stops_before_model_task_create(self):
        query_value = "completion-signed-value"
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            reference.write_bytes(b"image")
            listed = response({"data": [{"id": "leonardo-seedance-2.5-480p"}]})
            prepared = response({"data": [{
                "id": "media_1", "method": "PUT", "upload_url": "https://uploads.example/one",
                "headers": {},
            }]})
            completed = response({"data": [{
                "id": "media_1",
                "url": f"https://assets.example/reference.png?signature={query_value}",
            }]})
            created = response({"id": "task_1", "status": "queued"}, status=202)
            with patch.object(cli.api, "request_json", side_effect=[listed, prepared, completed, created]) as request, patch.object(cli.api, "upload_media_files", return_value=[]):
                code, stdout, stderr = run_cli(
                    ["generate", "--model", "leonardo-seedance-2.5-480p", "--prompt", "lake", "--duration", "4", "--reference-mode", "frame", "--image", str(reference)],
                    {"SUPERTOKEN_API_KEY": "sk_test", "SUPERTOKEN_RESOURCE_API_KEY": "ak_test"},
                )
        self.assertEqual(code, 2)
        self.assertEqual(request.call_count, 3)
        self.assertIn("media completion response was invalid", stderr)
        self.assertNotIn(query_value, stdout)
        self.assertNotIn(query_value, stderr)
        self.assertNotIn(
            query_value,
            json.dumps([call.args[4] for call in request.call_args_list if call.args[0] == "POST"]),
        )

    def test_upload_requires_resource_key_and_reports_no_temporary_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(b"image")
            prepared = response({"data": [{
                "id": "media_1", "method": "PATCH", "upload_url": "https://uploads.example/one",
                "headers": {"Content-Type": "image/png"},
            }]})
            completed = response({"data": [{"id": "media_1", "url": "https://assets.example/reference.png"}]})
            with patch.object(cli.api, "request_json", side_effect=[prepared, completed]) as request, patch.object(cli.api, "upload_media_files", return_value=[]) as uploaded:
                code, stdout, stderr = run_cli(["upload", "--file", str(source), "--kind", "image"], {"SUPERTOKEN_RESOURCE_API_KEY": "ak_test"})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(request.call_args.args[2], "ak_test")
        self.assertEqual(json.loads(stdout), {"kind": "image", "media_id": "media_1"})
        self.assertNotIn("assets.example", stdout)
        self.assertEqual(
            request.call_args_list[0].args[4],
            {"files": [{"kind": "image", "filename": "source.png", "mime_type": "image/png", "size_bytes": 5}]},
        )
        self.assertEqual(request.call_args_list[1].args[4], {"upload_ids": ["media_1"]})
        uploaded.assert_called_once_with(
            "https://uploads.example/one", [source], cli.HTTP_TIMEOUT,
            headers={"Content-Type": "image/png"}, method="PATCH",
        )

    def test_upload_summary_redacts_a_key_shaped_media_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(b"image")
            prepared = response({"data": [{
                "id": "sk_server_secret", "method": "PUT", "upload_url": "https://uploads.example/one",
                "headers": {},
            }]})
            completed = response({"data": [{"id": "sk_server_secret", "url": "https://assets.example/reference.png"}]})
            with patch.object(cli.api, "request_json", side_effect=[prepared, completed]), patch.object(cli.api, "upload_media_files", return_value=[]):
                code, stdout, stderr = run_cli(["upload", "--file", str(source), "--kind", "image"], {"SUPERTOKEN_RESOURCE_API_KEY": "ak_test"})
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("sk_server_secret", stdout)

    def test_upload_rejects_malformed_documented_records_before_presigned_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(b"image")
            prepared = response({"data": [{
                "id": "media_1", "method": "PUT", "upload_url": "https://uploads.example/one",
                "headers": {"X-Upload": "bad\nvalue"},
            }]})
            with patch.object(cli.api, "request_json", return_value=prepared), patch.object(cli.api, "upload_media_files") as uploaded:
                code, _stdout, _stderr = run_cli(
                    ["upload", "--file", str(source), "--kind", "image"],
                    {"SUPERTOKEN_RESOURCE_API_KEY": "ak_test"},
                )
        self.assertEqual(code, 2)
        uploaded.assert_not_called()

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

    def test_operational_commands_reject_credential_options_before_requests_or_echoes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(b"image")
            commands = (
                ("models", ["models"], {"SUPERTOKEN_API_KEY": "sk_environment"}),
                ("upload", ["upload", "--file", str(source), "--kind", "image"], {"SUPERTOKEN_RESOURCE_API_KEY": "ak_environment"}),
                ("generate", ["generate", "--model", "adobe-kling-3.0-720p", "--prompt", "sunrise", "--duration", "3"], {"SUPERTOKEN_API_KEY": "sk_environment"}),
                ("task", ["task", "task_1"], {"SUPERTOKEN_RESOURCE_API_KEY": "ak_environment"}),
                ("wait", ["wait", "task_1", "--output", "a.mp4"], {"SUPERTOKEN_RESOURCE_API_KEY": "ak_environment"}),
            )
            options = (
                ("--api-key", ("sk_cli_argument_sentinel", "sk\\u005fcli\\u005fargument\\u005fsentinel")),
                ("--resource-api-key", ("ak_cli_argument_sentinel", "ak\\u005fcli\\u005fargument\\u005fsentinel")),
            )
            with patch.object(cli.api, "request_json") as request:
                for command, argv, environment in commands:
                    for option, values in options:
                        for value in values:
                            with self.subTest(command=command, option=option, value=value):
                                code, stdout, stderr = run_cli([*argv, option, value], environment)
                                self.assertEqual(code, 2)
                                self.assertEqual(stdout, "")
                                self.assertNotIn(value, stderr)
                                self.assertNotIn(value.replace("\\u005f", "_"), stderr)
                                self.assertNotIn("cli_argument_sentinel", stderr)
            request.assert_not_called()

    def test_generate_rejects_absent_live_model_before_local_upload_or_create(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            reference.write_bytes(b"image")
            listed = response({"data": [{"id": "adobe-veo-3.1-fast-720p"}]})
            with patch.object(cli.api, "request_json", return_value=listed) as request, patch.object(cli.api, "upload_media_files") as uploaded:
                code, stdout, stderr = run_cli(
                    ["generate", "--model", "leonardo-seedance-2.5-480p", "--prompt", "lake", "--duration", "4", "--reference-mode", "frame", "--image", str(reference)],
                    {"SUPERTOKEN_API_KEY": "sk_test", "SUPERTOKEN_RESOURCE_API_KEY": "ak_test"},
                )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[:3], ("GET", "https://api.supertoken.cc/v1/models", "sk_test"))
        self.assertNotIn("/v1/video/tasks", str(request.call_args_list))
        self.assertNotIn("/v1/media/uploads", str(request.call_args_list))
        self.assertIn("model", stderr)
        uploaded.assert_not_called()

    def test_generate_accepts_live_unknown_model_without_static_contract_preflight(self):
        model = "adobe-nextgen-video-2026"
        listed = response({"data": [{"id": model}]})
        created = response({"id": "task_1", "status": "queued"}, status=202)
        with patch.object(cli.api, "request_json", side_effect=[listed, created]) as request:
            code, stdout, stderr = run_cli(
                ["generate", "--model", model, "--prompt", "sunrise", "--duration", "1"],
                {"SUPERTOKEN_API_KEY": "sk_test"},
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "POST"])
        self.assertEqual(request.call_args_list[1].args[4]["model"], model)
        self.assertEqual(json.loads(stdout)["task_id"], "task_1")

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
