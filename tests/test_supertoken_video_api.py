import json
import io
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "supertoken-video-generation" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import supertoken_video_api as api
import supertoken_video_config as config


class VideoConfigTests(unittest.TestCase):
    def test_base_url_is_clean_https_and_never_duplicates_v1(self):
        self.assertEqual(config.normalize_base_url("https://api.supertoken.cc/v1/"), "https://api.supertoken.cc/v1")
        self.assertEqual(api.endpoint_url("https://api.supertoken.cc", "/v1/video/tasks"), "https://api.supertoken.cc/v1/video/tasks")
        with self.assertRaises(config.ConfigError):
            config.normalize_base_url("http://user:secret@example.test/v1")

    def test_key_types_are_rejected_without_echoing_values(self):
        with patch.dict("os.environ", {"SUPERTOKEN_API_KEY": "ak_secret"}, clear=False):
            with self.assertRaises(config.ConfigError) as captured:
                config.get_model_key()
        self.assertNotIn("secret", str(captured.exception))


class VideoTransportTests(unittest.TestCase):
    def test_json_request_sends_bearer_json_and_never_follows_redirects(self):
        response = api.ApiResponse(202, {"Content-Type": "application/json"}, b'{"id":"task_1"}')
        with patch.object(api, "_open_request", return_value=response) as opened:
            actual = api.request_json("POST", "https://api.example/v1/video/tasks", "sk_test", 30, {"model": "adobe-kling-3.0-720p"}, {"Idempotency-Key": "key-1"})
        self.assertEqual(actual.status, 202)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer sk_test")
        self.assertEqual(request.get_header("Idempotency-key"), "key-1")

    def test_diagnostics_redact_keys_and_signed_url_components(self):
        text = api.sanitize_diagnostic("sk_secret ak_secret wk_secret https://user:pass@host/x?sig=secret#fragment")
        for secret in ("sk_secret", "ak_secret", "wk_secret", "user:pass", "sig=secret", "fragment"):
            self.assertNotIn(secret, text)

    def test_request_errors_redact_opaque_submitted_api_key(self):
        opaque_key = "opaque-client-credential"
        error = urllib.error.HTTPError(
            "https://api.example/v1/video/tasks", 400, "bad request", {},
            io.BytesIO(opaque_key.encode("utf-8")),
        )
        with patch.object(api._OPENER, "open", side_effect=error):
            with self.assertRaises(api.ApiResponseError) as captured:
                api.request_json(
                    "POST", "https://api.example/v1/video/tasks", opaque_key, 30,
                    {"model": "adobe-kling-3.0-720p"},
                )
        self.assertNotIn(opaque_key, str(captured.exception))


class VideoMediaTransferTests(unittest.TestCase):
    def test_upload_rejects_alternate_numeric_loopback_urls_before_transport(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mp4"
            source.write_bytes(b"video")
            with patch.object(api, "_open_public_request") as opened:
                for host in ("2130706433", "0x7f000001"):
                    with self.subTest(host=host):
                        with self.assertRaises(api.ApiUsageError):
                            api.upload_media_files(f"https://{host}/upload", [source], 30)
                opened.assert_not_called()

    def test_download_adds_resource_key_only_for_resource_authorized_items(self):
        class Response:
            status = 200
            headers = {}

            def __init__(self, body):
                self.stream = io.BytesIO(body)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1):
                return self.stream.read(size)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                api, "_open_public_request", side_effect=[Response(b"first"), Response(b"second")]
            ) as opened:
                api.download_video_items([
                    {"url": "https://cdn.example/public.mp4", "filename": "public.mp4"},
                    {"url": "https://cdn.example/private.mp4", "filename": "private.mp4", "url_auth": "resource_api_key"},
                ], temp_dir, 30, "opaque-resource-key")
        first = opened.call_args_list[0].args[0]
        second = opened.call_args_list[1].args[0]
        self.assertIsNone(first.get_header("Authorization"))
        self.assertEqual(second.get_header("Authorization"), "Bearer opaque-resource-key")

    def test_download_uses_unique_paths_for_colliding_server_filenames(self):
        class Response:
            status = 200
            headers = {}

            def __init__(self, body):
                self.stream = io.BytesIO(body)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1):
                return self.stream.read(size)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                api, "_open_public_request", side_effect=[Response(b"first"), Response(b"second")]
            ):
                saved = api.download_video_items([
                    {"url": "https://cdn.example/one.mp4", "filename": "video.mp4"},
                    {"url": "https://cdn.example/two.mp4", "filename": "video.mp4"},
                ], temp_dir, 30)
            paths = [Path(item["path"]) for item in saved]
            self.assertEqual(len(set(paths)), 2)
            self.assertEqual({path.read_bytes() for path in paths}, {b"first", b"second"})
