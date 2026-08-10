import base64
import json
import io
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
    def test_request_json_rejects_non_finite_payload_values_before_transport(self):
        with patch.object(api, "_open_request") as opened:
            with self.assertRaises(api.ApiUsageError):
                api.request_json(
                    "POST", "https://api.example/v1/video/tasks", "sk_test", 30,
                    {"metadata": {"value": float("nan")}},
                )
        opened.assert_not_called()

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

    def test_diagnostics_redact_repeatedly_escaped_and_percent_encoded_credentials(self):
        opaque_key = "opaque-client-credential"
        json_escaped = json.dumps({"detail": r"opaque\u002dclient\u002dcredential"})
        percent_encoded = "opaque%252Dclient%252Dcredential"
        encoded_key_shaped_value = "sk%255Fserver%255Fsecret"

        escaped_text = api.sanitize_diagnostic(json_escaped, opaque_key)
        percent_text = api.sanitize_diagnostic(f"server echo {percent_encoded}", opaque_key)
        key_shaped_text = api.sanitize_diagnostic(
            f"server echo {encoded_key_shaped_value}"
        )

        for forbidden in (
            opaque_key,
            r"opaque\u002dclient\u002dcredential",
            percent_encoded,
            encoded_key_shaped_value,
            "sk_server_secret",
        ):
            self.assertNotIn(forbidden, escaped_text + percent_text + key_shaped_text)
        self.assertIn("[REDACTED]", escaped_text)
        self.assertIn("[REDACTED]", percent_text)
        self.assertIn("[REDACTED]", key_shaped_text)

    def test_diagnostics_redact_more_than_twenty_percent_encoded_layers(self):
        opaque_key = "opaque/client/credential"
        encoded = opaque_key
        for _ in range(21):
            encoded = urllib.parse.quote(encoded, safe="")

        text = api.sanitize_diagnostic(f"server echo {encoded}", opaque_key)

        self.assertNotIn(opaque_key, text)
        self.assertNotIn(encoded, text)
        self.assertIn("[REDACTED]", text)

    def test_diagnostics_redact_values_under_escaped_sensitive_json_field_names(self):
        opaque_key = "opaque-server-credential"
        payload = json.dumps({r"tok\u0065n": opaque_key})

        text = api.sanitize_diagnostic(payload)

        self.assertNotIn(opaque_key, text)
        self.assertIn("[REDACTED]", text)

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

    def test_unparseable_http_error_does_not_echo_json_escaped_opaque_key(self):
        opaque_key = "opaque-client-credential"
        escaped_key = "opaque\\u002dclient\\u002dcredential"
        error = urllib.error.HTTPError(
            "https://api.example/v1/video/tasks", 400, "bad request", {},
            io.BytesIO(escaped_key.encode("ascii")),
        )
        with patch.object(api._OPENER, "open", side_effect=error):
            with self.assertRaises(api.ApiResponseError) as captured:
                api.request_json(
                    "POST", "https://api.example/v1/video/tasks", opaque_key, 30,
                    {"model": "adobe-kling-3.0-720p"},
                )
        self.assertNotIn(opaque_key, str(captured.exception))
        self.assertNotIn(escaped_key, str(captured.exception))

    def test_malformed_success_response_redacts_opaque_submitted_api_key(self):
        class Response:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1):
                return self.body.read(size)

            def __init__(self, body):
                self.body = io.BytesIO(body)

        opaque_key = "opaque-client-credential"
        with patch.object(api._OPENER, "open", return_value=Response(opaque_key.encode("utf-8"))):
            response = api.request_json(
                "POST", "https://api.example/v1/video/tasks", opaque_key, 30,
                {"model": "adobe-kling-3.0-720p"},
            )
        with self.assertRaises(api.ApiResponseError) as captured:
            api.parse_json_response(response)
        self.assertNotIn(opaque_key, str(captured.exception))

    def test_malformed_success_response_does_not_echo_json_escaped_opaque_key(self):
        class Response:
            status = 200
            headers = {}

            def __init__(self, body):
                self.body = io.BytesIO(body)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1):
                return self.body.read(size)

        opaque_key = "opaque-client-credential"
        escaped_key = "opaque\\u002dclient\\u002dcredential"
        with patch.object(api._OPENER, "open", return_value=Response(escaped_key.encode("ascii"))):
            response = api.request_json(
                "POST", "https://api.example/v1/video/tasks", opaque_key, 30,
                {"model": "adobe-kling-3.0-720p"},
            )
        with self.assertRaises(api.ApiResponseError) as captured:
            api.parse_json_response(response)
        self.assertNotIn(escaped_key, str(captured.exception))


