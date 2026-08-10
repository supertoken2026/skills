import json
import sys
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
