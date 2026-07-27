import base64
import email.message
import io
import json
import sys
import tempfile
import urllib.error
import urllib.response
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
        self._stream = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        return self._stream.read(size)

    def geturl(self):
        return "https://api.example.test/response"


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
            image.write_bytes(b"image-bytes")
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

    def test_download_rejects_a_redirect_to_http(self):
        response = MagicMock()
        response.geturl.return_value = "http://cdn.example/image.png"
        response.__enter__.return_value = response
        with patch.object(api.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(api.ApiResponseError, "HTTPS"):
                api.download_image("https://cdn.example/start", 5)

    def test_download_image_body_is_bounded(self):
        response = FakeResponse(200, {"Content-Length": "5"}, b"12345")
        with patch.object(api, "MAX_DOWNLOAD_BYTES", 4):
            with patch.object(api.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(api.ApiResponseError, "超过"):
                    api.download_image("https://cdn.example/image.png", 5)
