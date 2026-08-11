import io
import http.client
import json
import os
import socket
import sys
import tempfile
import time
import threading
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "supertoken-video-generation" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import supertoken_video_api as api
import supertoken_video_config as config


class VideoConfigTests(unittest.TestCase):
    def test_endpoint_url_normalizes_one_v1_prefix(self):
        self.assertEqual(
            api.endpoint_url("https://api.supertoken.cc/v1/", "/v1/video/tasks"),
            "https://api.supertoken.cc/v1/video/tasks",
        )

    def test_key_types_are_rejected_without_echoing_values(self):
        with patch.dict(os.environ, {"SUPERTOKEN_API_KEY": "ak_secret"}, clear=False):
            with self.assertRaises(config.ConfigError) as captured:
                config.get_model_key()
        self.assertNotIn("secret", str(captured.exception))


class VideoTransportTests(unittest.TestCase):
    def test_request_json_sends_json_bearer_and_custom_headers(self):
        response = api.ApiResponse(202, {"Content-Type": "application/json"}, b'{"id":"task_1"}')
        with patch.object(api, "_open_request", return_value=response) as opened:
            actual = api.request_json(
                "POST",
                "https://api.example/v1/video/tasks",
                "sk_test",
                30,
                {"model": "leonardo-seedance-2.5-480p"},
                {"Idempotency-Key": "request-1"},
            )

        self.assertEqual(actual, response)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk_test")
        self.assertEqual(request.get_header("Idempotency-key"), "request-1")
        self.assertEqual(json.loads(request.data), {"model": "leonardo-seedance-2.5-480p"})

    def test_request_json_rejects_non_finite_payload_before_transport(self):
        with patch.object(api, "_open_request") as opened:
            with self.assertRaises(api.ApiUsageError):
                api.request_json(
                    "POST",
                    "https://api.example/v1/video/tasks",
                    "sk_test",
                    30,
                    {"metadata": {"value": float("nan")}},
                )
        opened.assert_not_called()

    def test_request_errors_redact_supplied_credentials(self):
        error = urllib.error.HTTPError(
            "https://api.example/v1/video/tasks",
            400,
            "bad request",
            {},
            io.BytesIO(b"opaque-client-credential"),
        )
        with patch.object(api._OPENER, "open", side_effect=error):
            with self.assertRaises(api.ApiResponseError) as captured:
                api.request_json(
                    "POST",
                    "https://api.example/v1/video/tasks",
                    "opaque-client-credential",
                    30,
                    {"model": "leonardo-seedance-2.5-480p"},
                )
        self.assertNotIn("opaque-client-credential", str(captured.exception))

    def test_request_json_stops_a_trickling_response_at_the_absolute_deadline(self):
        clock = [0.0]

        class TrickleResponse(_Response):
            def __init__(self):
                super().__init__(b"")
                self.chunks = [b"{", b"}", b""]

            def read(self, size=-1):
                clock[0] += 0.6
                return self.chunks.pop(0)

        with patch.object(api._OPENER, "open", return_value=TrickleResponse()):
            with self.assertRaises(api.ApiResponseError) as captured:
                api.request_json(
                    "GET",
                    "https://api.example/v1/video/tasks/task_1",
                    "sk_test",
                    30,
                    deadline=1.0,
                    deadline_message="task wait timeout exceeded",
                    monotonic=lambda: clock[0],
                )

        self.assertTrue(getattr(captured.exception, "deadline_exceeded", False))

    def test_request_json_interrupts_a_blocked_response_at_the_absolute_deadline(self):
        server, client = socket.socketpair()
        response = None
        try:
            client.settimeout(0.5)
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nA")
            response = http.client.HTTPResponse(client)
            response.begin()
            started = time.monotonic()
            with patch.object(api._OPENER, "open", return_value=response):
                with self.assertRaises(api.ApiResponseError) as captured:
                    api.request_json(
                        "GET",
                        "https://api.example/v1/video/tasks/task_1",
                        "sk_test",
                        30,
                        deadline=started + 0.05,
                        deadline_message="task wait timeout exceeded",
                    )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.25)
            self.assertTrue(getattr(captured.exception, "deadline_exceeded", False))
        finally:
            if response is not None:
                response.close()
            client.close()
            server.close()

    def test_request_json_interrupts_a_blocked_open_at_the_absolute_deadline(self):
        opened = threading.Event()
        release = threading.Event()

        def blocking_open(*_args, **_kwargs):
            opened.set()
            release.wait(0.2)
            raise OSError("open stalled")

        started = time.monotonic()
        with patch.object(api._OPENER, "open", side_effect=blocking_open):
            with self.assertRaises(api.ApiResponseError) as captured:
                api.request_json(
                    "GET",
                    "https://api.example/v1/video/tasks/task_1",
                    "sk_test",
                    30,
                    deadline=started + 0.05,
                    deadline_message="task wait timeout exceeded",
                )
        elapsed = time.monotonic() - started

        self.assertTrue(opened.is_set())
        self.assertLess(elapsed, 0.15)
        self.assertTrue(getattr(captured.exception, "deadline_exceeded", False))

    def test_deadline_closes_an_http_error_that_arrives_after_open_timeout(self):
        body = _TrackingBody(b"error")
        late_error = urllib.error.HTTPError(
            "https://api.example/v1/video/tasks/task_1", 500, "server error", {}, body
        )
        opened = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def late_open(*_args, **_kwargs):
            opened.set()
            release.wait(0.2)
            try:
                raise late_error
            finally:
                finished.set()

        with patch.object(api._OPENER, "open", side_effect=late_open):
            with self.assertRaises(api.ApiResponseError) as captured:
                api._open_with_deadline(
                    api.urllib.request.Request("https://api.example/v1/video/tasks/task_1"),
                    30,
                    deadline=time.monotonic() + 0.05,
                    deadline_message="task wait timeout exceeded",
                )

        self.assertTrue(opened.is_set())
        self.assertTrue(getattr(captured.exception, "deadline_exceeded", False))
        release.set()
        self.assertTrue(finished.wait(0.2))
        self.assertTrue(body.closed_by_api)

    def test_deadline_closes_an_http_error_already_queued_by_open(self):
        body = _TrackingBody(b"error")
        queued_error = urllib.error.HTTPError(
            "https://api.example/v1/video/tasks/task_1", 500, "server error", {}, body
        )
        clock_values = iter((0.0, 0.0, 1.0))

        with patch.object(api._OPENER, "open", side_effect=queued_error):
            with self.assertRaises(api.ApiResponseError) as captured:
                api._open_with_deadline(
                    api.urllib.request.Request("https://api.example/v1/video/tasks/task_1"),
                    30,
                    deadline=1.0,
                    deadline_message="task wait timeout exceeded",
                    monotonic=lambda: next(clock_values),
                )

        self.assertTrue(getattr(captured.exception, "deadline_exceeded", False))
        self.assertTrue(body.closed_by_api)

    def test_deadline_closes_an_http_error_enqueued_during_open_timeout_race(self):
        body = _TrackingBody(b"error")
        raced_error = urllib.error.HTTPError(
            "https://api.example/v1/video/tasks/task_1", 500, "server error", {}, body
        )
        allow_worker = threading.Event()
        enqueued = threading.Event()

        class TimeoutRaceQueue:
            def __init__(self, *_args, **_kwargs):
                pass

            def get(self, timeout):
                del timeout
                allow_worker.set()
                if not enqueued.wait(0.2):
                    raise AssertionError("worker did not enqueue the HTTP error")
                raise api.queue.Empty

            def put(self, _value):
                enqueued.set()

        def racing_open(*_args, **_kwargs):
            allow_worker.wait(0.2)
            raise raced_error

        with patch.object(api.queue, "Queue", TimeoutRaceQueue), patch.object(
            api._OPENER, "open", side_effect=racing_open
        ):
            with self.assertRaises(api.ApiResponseError) as captured:
                api._open_with_deadline(
                    api.urllib.request.Request("https://api.example/v1/video/tasks/task_1"),
                    30,
                    deadline=time.monotonic() + 1,
                    deadline_message="task wait timeout exceeded",
                )

        self.assertTrue(getattr(captured.exception, "deadline_exceeded", False))
        self.assertTrue(body.closed_by_api)

    def test_malformed_http_responses_become_api_errors(self):
        with patch.object(api._OPENER, "open", side_effect=http.client.BadStatusLine("bad status")):
            with self.assertRaises(api.ApiResponseError):
                api.request_json(
                    "GET",
                    "https://api.example/v1/video/tasks/task_1",
                    "sk_test",
                    30,
                )
            with self.assertRaises(api.ApiResponseError):
                api._open_public_request(
                    api.urllib.request.Request("https://downloads.example/movie.mp4"),
                    30,
                )

    def test_sanitize_diagnostic_redacts_keys_signed_urls_and_encoded_path_segments(self):
        text = api.sanitize_diagnostic(
            "sk_secret https://user:pass@host/media/ak%255Fserver%255Fsecret/file.mp4?sig=secret#fragment"
        )
        for value in (
            "sk_secret",
            "user:pass",
            "ak%255Fserver%255Fsecret",
            "ak_server_secret",
            "sig=secret",
            "fragment",
        ):
            self.assertNotIn(value, text)
        self.assertIn("[REDACTED]", text)

    def test_parse_json_response_requires_an_object(self):
        with self.assertRaises(api.ApiResponseError):
            api.parse_json_response(api.ApiResponse(200, {}, b"[]"))


