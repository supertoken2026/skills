import base64
import io
import json
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "supertoken-gpt-image-2"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import supertoken_api as api  # noqa: E402


PNG_BYTES = b"\x89PNG\r\n\x1a\nimage"
JPEG_BYTES = b"\xff\xd8\xffimage"


class FakeResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class EndpointTests(unittest.TestCase):
    def test_root_base_adds_v1(self):
        self.assertEqual(
            api.endpoint_url("https://api.supertoken.cc", "/v1/images/generations"),
            "https://api.supertoken.cc/v1/images/generations",
        )

    def test_v1_base_does_not_duplicate_v1(self):
        self.assertEqual(
            api.endpoint_url("https://proxy.example/v1", "/v1/images/edits"),
            "https://proxy.example/v1/images/edits",
        )

    def test_legacy_base_only_accepts_sync_images_routes(self):
        base = "https://api.supertoken.cc/image-wrapper/v1"
        self.assertEqual(
            api.endpoint_url(base, "/v1/images/generations"),
            f"{base}/images/generations",
        )
        with self.assertRaisesRegex(api.ApiUsageError, "旧版"):
            api.endpoint_url(base, "/v1/image/tasks")

    def test_route_must_begin_with_v1(self):
        with self.assertRaisesRegex(api.ApiUsageError, "/v1/"):
            api.endpoint_url("https://api.supertoken.cc", "/images/generations")


class MultipartTests(unittest.TestCase):
    def test_encode_multipart_repeats_image_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "one.png"
            second = Path(temp_dir) / "two.png"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            body, content_type = api.encode_multipart(
                [("model", "gpt-image-2")],
                [
                    api.MultipartFile("image", first, "image/png"),
                    api.MultipartFile("image", second, "image/png"),
                ],
                boundary="test-boundary",
            )
        self.assertEqual(body.count(b'name="image"'), 2)
        self.assertIn("boundary=test-boundary", content_type)

    def test_encode_multipart_rejects_newline_in_header_values(self):
        with self.assertRaisesRegex(api.ApiUsageError, "换行符"):
            api.encode_multipart([("model\nname", "gpt-image-2")], [])


class DiagnosticTests(unittest.TestCase):
    def test_sanitize_diagnostic_redacts_all_key_types(self):
        body = b"sk-123456789 ak_123456789 wk-123456789 explicit-secret"
        text = api.sanitize_diagnostic(body, "explicit-secret")
        self.assertNotIn("123456789", text)
        self.assertNotIn("explicit-secret", text)
        self.assertLessEqual(len(text), 1000)