class VideoMediaTransferTests(unittest.TestCase):
    def assert_cleanup_eventually_finishes(self, directory):
        deadline = time.monotonic() + 1.0
        while list(Path(directory).iterdir()) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(list(Path(directory).iterdir()), [])

    def test_public_transfers_reject_reserved_and_resolved_private_targets_before_transport(self):
        class Response:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return b""

        private_dns_result = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.1.2.3", 443))
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mp4"
            source.write_bytes(b"video")
            with patch.object(api._OPENER, "open", return_value=Response()) as legacy_open:
                for host in ("localhost", "localhost.", "uploads.localhost", "uploads.local"):
                    with self.subTest(host=host):
                        with self.assertRaises(api.ApiUsageError):
                            api.upload_media_files(f"https://{host}/signed", [source], 30)
                        with self.assertRaises(api.ApiUsageError):
                            api.download_video_items(
                                [{
                                    "url": f"https://{host}/video.mp4",
                                    "url_auth": "resource_api_key",
                                }],
                                temp_dir,
                                30,
                                "opaque-resource-key",
                            )
                with patch("socket.getaddrinfo", return_value=private_dns_result) as resolve:
                    with self.assertRaises(api.ApiUsageError):
                        api.upload_media_files("https://uploads.example/signed", [source], 30)
                    with self.assertRaises(api.ApiUsageError):
                        api.download_video_items(
                            [{
                                "url": "https://downloads.example/video.mp4",
                                "url_auth": "resource_api_key",
                            }],
                            temp_dir,
                            30,
                            "opaque-resource-key",
                        )
                self.assertGreaterEqual(resolve.call_count, 2)
        legacy_open.assert_not_called()

    def test_protected_download_uses_only_validated_pinned_addresses(self):
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

        resolution = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("socket.getaddrinfo", return_value=resolution) as resolve, patch.object(
                api, "_open_pinned_public_request", create=True, return_value=Response(b"video")
            ) as pinned_open, patch.object(api._OPENER, "open", return_value=Response(b"video")) as legacy_open:
                api.download_video_items(
                    [{
                        "url": "https://cdn.example/video.mp4?signature=opaque",
                        "filename": "video.mp4",
                        "url_auth": "resource_api_key",
                    }],
                    temp_dir,
                    30,
                    "opaque-resource-key",
                )

        resolve.assert_called_once()
        legacy_open.assert_not_called()
        pinned_open.assert_called_once()
        call = pinned_open.call_args
        self.assertEqual(call.kwargs["host"], "cdn.example")
        self.assertEqual(call.kwargs["port"], 443)
        self.assertEqual(
            [address.socket_address for address in call.kwargs["addresses"]],
            [("8.8.8.8", 443)],
        )
        self.assertEqual(
            call.kwargs["request"].get_header("Authorization"),
            "Bearer opaque-resource-key",
        )

    def test_pinned_connection_preserves_original_hostname_without_a_second_lookup(self):
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
            "cdn.example", 443, ApprovedAddress(), timeout=2.5
        )
        connection._context = tls_context

        with patch("socket.getaddrinfo", side_effect=AssertionError("pinned connection resolved again")) as resolve:
            with patch("socket.socket", return_value=raw_socket) as create_socket:
                connection.connect()

        resolve.assert_not_called()
        create_socket.assert_called_once_with(
            socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP
        )
        self.assertEqual(raw_socket.timeout, 2.5)
        self.assertEqual(raw_socket.connected_address, ("8.8.8.8", 443))
        self.assertEqual(tls_context.server_hostname, "cdn.example")

    def test_signed_chunked_upload_uses_chunk_framing_without_content_length(self):
        class RecordingConnection(http.client.HTTPConnection):
            def __init__(self):
                super().__init__("uploads.example")
                self.sent = []

            def send(self, data):
                self.sent.append(data)

        request = api._PresignedUploadRequest(
            "https://uploads.example/signed?signature=opaque",
            data=b"video",
            method="PUT",
        )
        request.add_header("Transfer-Encoding", "chunked")
        connection = RecordingConnection()

        api._send_pinned_request(connection, request, "/signed?signature=opaque")

        headers, body = b"".join(connection.sent).split(b"\r\n\r\n", 1)
        normalized_headers = headers.lower()
        self.assertIn(b"transfer-encoding: chunked", normalized_headers)
        self.assertNotIn(b"content-length:", normalized_headers)
        self.assertEqual(body, b"5\r\nvideo\r\n0\r\n\r\n")

    def test_download_interrupts_a_blocking_read_at_deadline_and_cleans_staged_output(self):
        class BlockingResponse:
            status = 200
            headers = {}

            def __init__(self):
                self.release = threading.Event()
                self.read_started = threading.Event()
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()
                return False

            def close(self):
                self.closed = True
                self.release.set()

            def read(self, _size=-1):
                self.read_started.set()
                self.release.wait(0.5)
                return b""

        response = BlockingResponse()
        with tempfile.TemporaryDirectory() as temp_dir:
            started = time.monotonic()
            with patch.object(api, "_open_public_request", return_value=response):
                with self.assertRaisesRegex(api.ApiResponseError, "deadline"):
                    api.download_video_items(
                        [{"url": "https://cdn.example/video.mp4", "filename": "video.mp4"}],
                        temp_dir,
                        30,
                        deadline=started + 0.05,
                    )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.25)
            self.assertTrue(response.read_started.is_set())
            self.assertTrue(response.closed)
            self.assert_cleanup_eventually_finishes(temp_dir)

    def test_detached_cleanup_returns_at_deadline_then_removes_staged_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            part = root / ".video.part"
            backup = root / ".video.backup"
            part.write_bytes(b"partial")
            backup.write_bytes(b"original")
            actions = [
                api._cleanup_unlink_action(part),
                api._cleanup_unlink_action(backup),
            ]

            started = time.monotonic()
            with self.assertRaisesRegex(api.ApiResponseError, "deadline"):
                api._execute_cleanup_plan(
                    actions,
                    deadline=started + 0.02,
                    deadline_message="deadline exceeded",
                    not_before=time.time() + 0.12,
                )
            self.assertLess(time.monotonic() - started, 0.1)

            deadline = time.monotonic() + 1.0
            while (part.exists() or backup.exists()) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(part.exists())
            self.assertFalse(backup.exists())

    def test_detached_cleanup_survives_parent_exit_without_a_live_popen_warning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            part = Path(temp_dir) / ".video.part"
            script = f'''
import sys
import time
from pathlib import Path

sys.path.insert(0, {str(SCRIPTS)!r})
import supertoken_video_api as api

part = Path({str(part)!r})
part.write_bytes(b"partial")
try:
    api._execute_cleanup_plan(
        [api._cleanup_unlink_action(part)],
        deadline=time.monotonic() + 0.01,
        deadline_message="deadline exceeded",
        not_before=time.time() + 0.4,
    )
except api.ApiResponseError:
    pass
'''
            completed = subprocess.run(
                [sys.executable, "-W", "error::ResourceWarning", "-c", script],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("ResourceWarning", completed.stderr)
            deadline = time.monotonic() + 1.0
            while part.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(part.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX cleanup uses a detached helper")
    def test_detached_cleanup_does_not_depend_on_parent_stdin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            part = Path(temp_dir) / ".video.part"
            script = f'''
import os
import sys
from pathlib import Path

sys.path.insert(0, {str(SCRIPTS)!r})
import supertoken_video_api as api

os.close(0)
part = Path({str(part)!r})
part.write_bytes(b"partial")
api._execute_cleanup_plan([api._cleanup_unlink_action(part)])
if part.exists():
    raise SystemExit("cleanup did not remove staged output")
'''
            completed = subprocess.run(
                [sys.executable, "-W", "error::ResourceWarning", "-c", script],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(part.exists())

    def test_cleanup_handoff_does_not_block_on_plan_fsync_after_deadline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            part = Path(temp_dir) / ".video.part"
            part.write_bytes(b"partial")
            started = time.monotonic()
            with patch.object(api.os, "fsync", side_effect=lambda _fd: time.sleep(0.2)):
                with self.assertRaisesRegex(api.ApiResponseError, "deadline"):
                    api._execute_cleanup_plan(
                        [api._cleanup_unlink_action(part)],
                        deadline=started + 0.01,
                        deadline_message="deadline exceeded",
                    )
            self.assertLess(time.monotonic() - started, 0.1)
            self.assert_cleanup_eventually_finishes(temp_dir)

    @unittest.skipUnless(os.name == "posix", "POSIX cleanup tracks detached PIDs")
    def test_detached_cleanup_reaps_completed_worker_without_a_later_launch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            part = Path(temp_dir) / ".video.part"
            part.write_bytes(b"partial")
            existing = set(api._DETACHED_CLEANUP_PIDS)
            started = time.monotonic()
            with self.assertRaisesRegex(api.ApiResponseError, "deadline"):
                api._execute_cleanup_plan(
                    [api._cleanup_unlink_action(part)],
                    deadline=started + 0.01,
                    deadline_message="deadline exceeded",
                    not_before=time.time() + 0.1,
                )
            pending = set(api._DETACHED_CLEANUP_PIDS) - existing
            self.assertEqual(len(pending), 1)
            process_id = pending.pop()
            deadline = time.monotonic() + 1.0
            while process_id in api._DETACHED_CLEANUP_PIDS and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertNotIn(process_id, api._DETACHED_CLEANUP_PIDS)
            with self.assertRaises(ChildProcessError):
                os.waitpid(process_id, os.WNOHANG)

    def test_detached_cleanup_does_not_clobber_replaced_staged_or_retry_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            part = root / ".video.part"
            backup = root / ".video.backup"
            destination = root / "video.mp4"
            part.write_bytes(b"old staged")
            backup.write_bytes(b"original")
            destination.write_bytes(b"failed output")
            actions = [
                api._cleanup_unlink_action(part),
                api._cleanup_restore_action(backup, destination),
            ]

            part.unlink()
            part.write_bytes(b"retry staged")
            destination.unlink()
            destination.write_bytes(b"retry output")

            with self.assertRaisesRegex(api.ApiResponseError, "cleanup"):
                api._execute_cleanup_plan(actions)

            self.assertEqual(part.read_bytes(), b"retry staged")
            self.assertEqual(destination.read_bytes(), b"retry output")
            self.assertEqual(backup.read_bytes(), b"original")

    def test_same_cli_retry_is_blocked_by_pending_detached_cleanup_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "video.mp4"
            part = Path(temp_dir) / ".video.part"
            part.write_bytes(b"partial")
            locks = api._acquire_cleanup_locks([destination])
            with self.assertRaisesRegex(api.ApiResponseError, "deadline"):
                api._execute_cleanup_plan(
                    [api._cleanup_unlink_action(part)],
                    finalizers=[api._cleanup_unlink_action(lock) for lock in locks],
                    deadline=time.monotonic(),
                    deadline_message="deadline exceeded",
                    not_before=time.time() + 0.2,
                )
            with patch.object(api, "_stage_download") as staged:
                with self.assertRaisesRegex(api.ApiResponseError, "cleanup"):
                    api.download_video_items(
                        [{"url": "https://cdn.example/video.mp4", "filename": "video.mp4"}],
                        temp_dir,
                        30,
                    )
            staged.assert_not_called()
            self.assert_cleanup_eventually_finishes(temp_dir)

    def test_cleanup_child_environment_carries_only_encoded_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".video.part"
            path.write_bytes(b"partial")
            action = api._cleanup_unlink_action(path)
            with patch.dict(
                os.environ,
                {
                    "SUPERTOKEN_API_KEY": "sk-secret",
                    "SUPERTOKEN_RESOURCE_API_KEY": "ak-secret",
                },
                clear=False,
            ):
                payload = api._encode_cleanup_plan([action], (), 0)
                environment = api._cleanup_child_environment(payload)

        self.assertLessEqual(
            len(payload.encode("ascii")), api.MAX_CLEANUP_PLAN_ENV_BYTES,
        )
        self.assertEqual(environment["SUPERTOKEN_VIDEO_CLEANUP_PLAN"], payload)
        self.assertNotIn("SUPERTOKEN_API_KEY", environment)
        self.assertNotIn("SUPERTOKEN_RESOURCE_API_KEY", environment)
        self.assertNotIn(str(path), "\n".join(environment.values()))
        decoded_plan = json.loads(base64.b64decode(payload.encode("ascii")))
        self.assertEqual(decoded_plan["actions"], [action])
        self.assertNotIn(str(path), json.dumps(decoded_plan))

    def test_oversized_cleanup_plan_is_rejected_before_process_spawn(self):
        path_token = base64.b64encode(
            ("/tmp/" + "x" * api.MAX_CLEANUP_PLAN_ENV_BYTES).encode("ascii")
        ).decode("ascii")
        action = {
            "op": "unlink",
            "path": path_token,
            "identity": [1, 1, 0],
        }
        with patch.object(api, "_spawn_posix_cleanup_process") as posix_spawn, patch.object(
            api, "_spawn_cleanup_process_without_posix_spawn"
        ) as fallback_spawn:
            with self.assertRaisesRegex(api.ApiResponseError, "plan is too large"):
                api._execute_cleanup_plan([action])

        posix_spawn.assert_not_called()
        fallback_spawn.assert_not_called()

    def test_non_posix_cleanup_spawn_prefers_waitable_pnowait(self):
        with patch.object(api.os, "P_NOWAIT", 71, create=True), patch.object(
            api.os, "P_DETACH", 72, create=True,
        ), patch.object(api.os, "spawnve", return_value=1234) as spawned:
            process_id, waitable = api._spawn_cleanup_process_without_posix_spawn(
                ["cleanup-helper"], {"SAFE": "1"},
            )

        self.assertEqual(process_id, 1234)
        self.assertTrue(waitable)
        self.assertEqual(spawned.call_args.args[0], 71)

    @unittest.skipUnless(os.name == "nt", "requires Windows P_NOWAIT semantics")
    def test_windows_pnowait_child_exit_is_waitable(self):
        command = [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys; sys.exit(7)",
        ]
        process_id, waitable = api._spawn_cleanup_process_without_posix_spawn(
            command,
            api._cleanup_child_environment("ignored"),
        )
        status = api._wait_for_non_posix_cleanup_pid(process_id)

        self.assertTrue(waitable)
        self.assertNotEqual(status, 0)

    def test_non_posix_waitable_cleanup_child_reports_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            part = Path(temp_dir) / ".video.part"
            part.write_bytes(b"partial")
            action = api._cleanup_unlink_action(part)
            with patch.object(api.os, "name", "nt"), patch.object(
                api, "_spawn_cleanup_process_without_posix_spawn", return_value=(1234, True),
            ), patch.object(
                api, "_wait_for_non_posix_cleanup_pid", return_value=1,
            ) as waited:
                with self.assertRaisesRegex(api.ApiResponseError, "cleanup"):
                    api._execute_cleanup_plan([action])

        waited.assert_called_once_with(
            1234,
            deadline=None,
            deadline_message=None,
            monotonic=time.monotonic,
        )

    def test_non_posix_unwaitable_cleanup_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            part = Path(temp_dir) / ".video.part"
            part.write_bytes(b"partial")
            action = api._cleanup_unlink_action(part)
            with patch.object(api.os, "name", "nt"), patch.object(
                api, "_spawn_cleanup_process_without_posix_spawn", return_value=(0, False),
            ):
                with self.assertRaisesRegex(api.ApiResponseError, "monitored"):
                    api._execute_cleanup_plan(
                        [action], deadline=time.monotonic() + 1.0,
                    )

    def test_non_posix_cleanup_deadline_returns_while_reaper_drains_pid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            part = Path(temp_dir) / ".video.part"
            part.write_bytes(b"partial")
            action = api._cleanup_unlink_action(part)
            process_id = 8675309
            existing = set(api._NON_POSIX_CLEANUP_PIDS)
            entered_wait = threading.Event()
            release_wait = threading.Event()

            def blocking_waitpid(pid, options):
                self.assertEqual(pid, process_id)
                self.assertEqual(options, 0)
                entered_wait.set()
                release_wait.wait(0.5)
                return pid, 0

            started = time.monotonic()
            try:
                with patch.object(api.os, "name", "nt"), patch.object(
                    api, "_spawn_cleanup_process_without_posix_spawn",
                    return_value=(process_id, True),
                ), patch.object(api.os, "waitpid", side_effect=blocking_waitpid):
                    with self.assertRaisesRegex(api.ApiResponseError, "deadline"):
                        api._execute_cleanup_plan(
                            [action],
                            deadline=started + 0.03,
                            deadline_message="deadline exceeded",
                        )
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertTrue(entered_wait.is_set())
                self.assertIn(process_id, api._NON_POSIX_CLEANUP_PIDS - existing)
            finally:
                release_wait.set()

            deadline = time.monotonic() + 1.0
            while process_id in api._NON_POSIX_CLEANUP_PIDS and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertNotIn(process_id, api._NON_POSIX_CLEANUP_PIDS)

    def test_download_rejects_cleanup_plan_overflow_before_locks_or_staging(self):
        items = [
            {
                "url": f"https://cdn.example/video-{index}.mp4",
                "filename": f"video-{index}.mp4",
            }
            for index in range(200)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(api, "_stage_download") as staged:
                with self.assertRaisesRegex(api.ApiResponseError, "plan is too large"):
                    api.download_video_items(items, temp_dir, 30)

            staged.assert_not_called()
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX cleanup uses posix_spawn")
    def test_detached_cleanup_child_receives_only_encoded_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".video.part"
            path.write_bytes(b"partial")
            action = api._cleanup_unlink_action(path)
            with patch.dict(
                os.environ,
                {
                    "SUPERTOKEN_API_KEY": "sk-secret",
                    "SUPERTOKEN_RESOURCE_API_KEY": "ak-secret",
                },
                clear=False,
            ), patch.object(api.os, "posix_spawn", return_value=1234) as launched, patch.object(
                api, "_wait_for_detached_cleanup_pid", return_value=0,
            ):
                api._execute_cleanup_plan([action])

        command = launched.call_args.args[1]
        environment = launched.call_args.args[2]
        options = launched.call_args.kwargs
        self.assertIn("-I", command)
        self.assertIn("-S", command)
        self.assertIn("-c", command)
        self.assertNotIn("sk-secret", " ".join(command))
        self.assertNotIn("ak-secret", " ".join(command))
        self.assertNotIn("SUPERTOKEN_API_KEY", environment)
        self.assertNotIn("SUPERTOKEN_RESOURCE_API_KEY", environment)
        self.assertEqual(
            set(environment) - {"SYSTEMROOT", "WINDIR"},
            {"SUPERTOKEN_VIDEO_CLEANUP_PLAN"},
        )
        self.assertTrue(options["setsid"])
        self.assertTrue(any(
            action[0] == os.POSIX_SPAWN_OPEN and action[1] == 0
            for action in options["file_actions"]
        ))
        self.assertNotIn("--manifest", command)
        self.assertNotIn(str(path), " ".join(command))
        payload = environment["SUPERTOKEN_VIDEO_CLEANUP_PLAN"]
        self.assertLessEqual(
            len(payload.encode("ascii")), api.MAX_CLEANUP_PLAN_ENV_BYTES,
        )
        self.assertNotIn(str(path), "\n".join(environment.values()))
        decoded_plan = json.loads(base64.b64decode(payload.encode("ascii")))
        self.assertEqual(decoded_plan["actions"], [action])
        self.assertNotIn(str(path), json.dumps(decoded_plan))

    @unittest.skipUnless(os.name == "posix", "POSIX cleanup uses posix_spawn")
    def test_posix_cleanup_falls_back_when_spawn_does_not_accept_setsid(self):
        with patch.object(api.os, "posix_spawn", side_effect=[TypeError, 1234]) as spawned:
            process_id = api._spawn_posix_cleanup_process(["cleanup-helper"], {})

        self.assertEqual(process_id, 1234)
        self.assertEqual(spawned.call_count, 2)
        self.assertTrue(spawned.call_args_list[0].kwargs["setsid"])
        self.assertNotIn("setsid", spawned.call_args_list[1].kwargs)

    def test_upload_uses_signed_method_and_headers_without_resource_authorization(self):
        class Response:
            status = 204
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return b""

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mp4"
            source.write_bytes(b"video")
            with patch.object(api, "_open_public_request", return_value=Response()) as opened:
                api.upload_media_files(
                    "https://uploads.example/signed", [source], 30,
                    headers={"Content-Type": "video/custom", "X-Upload-Token": "signed"},
                    method="PATCH+SIGNED",
                )
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH+SIGNED")
        self.assertEqual(request.get_header("Content-type"), "video/custom")
        self.assertEqual(request.get_header("X-upload-token"), "signed")
        self.assertIsNone(request.get_header("Authorization"))

    def test_empty_signed_headers_do_not_inject_content_type_at_handler_boundary(self):
        class Response:
            status = 204
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return b""

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mp4"
            source.write_bytes(b"video")
            with patch.object(api, "_open_public_request", return_value=Response()) as opened:
                api.upload_media_files(
                    "https://uploads.example/signed", [source], 30,
                    headers={}, method="PATCH+SIGNED",
                )
        request = opened.call_args.args[0]
        handler = urllib.request.AbstractHTTPHandler()
        handler.parent = type("Parent", (), {"addheaders": []})()
        outgoing = handler.do_request_(request)
        self.assertEqual(outgoing.get_method(), "PATCH+SIGNED")
        self.assertFalse(any(name.lower() == "content-type" for name, _value in outgoing.header_items()))
        self.assertFalse(any(name.lower() == "authorization" for name, _value in outgoing.header_items()))

    def test_download_deadline_after_fsync_cleans_output_before_promotion(self):
        class Clock:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                return self.value

        class Response:
            status = 200
            headers = {}

            def __init__(self):
                self.chunks = [b"video", b""]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return self.chunks.pop(0)

        clock = Clock()
        with tempfile.TemporaryDirectory() as temp_dir:
            def advance_after_sync(_descriptor):
                clock.value = 1.1

            with patch.object(api, "_open_public_request", return_value=Response()), patch.object(api.os, "fsync", side_effect=advance_after_sync):
                with self.assertRaises(api.ApiResponseError):
                    api.download_video_items(
                        [{"url": "https://cdn.example/video.mp4", "filename": "video.mp4"}],
                        temp_dir, 30, deadline=1.0, monotonic=clock,
                    )
            self.assert_cleanup_eventually_finishes(temp_dir)

    def test_download_deadline_before_promotion_cleans_staged_output(self):
        class Clock:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                return self.value

        clock = Clock()
        with tempfile.TemporaryDirectory() as temp_dir:
            part = Path(temp_dir) / ".video.part"
            part.write_bytes(b"video")

            def staged_download(*_args, **_kwargs):
                clock.value = 1.1
                return part, 5

            with patch.object(api, "_stage_download", side_effect=staged_download):
                with self.assertRaises(api.ApiResponseError):
                    api.download_video_items(
                        [{"url": "https://cdn.example/video.mp4", "filename": "video.mp4"}],
                        temp_dir, 30, deadline=1.0, monotonic=clock,
                    )
            self.assert_cleanup_eventually_finishes(temp_dir)

    def test_download_deadline_after_final_eof_read_cleans_staged_output(self):
        class Clock:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                return self.value

        class Response:
            status = 200
            headers = {}

            def __init__(self, clock):
                self.clock = clock
                self.chunks = [b"video", b""]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                self.clock.value += 0.6
                return self.chunks.pop(0)

        clock = Clock()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(api, "_open_public_request", return_value=Response(clock)):
                with self.assertRaises(api.ApiResponseError):
                    api.download_video_items(
                        [{"url": "https://cdn.example/video.mp4", "filename": "video.mp4"}],
                        temp_dir, 30, deadline=1.0, monotonic=clock,
                    )
            self.assert_cleanup_eventually_finishes(temp_dir)

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
                api, "_open_public_request",
                side_effect=[Response(b"first"), Response(b"second"), Response(b"third"), Response(b"fourth")],
            ) as opened:
                api.download_video_items([
                    {"url": "https://cdn.example/public.mp4", "filename": "public.mp4"},
                    {"url": "https://cdn.example/null.mp4", "filename": "null.mp4", "url_auth": None},
                    {"url": "https://cdn.example/public.mp4?temporary=opaque", "filename": "explicit-none.mp4", "url_auth": "none"},
                    {"url": "https://cdn.example/private.mp4", "filename": "private.mp4", "url_auth": "resource_api_key"},
                ], temp_dir, 30, "opaque-resource-key")
        first = opened.call_args_list[0].args[0]
        second = opened.call_args_list[1].args[0]
        third = opened.call_args_list[2].args[0]
        fourth = opened.call_args_list[3].args[0]
        self.assertIsNone(first.get_header("Authorization"))
        self.assertIsNone(second.get_header("Authorization"))
        self.assertIsNone(third.get_header("Authorization"))
        self.assertEqual(third.full_url, "https://cdn.example/public.mp4?temporary=opaque")
        handler = urllib.request.AbstractHTTPHandler()
        handler.parent = type("Parent", (), {"addheaders": []})()
        outgoing = handler.do_request_(third)
        self.assertFalse(any(name.lower() == "authorization" for name, _value in outgoing.header_items()))
        self.assertEqual(fourth.get_header("Authorization"), "Bearer opaque-resource-key")

    def test_download_explicit_none_does_not_require_a_resource_key(self):
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
            with patch.object(api, "_open_public_request", return_value=Response(b"public")) as opened:
                api.download_video_items([
                    {"url": "https://cdn.example/public.mp4", "filename": "public.mp4", "url_auth": "none"},
                ], temp_dir, 30)
        self.assertIsNone(opened.call_args.args[0].get_header("Authorization"))

    def test_download_rejects_unknown_url_auth_before_transport(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(api, "_open_public_request") as opened:
                with self.assertRaises(api.ApiUsageError):
                    api.download_video_items([
                        {"url": "https://cdn.example/video.mp4", "url_auth": "model_api_key"},
                    ], temp_dir, 30)
        opened.assert_not_called()

    def test_download_rejects_resource_authorized_item_without_key_before_transport(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(api, "_open_public_request") as opened:
                with self.assertRaises(api.ApiUsageError):
                    api.download_video_items([
                        {"url": "https://cdn.example/video.mp4", "url_auth": "resource_api_key"},
                    ], temp_dir, 30)
        opened.assert_not_called()

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