class _Response:
    status = 200

    def __init__(self, body, headers=None):
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.body.read(size)


class _TrackingBody(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.closed_by_api = False

    def close(self):
        self.closed_by_api = True
        super().close()


class VideoMediaTransferTests(unittest.TestCase):
    def test_public_media_urls_reject_local_and_private_literal_hosts(self):
        for url in (
            "https://localhost/media.mp4",
            "https://uploads.local/signed",
            "https://127.0.0.1/media.mp4",
            "https://127.1/media.mp4",
            "https://127.0.1/media.mp4",
            "https://2130706433/media.mp4",
            "https://10.0.0.1/media.mp4",
        ):
            with self.subTest(url=url):
                with self.assertRaises(api.ApiUsageError):
                    api.validate_public_url(url)

    def test_presigned_upload_uses_server_method_and_headers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "reference.png"
            source.write_bytes(b"image bytes")
            with patch.object(api, "_open_public_request", return_value=_Response(b"")) as opened:
                result = api.upload_media_files(
                    "https://uploads.example/signed/object?signature=secret",
                    [source],
                    30,
                    headers={"X-Upload-Token": "signed"},
                    method="PATCH",
                )

        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(request.get_header("X-upload-token"), "signed")
        self.assertEqual(request.data, b"image bytes")
        self.assertEqual(result[0]["bytes_written"], len(b"image bytes"))
        self.assertEqual(result[0]["upload_url"], "https://uploads.example/signed/object")

    def test_download_promotes_part_file_without_a_cleanup_subprocess(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "movie.mp4"
            destination.write_bytes(b"old video")
            with patch.object(api, "_open_public_request", return_value=_Response(b"new video")), patch.object(
                api.os,
                "posix_spawn",
                side_effect=AssertionError("must not spawn a cleanup process"),
                create=True,
            ), patch.object(
                api.os,
                "spawnve",
                side_effect=AssertionError("must not spawn a cleanup process"),
                create=True,
            ):
                saved = api.download_video_items(
                    [{"url": "https://downloads.example/movie.mp4"}],
                    temp_dir,
                    30,
                    output_path=destination,
                )

            self.assertEqual(destination.read_bytes(), b"new video")
            self.assertEqual(saved[0]["path"], str(destination.resolve()))
            self.assertEqual(list(Path(temp_dir).glob("*.part")), [])

    def test_failed_download_removes_part_and_preserves_existing_output(self):
        class BrokenResponse(_Response):
            def read(self, size=-1):
                if self.body.tell() == 0:
                    return self.body.read(min(size, 3))
                raise OSError("stream stopped")

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "movie.mp4"
            destination.write_bytes(b"old video")
            with patch.object(api, "_open_public_request", return_value=BrokenResponse(b"new video")):
                with self.assertRaises(api.ApiResponseError):
                    api.download_video_items(
                        [{"url": "https://downloads.example/movie.mp4"}],
                        temp_dir,
                        30,
                        output_path=destination,
                    )

            self.assertEqual(destination.read_bytes(), b"old video")
            self.assertEqual(list(Path(temp_dir).glob("*.part")), [])

    def test_download_stops_trickling_stream_at_deadline_and_preserves_output(self):
        clock = [0.0]

        class TrickleResponse(_Response):
            def __init__(self):
                super().__init__(b"")
                self.chunks = [b"first", b"second", b""]

            def read(self, size=-1):
                clock[0] += 0.6
                return self.chunks.pop(0)

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "movie.mp4"
            destination.write_bytes(b"old video")
            with patch.object(api, "_open_public_request", return_value=TrickleResponse()):
                with self.assertRaises(api.ApiResponseError) as captured:
                    api.download_video_items(
                        [{"url": "https://downloads.example/movie.mp4"}],
                        temp_dir,
                        30,
                        output_path=destination,
                        deadline=1.0,
                        deadline_message="task wait timeout exceeded",
                        monotonic=lambda: clock[0],
                    )

            self.assertTrue(getattr(captured.exception, "deadline_exceeded", False))
            self.assertEqual(destination.read_bytes(), b"old video")
            self.assertEqual(list(Path(temp_dir).glob("*.part")), [])

    def test_download_interrupts_blocked_http_response_at_deadline_and_preserves_output(self):
        server, client = socket.socketpair()
        response = None
        try:
            client.settimeout(0.5)
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nA")
            response = http.client.HTTPResponse(client)
            response.begin()
            with tempfile.TemporaryDirectory() as temp_dir:
                destination = Path(temp_dir) / "movie.mp4"
                destination.write_bytes(b"old video")
                started = time.monotonic()
                with patch.object(api, "_open_public_request", return_value=response):
                    with self.assertRaises(api.ApiResponseError) as captured:
                        api.download_video_items(
                            [{"url": "https://downloads.example/movie.mp4"}],
                            temp_dir,
                            30,
                            output_path=destination,
                            deadline=started + 0.05,
                            deadline_message="task wait timeout exceeded",
                        )
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, 0.25)
                self.assertTrue(getattr(captured.exception, "deadline_exceeded", False))
                self.assertEqual(destination.read_bytes(), b"old video")
                self.assertEqual(list(Path(temp_dir).glob("*.part")), [])
        finally:
            if response is not None:
                response.close()
            client.close()
            server.close()

    def test_download_interrupts_a_blocked_open_at_the_absolute_deadline(self):
        opened = threading.Event()
        release = threading.Event()

        def blocking_open(*_args, **_kwargs):
            opened.set()
            release.wait(0.2)
            raise OSError("open stalled")

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "movie.mp4"
            destination.write_bytes(b"old video")
            started = time.monotonic()
            with patch.object(api._OPENER, "open", side_effect=blocking_open):
                with self.assertRaises(api.ApiResponseError) as captured:
                    api.download_video_items(
                        [{"url": "https://downloads.example/movie.mp4"}],
                        temp_dir,
                        30,
                        output_path=destination,
                        deadline=started + 0.05,
                        deadline_message="task wait timeout exceeded",
                    )
            elapsed = time.monotonic() - started

            self.assertTrue(opened.is_set())
            self.assertLess(elapsed, 0.15)
            self.assertTrue(getattr(captured.exception, "deadline_exceeded", False))
            self.assertEqual(destination.read_bytes(), b"old video")

    def test_download_keeps_a_shorter_socket_timeout_as_a_media_error(self):
        server, client = socket.socketpair()
        response = None
        try:
            client.settimeout(0.05)
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nA")
            response = http.client.HTTPResponse(client)
            response.begin()
            with tempfile.TemporaryDirectory() as temp_dir:
                destination = Path(temp_dir) / "movie.mp4"
                destination.write_bytes(b"old video")
                started = time.monotonic()
                with patch.object(api, "_open_public_request", return_value=response):
                    with self.assertRaises(api.ApiResponseError) as captured:
                        api.download_video_items(
                            [{"url": "https://downloads.example/movie.mp4"}],
                            temp_dir,
                            30,
                            output_path=destination,
                            deadline=started + 1.0,
                            deadline_message="task wait timeout exceeded",
                        )

                self.assertFalse(getattr(captured.exception, "deadline_exceeded", False))
                self.assertEqual(str(captured.exception), "could not read video download")
                self.assertEqual(destination.read_bytes(), b"old video")
                self.assertEqual(list(Path(temp_dir).glob("*.part")), [])
        finally:
            if response is not None:
                response.close()
            client.close()
            server.close()

    def test_download_checks_deadline_after_contexts_close_before_replacing_output(self):
        clock = [0.0]

        class ClosingResponse(_Response):
            def __exit__(self, *_args):
                clock[0] = 1.0
                return False

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "movie.mp4"
            destination.write_bytes(b"old video")
            with patch.object(api, "_open_public_request", return_value=ClosingResponse(b"new video")), patch.object(
                api.os, "replace"
            ) as replace:
                with self.assertRaises(api.ApiResponseError) as captured:
                    api.download_video_items(
                        [{"url": "https://downloads.example/movie.mp4"}],
                        temp_dir,
                        30,
                        output_path=destination,
                        deadline=1.0,
                        deadline_message="task wait timeout exceeded",
                        monotonic=lambda: clock[0],
                    )

            self.assertTrue(getattr(captured.exception, "deadline_exceeded", False))
            replace.assert_not_called()
            self.assertEqual(destination.read_bytes(), b"old video")
            self.assertEqual(list(Path(temp_dir).glob("*.part")), [])

    def test_download_adds_resource_authorization_only_when_requested(self):
        seen = []

        def opened(request, _timeout, **_deadline_options):
            seen.append(
                {
                    "authorization": request.get_header("Authorization"),
                    "user_agent": request.get_header("User-agent"),
                }
            )
            return _Response(b"video")

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(api, "_open_public_request", side_effect=opened):
                api.download_video_items(
                    [{"url": "https://downloads.example/public.mp4", "url_auth": "none"}],
                    temp_dir,
                    30,
                    "ak_test",
                )
                api.download_video_items(
                    [{"url": "https://downloads.example/protected.mp4", "url_auth": "resource_api_key"}],
                    temp_dir,
                    30,
                    "ak_test",
                )

        self.assertEqual(
            seen,
            [
                {"authorization": None, "user_agent": "Mozilla/5.0"},
                {"authorization": "Bearer ak_test", "user_agent": "Mozilla/5.0"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