class HttpErrorTests(unittest.TestCase):
    def test_401_and_403_distinguish_invalid_key_from_model_permission(self):
        invalid = api.classify_http_error(401, {}, "model", "gpt-image-2")
        forbidden = api.classify_http_error(403, {}, "model", "gpt-image-2")
        self.assertIn("SUPERTOKEN_API_KEY 无效", invalid)
        self.assertIn("无权访问模型 gpt-image-2", forbidden)
        self.assertNotEqual(invalid, forbidden)

    def test_defined_error_statuses_have_specific_messages(self):
        cases = {
            400: "请求字段无效",
            409: "Idempotency-Key",
            413: "multipart",
            429: "账户额度",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertIn(expected, api.classify_http_error(status, {}, "model"))

    def test_resource_key_error_names_resource_environment_variable(self):
        text = api.classify_http_error(401, {}, "resource")

        self.assertIn("SUPERTOKEN_RESOURCE_API_KEY", text)

    def test_temporary_service_error_includes_request_id(self):
        text = api.classify_http_error(503, {"X-Request-ID": "request-123"}, "model")

        self.assertIn("HTTP 503", text)
        self.assertIn("请求 ID：request-123", text)

    def test_all_5xx_errors_are_temporary_and_include_request_id(self):
        for status in (500, 504):
            with self.subTest(status=status):
                text = api.classify_http_error(
                    status, {"X-Request-Id": "request-123"}, "model"
                )

                self.assertIn(f"图片服务暂时不可用（HTTP {status}）", text)
                self.assertIn("请求 ID：request-123", text)

    def test_unclassified_status_uses_generic_message(self):
        self.assertEqual(
            api.classify_http_error(418, {}, "model"),
            "SuperToken 图片请求失败（HTTP 418）。",
        )


class ResponseParsingTests(unittest.TestCase):
    def test_parse_json_response_returns_object(self):
        response = api.ApiResponse(200, {}, b'{"data": []}')

        self.assertEqual(api.parse_json_response(response), {"data": []})

    def test_parse_json_response_redacts_non_json_body(self):
        secret = "sk-123456789"
        response = api.ApiResponse(502, {}, f"broken {secret}".encode())

        with self.assertRaises(api.ApiResponseError) as raised:
            api.parse_json_response(response, (secret,))

        self.assertIn("HTTP 502", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_parse_json_response_rejects_non_object_json(self):
        response = api.ApiResponse(200, {}, b"[]")

        with self.assertRaisesRegex(api.ApiResponseError, "不是对象"):
            api.parse_json_response(response)


class HeaderTests(unittest.TestCase):
    def test_request_id_is_case_insensitive_and_prioritizes_x_request_id(self):
        value = api.request_id({"CF-Ray": "cf-123", "X-Request-ID": "request-123"})

        self.assertEqual(value, "request-123")

    def test_header_value_is_case_insensitive_and_returns_default(self):
        headers = {"Content-Type": "application/json"}

        self.assertEqual(api.header_value(headers, "content-type"), "application/json")
        self.assertEqual(api.header_value(headers, "x-missing", "fallback"), "fallback")


class RequestTests(unittest.TestCase):
    def test_request_json_sets_method_bearer_json_headers_and_calls_once(self):
        with patch.object(
            api.urllib.request,
            "urlopen",
            return_value=FakeResponse(200, {"Content-Type": "application/json"}, b'{"ok": true}'),
        ) as urlopen:
            response = api.request_json(
                "POST",
                "https://api.example.test/v1/images/generations",
                "test-api-key",
                30,
                {"model": "gpt-image-2"},
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-api-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {"model": "gpt-image-2"})
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(response, api.ApiResponse(200, {"Content-Type": "application/json"}, b'{"ok": true}'))

    def test_request_multipart_sets_bearer_header_and_matching_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "source.png"
            image.write_bytes(b"image-bytes")
            with patch.object(
                api.urllib.request,
                "urlopen",
                return_value=FakeResponse(201, {}, b'{"id": "image-1"}'),
            ) as urlopen:
                response = api.request_multipart(
                    "POST",
                    "https://api.example.test/v1/images/edits",
                    "test-api-key",
                    30,
                    [("model", "gpt-image-2")],
                    [api.MultipartFile("image", image, "image/png")],
                )

        request = urlopen.call_args.args[0]
        content_type = request.get_header("Content-type")
        boundary = content_type.split("boundary=", 1)[1]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-api-key")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(f"--{boundary}".encode(), request.data)
        self.assertIn(b'name="image"', request.data)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(response, api.ApiResponse(201, {}, b'{"id": "image-1"}'))

    def test_http_error_preserves_status_headers_and_body(self):
        error = urllib.error.HTTPError(
            "https://api.example.test/v1/images/generations",
            429,
            "Too Many Requests",
            {"X-Request-ID": "request-123"},
            io.BytesIO(b'{"error": "limited"}'),
        )
        with patch.object(api.urllib.request, "urlopen", side_effect=error) as urlopen:
            response = api.request_json(
                "POST",
                "https://api.example.test/v1/images/generations",
                "test-api-key",
                30,
                {"model": "gpt-image-2"},
            )

        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers, {"X-Request-ID": "request-123"})
        self.assertEqual(response.body, b'{"error": "limited"}')
        self.assertTrue(error.fp.closed)
        self.assertEqual(urlopen.call_count, 1)

    def test_http_error_without_headers_uses_an_empty_header_mapping(self):
        error = urllib.error.HTTPError(
            "https://api.example.test/v1/images/generations",
            500,
            "Service Unavailable",
            None,
            io.BytesIO(b'{"error": "temporary"}'),
        )
        with patch.object(api.urllib.request, "urlopen", side_effect=error):
            response = api.request_json(
                "POST",
                "https://api.example.test/v1/images/generations",
                "test-api-key",
                30,
                {"model": "gpt-image-2"},
            )

        self.assertEqual(response.status, 500)
        self.assertEqual(response.headers, {})
        self.assertEqual(response.body, b'{"error": "temporary"}')
        self.assertTrue(error.fp.closed)

    def test_url_error_propagates_without_retry(self):
        with patch.object(
            api.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ) as urlopen:
            with self.assertRaises(urllib.error.URLError):
                api.request_json(
                    "GET",
                    "https://api.example.test/v1/image/tasks/task-1",
                    "test-api-key",
                    30,
                )

        self.assertEqual(urlopen.call_count, 1)


class ImageValidationAndOutputTests(unittest.TestCase):
    def test_validate_local_images_enforces_count_and_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = []
            for index in range(11):
                path = Path(temp_dir) / f"{index}.png"
                path.write_bytes(PNG_BYTES)
                paths.append(path)
            with self.assertRaisesRegex(api.ApiUsageError, "最多 10 张"):
                api.validate_local_images(paths)

            oversized = Path(temp_dir) / "oversized.png"
            with oversized.open("wb") as stream:
                stream.write(PNG_BYTES)
                stream.seek(api.MAX_FILE_BYTES)
                stream.write(b"x")
            with self.assertRaisesRegex(api.ApiUsageError, "20 MiB"):
                api.validate_local_images([oversized])

    def test_save_image_items_names_every_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            items = [
                {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")},
                {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")},
            ]
            saved = api.save_image_items(items, Path(temp_dir) / "result.png", 5)
        self.assertEqual(
            [item.path.name for item in saved],
            ["result-1.png", "result-2.png"],
        )

    def test_save_image_items_corrects_a_mismatched_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            item = {"b64_json": base64.b64encode(JPEG_BYTES).decode("ascii")}
            saved = api.save_image_items([item], Path(temp_dir) / "result.png", 5)
        self.assertEqual(saved[0].path.name, "result.jpeg")

    def test_save_image_items_rejects_non_string_base64_as_a_response_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"

            with self.assertRaisesRegex(api.ApiResponseError, "Base64"):
                api.save_image_items([{"b64_json": 7}], output, timeout=5)

            self.assertFalse(output.exists())
            self.assertFalse(Path(f"{output}.part").exists())

    def test_save_image_items_removes_part_file_after_write_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}

            def write_empty(path, _data):
                path.touch()
                return 0

            with patch.object(
                Path, "write_bytes", autospec=True, side_effect=write_empty,
            ):
                with self.assertRaisesRegex(api.ApiResponseError, "为空"):
                    api.save_image_items([item], output, 5)

            self.assertFalse((Path(temp_dir) / "result.png.part").exists())

    def test_download_rejects_a_redirect_to_http(self):
        response = MagicMock()
        response.geturl.return_value = "http://cdn.example/image.png"
        response.__enter__.return_value = response
        with patch.object(api.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(api.ApiResponseError, "HTTPS"):
                api.download_image("https://cdn.example/start", 5)
