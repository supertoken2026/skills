import base64
import email.message
import http.client
import io
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.response
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


PNG_BYTES = b"\x89PNG\r\n\x1a\nimage"
JPEG_BYTES = b"\xff\xd8\xffimage"


class FakeResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body
        self._stream = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        return self._stream.read(size)

    def geturl(self):
        return "https://api.example.test/response"


def public_dns_result(_host, port, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


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
            first.write_bytes(PNG_BYTES)
            second.write_bytes(PNG_BYTES)
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

    def test_validated_snapshot_is_uploaded_after_source_path_changes(self):
        replacement = b"\xff\xd8\xffreplacement"
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(PNG_BYTES)
            validated = api.validate_local_images([source])[0]
            self.assertTrue(
                hasattr(validated, "data"),
                "validated images must retain the bytes that passed validation",
            )
            source.write_bytes(replacement)

            body, _content_type = api.encode_multipart(
                [],
                [api.MultipartFile(
                    "image", validated.path, "image/png", validated.data,
                )],
                boundary="snapshot-boundary",
            )

        self.assertIn(PNG_BYTES, body)
        self.assertNotIn(replacement, body)
        self.assertIn(b'filename="source.png"', body)

    def test_encode_multipart_rejects_invalid_signature_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(b"not an image")
            invalid = api.MultipartFile("image", source, "image/png")
            with self.assertRaisesRegex(api.ApiUsageError, "PNG"):
                api.encode_multipart([], [invalid])

    def test_encode_multipart_rejects_oversized_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(PNG_BYTES + b"x" * 8)
            oversized = api.MultipartFile("image", source, "image/png")
            with patch.object(api, "MAX_FILE_BYTES", len(PNG_BYTES) + 7):
                with self.assertRaisesRegex(api.ApiUsageError, "20 MiB"):
                    api.encode_multipart([], [oversized])

    def test_encode_multipart_enforces_image_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(PNG_BYTES)
            files = [
                api.MultipartFile("image", source, "image/png")
                for _index in range(api.MAX_IMAGES + 1)
            ]
            with self.assertRaisesRegex(api.ApiUsageError, "最多 10 张"):
                api.encode_multipart([], files)

    def test_encode_multipart_enforces_combined_image_and_mask_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            padded = PNG_BYTES + b"x" * 4
            source.write_bytes(padded)
            files = [
                api.MultipartFile("image", source, "image/png"),
                api.MultipartFile("mask", source, "image/png"),
            ]
            with patch.object(api, "MAX_FILE_BYTES", len(padded)):
                with patch.object(api, "MAX_MULTIPART_BYTES", len(padded) * 2 - 1):
                    with self.assertRaisesRegex(api.ApiUsageError, "100 MiB"):
                        api.encode_multipart([], files)


class DiagnosticTests(unittest.TestCase):
    def test_sanitize_diagnostic_redacts_all_key_types(self):
        body = b"sk-123456789 ak_123456789 wk-123456789 explicit-secret"
        text = api.sanitize_diagnostic(body, "explicit-secret")
        self.assertNotIn("123456789", text)
        self.assertNotIn("explicit-secret", text)
        self.assertLessEqual(len(text), 1000)

    def test_sanitize_url_drops_credentials_query_and_fragment(self):
        self.assertEqual(
            api.sanitize_url(
                "https://user:secret@example.test/path/image.png?token=secret#part"
            ),
            "https://example.test/path/image.png",
        )

    def test_sanitize_request_id_redacts_credential_shaped_values(self):
        self.assertEqual(
            api.sanitize_request_id("request-sk-123456789-ak_resource123456"),
            "[REDACTED]",
        )

    def test_sanitize_diagnostic_removes_base64_and_signed_url_components(self):
        body = json.dumps({
            "data": [{
                "b64_json": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
                "url": (
                    "https://user:pass@cdn.example.test/image.png"
                    "?signature=signed-secret#fragment"
                ),
            }],
        }).encode("utf-8")

        text = api.sanitize_diagnostic(body)

        self.assertNotIn("iVBORw0KGgo", text)
        self.assertNotIn("user:pass", text)
        self.assertNotIn("signed-secret", text)
        self.assertNotIn("fragment", text)
        self.assertIn("https://cdn.example.test/image.png", text)

    def test_sanitize_diagnostic_redacts_bare_base64_image_data(self):
        encoded = base64.b64encode(PNG_BYTES * 8).decode("ascii")

        text = api.sanitize_diagnostic(f"upstream body: {encoded}".encode())

        self.assertNotIn(encoded, text)
        self.assertNotIn("iVBORw0KGgo", text)

    def test_sanitize_diagnostic_redacts_credential_shaped_json_keys(self):
        secret = "sk-serversecret123"

        text = api.sanitize_diagnostic(
            json.dumps({secret: "server value"}).encode("utf-8")
        )

        self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text), {"[REDACTED]": "server value"})

    def test_sanitize_url_redacts_credential_shaped_path_segments(self):
        secret = "sk-serversecret123"

        value = api.sanitize_url(
            f"https://example.test/v1/image/tasks/{secret}?token=signed"
        )

        self.assertEqual(
            value, "https://example.test/v1/image/tasks/[REDACTED]"
        )
        self.assertNotIn(secret, value)

    def test_sanitized_diagnostics_contain_no_raw_terminal_controls(self):
        controls = "\x00\x07\x09\x0b\x0c\x0d\x1b\x7f\x80\x85\x9f"
        body = f"before\n{controls}after".encode("utf-8")

        diagnostic = api.sanitize_diagnostic(body)
        server_text = api.sanitize_server_text(f"before\n{controls}after")

        self.assertIn("before\nafter", diagnostic)
        self.assertIn("before\nafter", server_text)
        for value in (diagnostic, server_text):
            for character in controls:
                self.assertNotIn(character, value)


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

    def test_5xx_request_id_is_sanitized(self):
        secret = "sk-requestsecret123"
        text = api.classify_http_error(
            503, {"X-Request-ID": f"request-{secret}"}, "model"
        )

        self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)

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

    def test_parse_json_response_sanitizes_urls_in_non_json_body(self):
        signed = "https://user:pass@example.test/path?token=signed-secret#fragment"
        response = api.ApiResponse(502, {}, f"broken {signed}".encode())

        with self.assertRaises(api.ApiResponseError) as raised:
            api.parse_json_response(response)

        text = str(raised.exception)
        self.assertIn("https://example.test/path", text)
        for forbidden in ("user:pass", "signed-secret", "fragment"):
            self.assertNotIn(forbidden, text)

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
            api,
            "_open_url",
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

    def test_credentials_and_idempotency_key_are_unredirected_headers(self):
        with patch.object(
            api,
            "_open_url",
            return_value=FakeResponse(202, {}, b'{"id": "task_1"}'),
        ) as urlopen:
            api.request_json(
                "POST",
                "https://api.example.test/v1/image/tasks",
                "model-key",
                30,
                {"prompt": "cat"},
                headers={"Idempotency-Key": "request-key"},
            )

        request = urlopen.call_args.args[0]
        lowered = {key.lower(): value for key, value in request.unredirected_hdrs.items()}
        self.assertEqual(lowered["authorization"], "Bearer model-key")
        self.assertEqual(lowered["idempotency-key"], "request-key")
        self.assertNotIn("Authorization", request.headers)

    def test_request_multipart_sets_bearer_header_and_matching_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "source.png"
            image.write_bytes(PNG_BYTES)
            with patch.object(
                api,
                "_open_url",
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
        with patch.object(api, "_open_url", side_effect=error) as urlopen:
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
        with patch.object(api, "_open_url", side_effect=error):
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
            api,
            "_open_url",
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

    def test_plaintext_authenticated_request_is_rejected_before_transport(self):
        with patch.object(api, "_open_url") as open_url:
            with self.assertRaisesRegex(api.ApiUsageError, "HTTPS"):
                api.request_json(
                    "POST", "http://source.example.test/start", "model-key", 5,
                    {"prompt": "cat"},
                )
        open_url.assert_not_called()

    def test_no_redirect_handler_stops_after_one_https_source_request(self):
        counts = {"source": 0, "target": 0}
        received = {"source_auth": None, "target_auth": None}

        class SimulatedTransport(urllib.request.BaseHandler):
            handler_order = 100

            def https_open(self, request):
                if request.host == "target.example.test":
                    counts["target"] += 1
                    received["target_auth"] = request.get_header("Authorization")
                    return urllib.response.addinfourl(
                        io.BytesIO(b"target"), {}, request.full_url, 200
                    )
                counts["source"] += 1
                received["source_auth"] = request.get_header("Authorization")
                headers = email.message.Message()
                headers["Location"] = "https://target.example.test/credential-target"
                response = urllib.response.addinfourl(
                    io.BytesIO(b"redirect"), headers, request.full_url, 302
                )
                response.msg = "Found"
                return response

        opener = urllib.request.build_opener(
            api._NoRedirectHandler(), SimulatedTransport()
        )
        request = urllib.request.Request(
            "https://source.example.test/start", data=b"{}", method="POST"
        )
        request.add_unredirected_header("Authorization", "Bearer model-key")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            opener.open(request)
        raised.exception.close()

        self.assertEqual(counts, {"source": 1, "target": 0})
        self.assertEqual(received["source_auth"], "Bearer model-key")
        self.assertIsNone(received["target_auth"])

    def test_bounded_read_rejects_streams_that_ignore_requested_size(self):
        class IgnoringStream:
            def read(self, _size=None):
                return b"12345"

        with self.assertRaisesRegex(api.ApiResponseError, "超过"):
            api.bounded_read(IgnoringStream(), 4, "测试响应")

    def test_bounded_read_rejects_streams_without_a_size_parameter(self):
        class NoSizeStream:
            def read(self):
                return b"1234"

        with self.assertRaisesRegex(api.ApiResponseError, "读取"):
            api.bounded_read(NoSizeStream(), 4, "测试响应")

    def test_api_success_and_error_bodies_are_bounded(self):
        success = FakeResponse(200, {"Content-Length": "5"}, b"12345")
        with patch.object(api, "MAX_API_BODY_BYTES", 4):
            with patch.object(api, "_open_url", return_value=success):
                with self.assertRaisesRegex(api.ApiResponseError, "超过"):
                    api.request_json(
                        "GET", "https://api.example.test/v1/models", "key", 5
                    )

        error = urllib.error.HTTPError(
            "https://api.example.test/v1/models",
            500,
            "error",
            {"Content-Length": "5"},
            io.BytesIO(b"12345"),
        )
        with patch.object(api, "MAX_ERROR_BODY_BYTES", 4):
            with patch.object(api, "_open_url", side_effect=error):
                with self.assertRaisesRegex(api.ApiResponseError, "超过"):
                    api.request_json(
                        "GET", "https://api.example.test/v1/models", "key", 5
                    )


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

    def test_validate_local_images_reads_a_bounded_immutable_snapshot(self):
        read_sizes = []

        class TrackingStream:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, size=-1):
                read_sizes.append(size)
                return PNG_BYTES

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            source.write_bytes(PNG_BYTES)
            with patch.object(Path, "open", return_value=TrackingStream()):
                validated = api.validate_local_images([source])[0]

        self.assertEqual(read_sizes, [api.MAX_FILE_BYTES + 1])
        self.assertEqual(validated.data, PNG_BYTES)
        self.assertEqual(validated.size, len(PNG_BYTES))

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

            class EmptyWriter:
                def __init__(self, descriptor):
                    self.descriptor = descriptor

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    api.os.close(self.descriptor)

                def write(self, data):
                    return len(data)

                def flush(self):
                    pass

                def fileno(self):
                    return self.descriptor

            with patch.object(
                api.os, "fdopen", side_effect=lambda descriptor, _mode: EmptyWriter(descriptor),
            ):
                with self.assertRaisesRegex(api.ApiResponseError, "为空"):
                    api.save_image_items([item], output, 5)

            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_staged_write_cleanup_error_does_not_hide_write_failure(self):
        item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}

        class FailingWriter:
            def __init__(self, descriptor):
                self.descriptor = descriptor

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                api.os.close(self.descriptor)

            def write(self, _data):
                raise OSError("write marker")

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            with patch.object(
                api.os, "fdopen",
                side_effect=lambda descriptor, _mode: FailingWriter(descriptor),
            ):
                with patch.object(Path, "unlink", side_effect=OSError("cleanup marker")):
                    with self.assertRaisesRegex(OSError, "write marker"):
                        api.save_image_items([item], output, 5)

    def test_save_image_items_rejects_more_than_ten_before_writing(self):
        item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            with self.assertRaisesRegex(api.ApiResponseError, "10"):
                api.save_image_items([item] * 11, output, 5)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_save_image_items_rolls_back_all_outputs_on_aggregate_overflow(self):
        item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "result.png"
            with patch.object(api, "MAX_OUTPUT_BYTES", len(PNG_BYTES) + 1):
                with self.assertRaisesRegex(api.ApiResponseError, "总大小"):
                    api.save_image_items([item, item], output, 5)
            self.assertEqual(list(root.iterdir()), [])

    def test_save_image_items_requires_the_expected_synchronous_count(self):
        item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            with self.assertRaisesRegex(api.ApiResponseError, "2"):
                api.save_image_items([item], output, 5, expected_count=2)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_existing_destination_is_never_moved_before_atomic_replacement(self):
        item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            output.write_bytes(b"previous")
            replace_calls = []
            real_replace = api.os.replace

            def tracked_replace(source, destination):
                replace_calls.append((Path(source), Path(destination)))
                return real_replace(source, destination)

            with patch.object(api.os, "replace", side_effect=tracked_replace):
                api.save_image_items([item], output, 5)

            self.assertFalse(any(source == output for source, _ in replace_calls))
            self.assertTrue(any(destination == output for _, destination in replace_calls))
            self.assertEqual(output.read_bytes(), PNG_BYTES)

    def test_partial_rollback_continues_and_retains_unrestored_backup(self):
        item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "result-1.png"
            second = root / "result-2.png"
            first.write_bytes(b"first-original")
            second.write_bytes(b"second-original")
            real_replace = api.os.replace
            restore_attempts = []

            def failing_replace(source, destination):
                source = Path(source)
                destination = Path(destination)
                if source.name.endswith(".part") and destination == second:
                    raise OSError("promotion marker")
                if source.name.endswith(".backup"):
                    restore_attempts.append(destination)
                    if destination == second:
                        raise OSError("restore marker")
                return real_replace(source, destination)

            with patch.object(api.os, "replace", side_effect=failing_replace):
                try:
                    api.save_image_items([item, item], root / "result.png", 5)
                except Exception as exc:
                    raised = exc
                else:
                    self.fail("promotion failure should be reported")

            self.assertIsInstance(raised, api.ApiResponseError)
            message = str(raised)
            self.assertIn("promotion marker", message)
            self.assertIn("恢复不完整", message)
            self.assertIn("备份已保留", message)
            self.assertEqual(restore_attempts, [second, first])
            self.assertEqual(first.read_bytes(), b"first-original")
            self.assertEqual(second.read_bytes(), b"second-original")
            retained = list(root.glob(".result-2.png.*.backup"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), b"second-original")
            self.assertEqual(list(root.glob("*.part")), [])

    def test_part_cleanup_failures_do_not_stop_later_cleanup_or_hide_primary(self):
        item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_replace = api.os.replace
            real_unlink = Path.unlink
            failed_cleanup = False

            def failing_replace(source, destination):
                source = Path(source)
                destination = Path(destination)
                if source.name.endswith(".part") and destination.name == "result-2.png":
                    raise OSError("primary marker")
                return real_replace(source, destination)

            def failing_unlink(path, *args, **kwargs):
                nonlocal failed_cleanup
                if (
                    not failed_cleanup
                    and path.name.endswith(".part")
                    and api.os.path.lexists(path)
                ):
                    failed_cleanup = True
                    real_unlink(path, *args, **kwargs)
                    raise OSError("cleanup marker")
                return real_unlink(path, *args, **kwargs)

            with patch.object(api.os, "replace", side_effect=failing_replace):
                with patch.object(Path, "unlink", new=failing_unlink):
                    with self.assertRaisesRegex(OSError, "primary marker"):
                        api.save_image_items([item, item, item], root / "result.png", 5)

            self.assertTrue(failed_cleanup)
            self.assertEqual(list(root.glob("*.part")), [])

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_save_image_items_replaces_output_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.png"
            output = root / "result.png"
            target.write_bytes(b"do-not-change")
            output.symlink_to(target)
            item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}

            saved = api.save_image_items(
                [item], output, 5, preserve_requested_path=True
            )

            self.assertFalse(output.is_symlink())
            self.assertEqual(output.read_bytes(), PNG_BYTES)
            self.assertEqual(target.read_bytes(), b"do-not-change")
            self.assertEqual(saved[0].path, output.absolute())
            self.assertEqual(list(root.glob(".result.png.*.part")), [])

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_save_image_items_replaces_a_broken_output_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "result.png"
            output.symlink_to(root / "missing-target.png")
            item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}

            api.save_image_items([item], output, 5, preserve_requested_path=True)

            self.assertFalse(output.is_symlink())
            self.assertEqual(output.read_bytes(), PNG_BYTES)
            self.assertEqual(
                sorted(path.name for path in root.iterdir()), ["result.png"]
            )

    def test_save_image_items_rejects_a_directory_destination_without_moving_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            output.mkdir()
            marker = output / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            item = {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}

            with self.assertRaisesRegex(api.ApiResponseError, "目录"):
                api.save_image_items(
                    [item], output, 5, preserve_requested_path=True
                )

            self.assertTrue(output.is_dir())
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertEqual(
                sorted(path.name for path in Path(temp_dir).iterdir()), ["result.png"]
            )

    def test_result_url_validation_rejects_malformed_and_dangerous_urls_before_transport(self):
        signed_secret = "signed-secret-value"
        cases = (
            "",
            "image.png",
            "http://cdn.example.test/image.png",
            f"https://user:pass@cdn.example.test/image.png?signature={signed_secret}",
            f"https://cdn.example.test/image.png?signature={signed_secret}#fragment",
            "https://cdn.example.test/image.png#",
            f"https://cdn.example.test/im age.png?signature={signed_secret}",
            f"https://cdn.example.test/image.png?signature={signed_secret}\x1b",
            f"https://cdn.example.test:0/image.png?signature={signed_secret}",
            f"https://cdn.example.test:99999/image.png?signature={signed_secret}",
        )
        response = FakeResponse(200, {}, PNG_BYTES)
        with patch.object(
            api.urllib.request, "urlopen", return_value=response
        ) as legacy_open:
            with patch.object(
                api, "_open_result_url", return_value=response, create=True
            ) as result_open:
                for url in cases:
                    with self.subTest(url=url):
                        with self.assertRaises(api.ApiResponseError) as raised:
                            api.download_image(url, 5)
                        self.assertEqual(str(raised.exception), "图片下载地址无效。")
                        self.assertNotIn(signed_secret, str(raised.exception))
        legacy_open.assert_not_called()
        result_open.assert_not_called()

    def test_result_url_validation_rejects_literal_and_resolved_non_global_hosts(self):
        literal_urls = (
            "https://127.0.0.1/image.png",
            "https://10.0.0.1/image.png",
            "https://169.254.1.1/image.png",
            "https://[::1]/image.png",
            "https://224.0.0.1/image.png",
            "https://0.0.0.0/image.png",
            "https://192.0.2.1/image.png",
        )
        response = FakeResponse(200, {}, PNG_BYTES)
        with patch.object(
            api.urllib.request, "urlopen", return_value=response
        ) as legacy_open:
            with patch.object(
                api, "_open_result_url", return_value=response, create=True
            ) as result_open:
                for url in literal_urls:
                    with self.subTest(url=url):
                        with self.assertRaisesRegex(api.ApiResponseError, "不安全"):
                            api.download_image(url, 5)
                with patch.object(
                    socket,
                    "getaddrinfo",
                    return_value=[
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))
                    ],
                ):
                    with self.assertRaisesRegex(api.ApiResponseError, "不安全"):
                        api.download_image("https://cdn.example.test/image.png", 5)
        legacy_open.assert_not_called()
        result_open.assert_not_called()

    def test_result_dns_failure_is_fixed_and_does_not_echo_signed_url(self):
        signed_secret = "dns-signed-secret"
        url = f"https://missing.example.test/image.png?signature={signed_secret}"
        with patch.object(
            socket, "getaddrinfo", side_effect=socket.gaierror(f"failure {url}")
        ):
            response = FakeResponse(200, {}, PNG_BYTES)
            with patch.object(
                api.urllib.request, "urlopen", return_value=response
            ) as legacy_open:
                with patch.object(
                    api, "_open_result_url", return_value=response, create=True
                ) as result_open:
                    with self.assertRaises(api.ApiResponseError) as raised:
                        api.download_image(url, 5)

        self.assertEqual(str(raised.exception), "图片下载地址解析失败。")
        self.assertNotIn(signed_secret, str(raised.exception))
        legacy_open.assert_not_called()
        result_open.assert_not_called()

    def test_result_connection_receives_only_the_validated_address_set(self):
        response = FakeResponse(200, {}, PNG_BYTES)
        resolutions = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]
        with patch.object(socket, "getaddrinfo", return_value=resolutions) as resolve:
            with patch.object(api, "_open_result_url", return_value=response) as open_url:
                api.download_image("https://cdn.example.test/image.png", 5)

        resolve.assert_called_once()
        call = open_url.call_args
        self.assertEqual(call.kwargs.get("host"), "cdn.example.test")
        self.assertEqual(call.kwargs.get("port"), 443)
        self.assertEqual(
            [item.socket_address for item in call.kwargs.get("addresses", ())],
            [("8.8.8.8", 443)],
        )

    def test_pinned_connection_uses_approved_address_without_resolving_again(self):
        connection_type = getattr(api, "_PinnedHTTPSConnection", None)
        self.assertIsNotNone(connection_type)

        class ApprovedAddress:
            family = socket.AF_INET
            socket_type = socket.SOCK_STREAM
            protocol = socket.IPPROTO_TCP
            socket_address = ("8.8.8.8", 443)

        class FakeSocket:
            def __init__(self):
                self.timeout = None
                self.connected_address = None

            def settimeout(self, timeout):
                self.timeout = timeout

            def connect(self, address):
                self.connected_address = address

            def setsockopt(self, *_args):
                pass

            def close(self):
                pass

        class FakeTLSContext:
            def __init__(self):
                self.server_hostname = None

            def wrap_socket(self, raw_socket, *, server_hostname):
                self.server_hostname = server_hostname
                return raw_socket

        raw_socket = FakeSocket()
        tls_context = FakeTLSContext()
        connection = connection_type(
            "cdn.example.test", 443, ApprovedAddress(), timeout=2.5
        )
        connection._context = tls_context

        with patch.object(
            socket,
            "getaddrinfo",
            side_effect=AssertionError("pinned connection performed a second lookup"),
        ) as resolve:
            with patch.object(socket, "socket", return_value=raw_socket) as create_socket:
                connection.connect()

        resolve.assert_not_called()
        create_socket.assert_called_once_with(
            socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP
        )
        self.assertEqual(raw_socket.timeout, 2.5)
        self.assertEqual(raw_socket.connected_address, ("8.8.8.8", 443))
        self.assertEqual(tls_context.server_hostname, "cdn.example.test")
        self.assertIs(connection.sock, raw_socket)

    def test_result_dns_resolution_is_bounded_by_the_absolute_deadline(self):
        release = threading.Event()

        def delayed_resolution(*_args, **_kwargs):
            release.wait(0.3)
            return public_dns_result("cdn.example.test", 443)

        started = time.monotonic()
        deadline = started + 0.05
        try:
            with patch.object(socket, "getaddrinfo", side_effect=delayed_resolution):
                with patch.object(api, "_open_result_url") as open_url:
                    with self.assertRaises(api.ApiResponseError) as raised:
                        api.download_image(
                            "https://cdn.example.test/image.png",
                            5,
                            deadline=deadline,
                            deadline_message="等待任务 task_dns 超过 0.05 秒。",
                        )
        finally:
            release.set()

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(str(raised.exception), "等待任务 task_dns 超过 0.05 秒。")
        self.assertTrue(getattr(raised.exception, "deadline_exceeded", False))
        open_url.assert_not_called()

    def test_download_does_not_follow_redirect_or_read_redirect_body(self):
        class TrackingBody(io.BytesIO):
            read_calls = 0

            def read(self, size=-1):
                self.read_calls += 1
                return super().read(size)

        redirect_body = TrackingBody(b"signed redirect body")
        headers = email.message.Message()
        headers["Location"] = "https://target.example.test/image.png"
        response = urllib.response.addinfourl(
            redirect_body,
            headers,
            "https://source.example.test/start",
            302,
        )
        response.msg = "Found"
        with patch.object(socket, "getaddrinfo", side_effect=public_dns_result):
            with patch.object(api, "_open_result_url", return_value=response) as open_url:
                with self.assertRaisesRegex(api.ApiResponseError, "重定向"):
                    api.download_image("https://source.example.test/start", 5)

        open_url.assert_called_once()
        self.assertEqual(redirect_body.read_calls, 0)

    def test_download_does_not_read_an_error_response_body(self):
        class ErrorResponse(FakeResponse):
            read_calls = 0

            def read(self, size=-1):
                self.read_calls += 1
                return super().read(size)

        response = ErrorResponse(500, {}, b"signed error body")
        with patch.object(socket, "getaddrinfo", side_effect=public_dns_result):
            with patch.object(api, "_open_result_url", return_value=response):
                with self.assertRaisesRegex(api.ApiResponseError, "请求失败"):
                    api.download_image("https://cdn.example.test/image.png", 5)

        self.assertEqual(response.read_calls, 0)

    def test_download_transport_and_http_parser_errors_are_fixed_and_redacted(self):
        signed_secret = "transport-signed-secret"
        url = f"https://cdn.example.test/image.png?signature={signed_secret}"
        failures = (
            http.client.InvalidURL(f"invalid {url}"),
            urllib.error.URLError(f"offline {url}"),
            UnicodeEncodeError("ascii", "é", 0, 1, f"invalid {url}"),
        )
        with patch.object(socket, "getaddrinfo", side_effect=public_dns_result):
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    with patch.object(
                        api.urllib.request, "urlopen", side_effect=failure
                    ):
                        with patch.object(
                            api, "_open_result_url", side_effect=failure, create=True
                        ):
                            try:
                                api.download_image(url, 5)
                            except Exception as exc:
                                self.assertIsInstance(exc, api.ApiResponseError)
                                self.assertEqual(str(exc), "图片下载请求失败。")
                                self.assertNotIn(signed_secret, str(exc))
                            else:
                                self.fail("download_image accepted a transport failure")

    def test_download_image_body_is_bounded(self):
        response = FakeResponse(200, {"Content-Length": "5"}, b"12345")
        with patch.object(api, "MAX_DOWNLOAD_BYTES", 4):
            with patch.object(socket, "getaddrinfo", side_effect=public_dns_result):
                with patch.object(api.urllib.request, "urlopen", return_value=response):
                    with patch.object(
                        api, "_open_result_url", return_value=response, create=True
                    ):
                        with self.assertRaisesRegex(api.ApiResponseError, "超过"):
                            api.download_image("https://cdn.example/image.png", 5)

    def test_result_body_slow_drip_honors_absolute_deadline_and_rolls_back(self):
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        class SlowDripResponse(FakeResponse):
            def __init__(self, clock):
                super().__init__(200, {}, PNG_BYTES)
                self.clock = clock

            def read1(self, size=-1):
                self.clock.now += 0.6
                return self._stream.read(min(size, 1))

        clock = Clock()
        response = SlowDripResponse(clock)
        deadline_message = "等待任务 task_slow_image 超过 1 秒。"
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.png"
            with patch.object(socket, "getaddrinfo", side_effect=public_dns_result):
                with patch.object(api.urllib.request, "urlopen", return_value=response):
                    with patch.object(
                        api, "_open_result_url", return_value=response, create=True
                    ):
                        with self.assertRaisesRegex(
                            api.ApiResponseError, "task_slow_image"
                        ):
                            api.save_image_items(
                                [{"url": "https://cdn.example.test/image.png"}],
                                output,
                                timeout=30,
                                deadline=1.0,
                                deadline_message=deadline_message,
                                monotonic=clock,
                            )

            self.assertFalse(output.exists())

    def test_real_http_response_read_is_interrupted_at_the_absolute_deadline(self):
        server, client = socket.socketpair()
        response = None
        try:
            client.settimeout(0.3)
            server.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nA"
            )
            response = http.client.HTTPResponse(client)
            response.begin()
            started = time.monotonic()
            caught = None
            try:
                api.bounded_read(
                    response,
                    16,
                    "图片下载响应",
                    response.headers,
                    deadline=started + 0.05,
                    deadline_message="等待任务 task_real_stream 超过 0.05 秒。",
                )
            except Exception as exc:
                caught = exc
            else:
                self.fail("real HTTP response exceeded the deadline without failing")
            elapsed = time.monotonic() - started
        finally:
            if response is not None:
                response.close()
            client.close()
            server.close()

        self.assertLess(elapsed, 0.2)
        self.assertIsInstance(caught, api.ApiResponseError)
        self.assertEqual(
            str(caught),
            "等待任务 task_real_stream 超过 0.05 秒。",
        )
        self.assertTrue(getattr(caught, "deadline_exceeded", False))
