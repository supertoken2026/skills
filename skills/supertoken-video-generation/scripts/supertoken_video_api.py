"""Small, defensive HTTP and file primitives for SuperToken video workflows."""

import base64
import binascii
import errno
import http.client
import ipaddress
import json
import os
import queue
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from supertoken_video_config import normalize_base_url


MAX_API_BODY_BYTES = 4 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_MEDIA_BYTES = 512 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
MAX_DIAGNOSTIC_DECODE_WORK = 64 * 1024
MAX_CLEANUP_PLAN_ENV_BYTES = 16 * 1024
_CLEANUP_PLAN_ENV_VAR = "SUPERTOKEN_VIDEO_CLEANUP_PLAN"


@dataclass(frozen=True)
class ApiResponse:
    status: int
    headers: dict
    body: bytes


@dataclass(frozen=True)
class _ResolvedAddress:
    family: int
    socket_type: int
    protocol: int
    socket_address: tuple


class ApiUsageError(ValueError):
    """Raised for unsafe input."""


class ApiResponseError(RuntimeError):
    """Raised for remote HTTP or media failures without exposing server data."""


def _deadline_error(message=None):
    error = ApiResponseError(message or "operation deadline exceeded")
    error.deadline_exceeded = True
    return error


def _run_with_deadline(operation, deadline, monotonic, *, cancel=None, message=None):
    """Bound an interruptible operation by the one absolute task deadline."""
    if deadline is None:
        return operation()
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _deadline_error(message)
    result = queue.Queue(maxsize=1)

    def run():
        try:
            result.put((True, operation()))
        except BaseException as exc:
            result.put((False, exc))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        succeeded, value = result.get(timeout=remaining)
    except queue.Empty as exc:
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass
        raise _deadline_error(message) from exc
    if monotonic() >= deadline:
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass
        raise _deadline_error(message)
    if not succeeded:
        raise value
    return value


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PresignedUploadRequest(urllib.request.Request):
    """Prevent urllib from adding a content type absent from signed headers."""

    def has_header(self, header_name):
        if isinstance(header_name, str) and header_name.lower() == "content-type":
            return True
        return super().has_header(header_name)


_OPENER = urllib.request.build_opener(_NoRedirectHandler())
_KEY_PATTERN = re.compile(r"\b(?:sk|ak|wk)[_-][A-Za-z0-9._~-]+", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SENSITIVE_PARAMETER_PATTERN = re.compile(
    r"\b(?:access[_-]?token|api[_-]?key|authorization|credential|key|secret|signature|sig|token|x-amz-[a-z0-9-]+)=[^\s&]+",
    re.IGNORECASE,
)
_SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?:key|token|secret|signature|sig|authorization|credential|url)",
    re.IGNORECASE,
)
_JSON_UNICODE_ESCAPE_PATTERN = re.compile(r"(\\+)(u[0-9A-Fa-f]{4})")


def _decode_json_unicode_escapes(value: str) -> str:
    """Decode one JSON-style unicode escape layer without parsing server text as JSON."""
    def replace(match):
        slashes = match.group(1)
        unicode_escape = match.group(2)
        retained = "\\" * (len(slashes) // 2)
        if len(slashes) % 2:
            return retained + chr(int(unicode_escape[1:], 16))
        return retained + unicode_escape

    return _JSON_UNICODE_ESCAPE_PATTERN.sub(replace, value)


def _decoded_text_variants(value: str):
    """Yield each decoding layer, failing closed when bounded work is exhausted."""
    current = value
    remaining_work = MAX_DIAGNOSTIC_DECODE_WORK
    while True:
        yield current
        # Every successful decode shrinks the text, so this caps total work.
        if len(current) > remaining_work:
            yield None
            return
        remaining_work -= len(current)
        decoded = urllib.parse.unquote(_decode_json_unicode_escapes(current))
        if decoded == current:
            return
        current = decoded


def _contains_reversible_secret(value: str, secrets) -> bool:
    for candidate in _decoded_text_variants(value):
        if candidate is None:
            return True
        if any(secret in candidate for secret in secrets if isinstance(secret, str) and secret):
            return True
        if _KEY_PATTERN.search(candidate) or _SENSITIVE_PARAMETER_PATTERN.search(candidate):
            return True
    return False


def _is_sensitive_field_name(value) -> bool:
    if not isinstance(value, str):
        return False
    for candidate in _decoded_text_variants(value):
        if candidate is None or _SENSITIVE_FIELD_PATTERN.search(candidate):
            return True
    return False


def endpoint_url(base_url: str, route: str) -> str:
    if not isinstance(route, str) or not route.startswith("/v1/"):
        raise ApiUsageError("route must begin with /v1/")
    normalized = normalize_base_url(base_url)
    return f"{normalized}{route[3:]}" if normalized.endswith("/v1") else f"{normalized}{route}"


def header_value(headers, name, default=None):
    if not hasattr(headers, "items"):
        return default
    target = str(name).lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return value
    return default


def _sanitize_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return "[REDACTED URL]"
    if not parsed.scheme or not host:
        return "[REDACTED URL]"
    netloc = host.lower()
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _sanitize_text(value: str, secrets=()) -> str:
    if _contains_reversible_secret(value, secrets):
        return "[REDACTED]"
    text = value
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[REDACTED]")
    text = _URL_PATTERN.sub(lambda match: _sanitize_url(match.group(0)), text)
    text = _KEY_PATTERN.sub("[REDACTED]", text)
    text = _SENSITIVE_PARAMETER_PATTERN.sub("[REDACTED]", text)
    return "".join(char for char in text if char == "\n" or 32 <= ord(char) < 127)


def _sanitize_json(value, secrets=(), field_name=None):
    if _is_sensitive_field_name(field_name):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            _sanitize_text(str(key), secrets): _sanitize_json(item, secrets, str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json(item, secrets) for item in value]
    return _sanitize_text(value, secrets) if isinstance(value, str) else value


def sanitize_diagnostic(value, *secrets) -> str:
    """Return a bounded diagnostic that cannot disclose keys or signed URL data."""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    try:
        decoded = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return _sanitize_text(text, secrets)[:1000]
    return json.dumps(_sanitize_json(decoded, secrets), ensure_ascii=True)[:1000]


def _validate_timeout(timeout):
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ApiUsageError("timeout must be a positive number")


def _validate_header(name, value):
    if not isinstance(name, str) or not isinstance(value, str) or not name:
        raise ApiUsageError("HTTP headers must be non-empty strings")
    if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
        raise ApiUsageError("HTTP headers must not contain line breaks")


_RESERVED_PUBLIC_HOSTS = {"localhost", "local", "localdomain", "broadcasthost"}
_RESERVED_PUBLIC_HOST_SUFFIXES = (".localhost", ".local", ".localdomain")


def _is_reserved_public_hostname(host):
    normalized = host.rstrip(".").lower()
    return (
        not normalized
        or normalized in _RESERVED_PUBLIC_HOSTS
        or normalized.endswith(_RESERVED_PUBLIC_HOST_SUFFIXES)
        or "." not in normalized
    )


def _is_unsafe_address(address):
    return (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def _validate_authenticated_url(url):
    if not isinstance(url, str) or any(ord(char) < 33 or ord(char) > 126 for char in url):
        raise ApiUsageError("authenticated requests require a clean absolute HTTPS URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ApiUsageError("authenticated requests require a clean absolute HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https" or not parsed.netloc or not parsed.hostname
        or parsed.username is not None or parsed.password is not None or parsed.fragment
        or port == 0
    ):
        raise ApiUsageError("authenticated requests require a clean absolute HTTPS URL")


def _cancel_stream(stream):
    try:
        raw = getattr(getattr(stream, "fp", None), "raw", None)
        active_socket = getattr(raw, "_sock", None)
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            active_socket.close()
            return
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


def _bounded_read(
    stream, limit, label, headers=None, *, deadline=None,
    deadline_message=None, monotonic=time.monotonic,
) -> bytes:
    length = header_value(headers or {}, "Content-Length")
    try:
        if length is not None and int(length) > limit:
            raise ApiResponseError(f"{label} exceeds the size limit")
    except ValueError:
        pass

    def read_all():
        chunks = []
        total = 0
        while True:
            if deadline is not None and monotonic() >= deadline:
                raise _deadline_error(deadline_message)
            requested = min(CHUNK_SIZE, limit - total + 1)
            try:
                chunk = stream.read(requested)
            except (OSError, TypeError, ValueError) as exc:
                raise ApiResponseError(f"could not read {label}") from exc
            if deadline is not None and monotonic() >= deadline:
                raise _deadline_error(deadline_message)
            if not isinstance(chunk, (bytes, bytearray)) or len(chunk) > requested:
                raise ApiResponseError(f"could not read {label}")
            total += len(chunk)
            if total > limit:
                raise ApiResponseError(f"{label} exceeds the size limit")
            if not chunk:
                return b"".join(chunks)
            chunks.append(bytes(chunk))

    return _run_with_deadline(
        read_all,
        deadline,
        monotonic,
        cancel=lambda: _cancel_stream(stream),
        message=deadline_message,
    )


def _response_status(response):
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    try:
        return response.getcode()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ApiResponseError("HTTP response had no valid status") from exc


def _open_request(
    request, timeout, *, deadline=None, deadline_message=None, monotonic=None,
) -> ApiResponse:
    """Open one authenticated request; the opener never follows redirects."""
    monotonic = monotonic or time.monotonic
    current = {"stream": None}

    def open_and_read():
        try:
            with _OPENER.open(
                request, timeout=_remaining_timeout(timeout, deadline, monotonic, deadline_message)
            ) as response:
                current["stream"] = response
                status = _response_status(response)
                headers = dict(response.headers or {})
                body = _bounded_read(
                    response,
                    MAX_API_BODY_BYTES,
                    "API response",
                    headers,
                    deadline=deadline,
                    deadline_message=deadline_message,
                    monotonic=monotonic,
                )
        except urllib.error.HTTPError as exc:
            current["stream"] = exc
            try:
                _bounded_read(
                    exc,
                    MAX_ERROR_BODY_BYTES,
                    "API error response",
                    exc.headers or {},
                    deadline=deadline,
                    deadline_message=deadline_message,
                    monotonic=monotonic,
                )
            finally:
                exc.close()
            raise ApiResponseError(f"SuperToken request failed (HTTP {exc.code})") from None
        except ApiResponseError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ApiResponseError("SuperToken request could not be completed") from exc
        if 300 <= status < 400:
            raise ApiResponseError("SuperToken request was redirected and was not followed")
        if not 200 <= status < 300:
            raise ApiResponseError(f"SuperToken request failed (HTTP {status})")
        return ApiResponse(status, headers, body)

    return _run_with_deadline(
        open_and_read,
        deadline,
        monotonic,
        cancel=lambda: _cancel_stream(current["stream"]),
        message=deadline_message,
    )


def request_json(
    method, url, api_key, timeout, payload=None, headers=None, *,
    deadline=None, deadline_message=None, monotonic=None,
) -> ApiResponse:
    """Send one authenticated JSON request without following redirects."""
    _validate_authenticated_url(url)
    _validate_timeout(timeout)
    if not isinstance(method, str) or not method:
        raise ApiUsageError("method must be a non-empty string")
    if not isinstance(api_key, str) or not api_key or any(char.isspace() for char in api_key):
        raise ApiUsageError("API key must be a non-empty token")
    try:
        body = None if payload is None else json.dumps(
            payload, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ApiUsageError("JSON payload is not serializable") from exc
    request = urllib.request.Request(url, data=body, method=method.upper())
    request.add_unredirected_header("Authorization", f"Bearer {api_key}")
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        _validate_header(name, value)
        if name.lower() == "authorization":
            raise ApiUsageError("Authorization is managed by request_json")
        if name.lower() in {"idempotency-key", "proxy-authorization"}:
            request.add_unredirected_header(name, value)
        else:
            request.add_header(name, value)
    if deadline is None:
        return _open_request(request, timeout)
    return _open_request(
        request,
        timeout,
        deadline=deadline,
        deadline_message=deadline_message,
        monotonic=monotonic,
    )


def parse_json_response(response: ApiResponse) -> dict:
    if not isinstance(response, ApiResponse):
        raise ApiUsageError("response must be an ApiResponse")
    try:
        value = json.loads(response.body)
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApiResponseError(
            f"SuperToken returned invalid JSON (HTTP {response.status})"
        ) from exc
    if not isinstance(value, dict):
        raise ApiResponseError("SuperToken returned JSON that was not an object")
    return value


def _validate_public_url(url, label):
    if not isinstance(url, str) or any(ord(char) < 33 or ord(char) > 126 for char in url):
        raise ApiUsageError(f"{label} must be a clean absolute HTTPS URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ApiUsageError(f"{label} must be a clean absolute HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https" or not parsed.netloc or not host
        or parsed.username is not None or parsed.password is not None or parsed.fragment
        or "#" in url or port == 0
    ):
        raise ApiUsageError(f"{label} must be a clean absolute HTTPS URL")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise ApiUsageError(f"{label} must be a clean absolute HTTPS URL") from exc
    address = _numeric_ipv4_address(ascii_host)
    if address is None:
        try:
            address = ipaddress.ip_address(ascii_host)
        except ValueError:
            if _is_reserved_public_hostname(ascii_host):
                raise ApiUsageError(f"{label} must not use a local or reserved hostname")
            return parsed
    if _is_unsafe_address(address):
        raise ApiUsageError(f"{label} must not use a private or unsafe literal address")
    return parsed


def validate_public_url(url, label="URL") -> str:
    """Validate a public HTTPS URL and return it unchanged."""
    _validate_public_url(url, label)
    return url


def _numeric_ipv4_address(host):
    """Parse historical all-numeric IPv4 spellings that URL parsers treat as hostnames."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values = []
    for part in parts:
        if not part:
            return None
        if part.lower().startswith("0x"):
            digits, base = part[2:], 16
        elif len(part) > 1 and part.startswith("0"):
            digits, base = part[1:], 8
        else:
            digits, base = part, 10
        if not digits or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
            return None
        try:
            values.append(int(digits, base))
        except ValueError:
            return None
    if len(values) == 1:
        number = values[0]
    else:
        if values[0] > 255:
            return None
        shifts = {2: 24, 3: 16, 4: 8}
        final_limit = 1 << shifts[len(values)]
        if any(value > 255 for value in values[1:-1]) or values[-1] >= final_limit:
            return None
        number = values[0] << 24
        for index, value in enumerate(values[1:-1], start=1):
            number |= value << (24 - 8 * index)
        number |= values[-1]
    if not 0 <= number <= 0xFFFFFFFF:
        return None
    return ipaddress.IPv4Address(number)


def _read_local_file(path, limit):
    source = Path(path).expanduser()
    if not source.is_file():
        raise ApiUsageError("media file does not exist")
    try:
        with source.open("rb") as stream:
            data = _bounded_read(stream, limit, "media file")
    except OSError as exc:
        raise ApiUsageError("media file could not be read") from exc
    if not data:
        raise ApiUsageError("media file must not be empty")
    return source, data


def _resolved_address(address, port):
    if address.version == 4:
        return _ResolvedAddress(
            socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
            (str(address), port),
        )
    return _ResolvedAddress(
        socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP,
        (str(address), port, 0, 0),
    )


def _resolve_public_addresses(
    host, port, label, *, deadline=None, deadline_message=None,
    monotonic=time.monotonic,
):
    address = _numeric_ipv4_address(host)
    if address is None:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
    if address is not None:
        if _is_unsafe_address(address):
            raise ApiUsageError(f"{label} must not use a private or unsafe literal address")
        return (_resolved_address(address, port),)
    try:
        resolved = _run_with_deadline(
            lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            deadline,
            monotonic,
            message=deadline_message,
        )
    except ApiResponseError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ApiResponseError(f"{label} hostname could not be resolved") from exc
    addresses = []
    for _family, _socket_type, _protocol, _canonical_name, socket_address in resolved:
        try:
            resolved_host = socket_address[0].split("%", 1)[0]
            address = ipaddress.ip_address(resolved_host)
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise ApiResponseError(f"{label} hostname could not be resolved") from exc
        if _is_unsafe_address(address):
            raise ApiUsageError(f"{label} must not resolve to a private or unsafe address")
        addresses.append(_resolved_address(address, port))
    if not addresses:
        raise ApiResponseError(f"{label} hostname could not be resolved")
    return tuple(dict.fromkeys(addresses))


def _prepare_public_transport(
    url, label, *, deadline=None, deadline_message=None,
    monotonic=time.monotonic,
):
    parsed = _validate_public_url(url, label)
    try:
        host = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port or 443
    except (AttributeError, UnicodeError, ValueError) as exc:
        raise ApiUsageError(f"{label} must be a clean absolute HTTPS URL") from exc
    addresses = _resolve_public_addresses(
        host,
        port,
        label,
        deadline=deadline,
        deadline_message=deadline_message,
        monotonic=monotonic,
    )
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return host, port, addresses, target


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that never resolves the requested hostname again."""

    def __init__(self, host, port, address, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self._resolved_address = address

    def connect(self):
        address = self._resolved_address
        raw_socket = socket.socket(address.family, address.socket_type, address.protocol)
        try:
            raw_socket.settimeout(self.timeout)
            if self.source_address:
                raw_socket.bind(self.source_address)
            raw_socket.connect(address.socket_address)
            try:
                raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise


class _PublicResponseContext:
    def __init__(self, connection, response):
        self.connection = connection
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self):
        try:
            self.response.close()
        finally:
            self.connection.close()


def _send_pinned_request(connection, request, request_target):
    headers = list(request.header_items())
    header_names = {name.lower() for name, _value in headers}
    transfer_encodings = [
        value for name, value in headers if name.lower() == "transfer-encoding"
    ]
    encode_chunked = any(
        token.strip().lower() == "chunked"
        for value in transfer_encodings
        for token in value.split(",")
    )
    body = request.data
    if body is not None and not isinstance(body, bytes):
        raise ApiUsageError("public request body must be bytes")
    connection.putrequest(
        request.get_method(),
        request_target,
        skip_host="host" in header_names,
        skip_accept_encoding=True,
    )
    if body is not None and "content-length" not in header_names and not transfer_encodings:
        connection.putheader("Content-Length", str(len(body)))
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders(body, encode_chunked=encode_chunked)


def _open_pinned_public_request(
    *, request, host, port, addresses, request_target, timeout,
    deadline=None, deadline_message=None, monotonic=time.monotonic,
):
    current = {"connection": None}
    cancelled = threading.Event()

    def cancel():
        cancelled.set()
        connection = current["connection"]
        if connection is not None:
            connection.close()

    def open_approved_address():
        last_error = None
        for address in addresses:
            if cancelled.is_set():
                raise _deadline_error(deadline_message)
            connection = _PinnedHTTPSConnection(
                host,
                port,
                address,
                _remaining_timeout(timeout, deadline, monotonic, deadline_message),
            )
            current["connection"] = connection
            try:
                _send_pinned_request(connection, request, request_target)
                response = connection.getresponse()
            except (OSError, UnicodeError, http.client.HTTPException) as exc:
                connection.close()
                last_error = exc
                continue
            return _PublicResponseContext(connection, response)
        if last_error is not None:
            raise last_error
        raise OSError("no approved public address")

    return _run_with_deadline(
        open_approved_address,
        deadline,
        monotonic,
        cancel=cancel,
        message=deadline_message,
    )


def _open_public_request(
    request, timeout, *, deadline=None, deadline_message=None, monotonic=None,
):
    monotonic = monotonic or time.monotonic
    try:
        host, port, addresses, request_target = _prepare_public_transport(
            request.full_url,
            "public request URL",
            deadline=deadline,
            deadline_message=deadline_message,
            monotonic=monotonic,
        )
        response_context = _open_pinned_public_request(
            request=request,
            host=host,
            port=port,
            addresses=addresses,
            request_target=request_target,
            timeout=timeout,
            deadline=deadline,
            deadline_message=deadline_message,
            monotonic=monotonic,
        )
    except (ApiUsageError, ApiResponseError):
        raise
    except (OSError, ValueError, UnicodeError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise ApiResponseError("media request could not be completed") from exc
    response = getattr(response_context, "response", response_context)
    status = _response_status(response)
    if 300 <= status < 400:
        _cancel_stream(response_context)
        raise ApiResponseError("media request was redirected and was not followed")
    if not 200 <= status < 300:
        _cancel_stream(response_context)
        raise ApiResponseError(f"media request failed (HTTP {status})")
    return response_context


def upload_media_files(upload_url, paths, timeout, headers=None, *, method="PUT", max_file_bytes=MAX_MEDIA_BYTES) -> list[dict]:
    """Upload local media to a temporary HTTPS URL and return upload metadata."""
    _validate_public_url(upload_url, "upload_url")
    _validate_timeout(timeout)
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ApiUsageError("at least one media file is required")
    if not isinstance(max_file_bytes, int) or max_file_bytes <= 0:
        raise ApiUsageError("max_file_bytes must be positive")
    if not isinstance(method, str) or not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Z]+", method):
        raise ApiUsageError("upload method must be an uppercase HTTP token")
    results = []
    for raw_path in paths:
        source, data = _read_local_file(raw_path, max_file_bytes)
        request = _PresignedUploadRequest(upload_url, data=data, method=method)
        for name, value in (headers or {}).items():
            _validate_header(name, value)
            request.add_header(name, value)
        with _open_public_request(request, timeout) as response:
            _bounded_read(response, MAX_ERROR_BODY_BYTES, "upload response", response.headers)
        results.append({"path": str(source.resolve()), "bytes_written": len(data), "upload_url": _sanitize_url(upload_url)})
    return results


def _safe_output_name(value, index):
    name = Path(value).name if isinstance(value, str) else ""
    if not name or name in {".", ".."}:
        name = f"video-{index + 1}.mp4"
    if not name.lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
        name += ".mp4"
    return name


def _unique_output_path(destination_root, name, used_names):
    candidate = Path(name)
    ordinal = 2
    while candidate.name.casefold() in used_names:
        candidate = Path(f"{Path(name).stem}-{ordinal}{Path(name).suffix}")
        ordinal += 1
    used_names.add(candidate.name.casefold())
    return destination_root / candidate.name


def _remaining_timeout(timeout, deadline, monotonic, deadline_message=None):
    if deadline is None:
        return timeout
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _deadline_error(deadline_message)
    return min(timeout, remaining)


def _path_identity(path):
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return None
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


def _cleanup_path_token(path):
    path = Path(path)
    if not path.is_absolute():
        raise ApiUsageError("cleanup paths must be absolute local paths")
    return base64.b64encode(os.fsencode(str(path))).decode("ascii")


def _cleanup_unlink_action(path, identity=None):
    identity = _path_identity(path) if identity is None else identity
    if identity is None:
        return None
    return {
        "op": "unlink",
        "path": _cleanup_path_token(path),
        "identity": list(identity),
    }


def _cleanup_restore_action(backup, destination, expected_destination=None):
    backup_identity = _path_identity(backup)
    if backup_identity is None:
        return None
    if expected_destination is None:
        expected_destination = _path_identity(destination)
    return {
        "op": "restore",
        "backup": _cleanup_path_token(backup),
        "backup_identity": list(backup_identity),
        "destination": _cleanup_path_token(destination),
        "expected_destination": (
            None if expected_destination is None else list(expected_destination)
        ),
    }


def _cleanup_lock_path(destination):
    destination = Path(destination)
    return destination.parent / f".{destination.name}.supertoken-video-cleanup.lock"


_CLEANUP_IDENTITY_UPPER_BOUND = [
    (1 << 64) - 1,
    (1 << 64) - 1,
    stat.S_IFMT(0o170000),
]


def _cleanup_component_name_limit(directory):
    try:
        value = os.pathconf(directory, "PC_NAME_MAX")
    except (AttributeError, OSError, ValueError):
        return 255
    return max(255, value) if isinstance(value, int) and value > 0 else 255


def _cleanup_placeholder_path_token(directory, component_bytes):
    parent = os.fsencode(str(Path(directory))).rstrip(b"/\\")
    separator = os.fsencode(os.sep)
    raw_path = (parent + separator if parent else separator) + b"x" * component_bytes
    return base64.b64encode(raw_path).decode("ascii")


def _preflight_cleanup_plan_capacity(destinations):
    """Reject result sets whose worst-case local rollback cannot fit in one plan."""
    actions = []
    finalizers = []
    for destination in destinations:
        destination = Path(destination)
        component_bytes = _cleanup_component_name_limit(destination.parent)
        staged_token = _cleanup_placeholder_path_token(
            destination.parent, component_bytes,
        )
        backup_token = _cleanup_placeholder_path_token(
            destination.parent, component_bytes,
        )
        identity = list(_CLEANUP_IDENTITY_UPPER_BOUND)
        actions.append({
            "op": "unlink",
            "path": staged_token,
            "identity": identity,
        })
        actions.append({
            "op": "restore",
            "backup": backup_token,
            "backup_identity": identity,
            "destination": _cleanup_path_token(destination),
            "expected_destination": identity,
        })
        finalizers.append({
            "op": "unlink",
            "path": _cleanup_path_token(_cleanup_lock_path(destination)),
            "identity": identity,
        })
        # This upper bound covers every cleanup state before any lock or part exists.
        _encode_cleanup_plan(actions, finalizers, 0)


def _acquire_cleanup_locks(destinations):
    locks = []
    try:
        for destination in sorted(set(destinations), key=lambda value: os.fsencode(str(value))):
            lock = _cleanup_lock_path(destination)
            try:
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise ApiResponseError("video output cleanup is still in progress") from exc
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"supertoken video cleanup journal\n")
            locks.append(lock)
    except Exception:
        for lock in locks:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return locks


def _backup_destination(destination):
    for _attempt in range(100):
        backup = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.backup"
        try:
            if destination.is_symlink():
                os.symlink(
                    os.readlink(destination),
                    backup,
                    target_is_directory=destination.is_dir(),
                )
            else:
                os.link(destination, backup)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ApiResponseError("could not create a safe output backup") from exc
        return backup
    raise ApiResponseError("could not allocate a safe output backup")


# This isolated helper receives only an encoded cleanup plan in its environment.
_DETACHED_CLEANUP_EXECUTOR = r'''
import base64
import json
import math
import os
import stat
import sys
import time


PLAN_ENV = "SUPERTOKEN_VIDEO_CLEANUP_PLAN"
MAX_PLAN_BYTES = 16 * 1024


def identity_item(item):
    return [item.st_dev, item.st_ino, stat.S_IFMT(item.st_mode)]


def identity(path):
    try:
        return identity_item(os.lstat(path))
    except FileNotFoundError:
        return None


def decode_path(value):
    if not isinstance(value, str):
        raise ValueError("cleanup path token was invalid")
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    path = os.fsdecode(raw)
    if "\x00" in path or not os.path.isabs(path):
        raise ValueError("cleanup path must be absolute")
    return path


def parse_identity(value):
    if (
        not isinstance(value, list)
        or len(value) != 3
        or any(type(part) is not int for part in value)
    ):
        raise ValueError("cleanup identity was invalid")
    return value


def validate_action(action):
    if not isinstance(action, dict):
        raise ValueError("cleanup action was invalid")
    operation = action.get("op")
    if operation == "unlink":
        if set(action) != {"op", "path", "identity"}:
            raise ValueError("cleanup action was invalid")
        decode_path(action["path"])
        parse_identity(action["identity"])
    elif operation == "restore":
        if set(action) != {
            "op", "backup", "backup_identity", "destination", "expected_destination",
        }:
            raise ValueError("cleanup action was invalid")
        decode_path(action["backup"])
        decode_path(action["destination"])
        parse_identity(action["backup_identity"])
        expected = action["expected_destination"]
        if expected is not None:
            parse_identity(expected)
    else:
        raise ValueError("cleanup action was invalid")
    return action


def validate_actions(actions):
    if not isinstance(actions, list):
        raise ValueError("cleanup actions were invalid")
    return [validate_action(action) for action in actions]


def load_plan():
    encoded = os.environ.pop(PLAN_ENV, None)
    if not isinstance(encoded, str):
        raise ValueError("cleanup plan was missing")
    try:
        encoded_bytes = encoded.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("cleanup plan was invalid") from exc
    if not encoded_bytes or len(encoded_bytes) > MAX_PLAN_BYTES:
        raise ValueError("cleanup plan was invalid")
    plan = json.loads(base64.b64decode(encoded_bytes, validate=True).decode("utf-8"))
    if not isinstance(plan, dict) or set(plan) != {"actions", "finalizers", "not_before"}:
        raise ValueError("cleanup plan was invalid")
    not_before = plan["not_before"]
    if (
        type(not_before) not in {int, float}
        or not math.isfinite(float(not_before))
    ):
        raise ValueError("cleanup plan was invalid")
    return validate_actions(plan["actions"]), validate_actions(plan["finalizers"]), not_before


def matches(path, expected):
    return isinstance(expected, list) and len(expected) == 3 and identity(path) == expected


def unlink(action):
    path = decode_path(action["path"])
    if identity(path) is None or not matches(path, action["identity"]):
        return True
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def restore(action):
    backup = decode_path(action["backup"])
    destination = decode_path(action["destination"])
    current_backup = identity(backup)
    if current_backup is None:
        return True
    if current_backup != action["backup_identity"]:
        return False
    current_destination = identity(destination)
    expected_destination = action["expected_destination"]
    if expected_destination is None:
        if current_destination is not None:
            return False
    elif current_destination is not None and current_destination != expected_destination:
        return False
    try:
        os.replace(backup, destination)
    except OSError:
        return False
    return True


def execute(action):
    if action.get("op") == "unlink":
        return unlink(action)
    if action.get("op") == "restore":
        return restore(action)
    return False


def finish(actions):
    pending = list(actions)
    for _attempt in range(20):
        remaining = [action for action in pending if not execute(action)]
        if not remaining:
            return True
        pending = remaining
        time.sleep(0.05)
    return False


try:
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            pass
    actions, finalizers, not_before = load_plan()
    while time.time() < not_before:
        time.sleep(min(0.05, not_before - time.time()))
    if finish(actions) and finish(finalizers):
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
'''
_DETACHED_CLEANUP_PIDS = set()
_NON_POSIX_CLEANUP_PIDS = set()
_NON_POSIX_CLEANUP_PIDS_LOCK = threading.Lock()


def _validate_cleanup_identity(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 3
        or any(type(part) is not int for part in value)
    ):
        raise ApiResponseError("video output cleanup plan was invalid")


def _validate_cleanup_path_token(value):
    if not isinstance(value, str):
        raise ApiResponseError("video output cleanup plan was invalid")
    try:
        path = os.fsdecode(base64.b64decode(value.encode("ascii"), validate=True))
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise ApiResponseError("video output cleanup plan was invalid") from exc
    if "\x00" in path or not os.path.isabs(path):
        raise ApiResponseError("video output cleanup plan was invalid")


def _validate_cleanup_action(action):
    if not isinstance(action, dict):
        raise ApiResponseError("video output cleanup plan was invalid")
    operation = action.get("op")
    if operation == "unlink":
        if set(action) != {"op", "path", "identity"}:
            raise ApiResponseError("video output cleanup plan was invalid")
        _validate_cleanup_path_token(action["path"])
        _validate_cleanup_identity(action["identity"])
    elif operation == "restore":
        if set(action) != {
            "op", "backup", "backup_identity", "destination", "expected_destination",
        }:
            raise ApiResponseError("video output cleanup plan was invalid")
        _validate_cleanup_path_token(action["backup"])
        _validate_cleanup_path_token(action["destination"])
        _validate_cleanup_identity(action["backup_identity"])
        expected_destination = action["expected_destination"]
        if expected_destination is not None:
            _validate_cleanup_identity(expected_destination)
    else:
        raise ApiResponseError("video output cleanup plan was invalid")


def _validate_cleanup_actions(actions):
    if not isinstance(actions, (list, tuple)):
        raise ApiResponseError("video output cleanup plan was invalid")
    for action in actions:
        _validate_cleanup_action(action)


def _encode_cleanup_plan(actions, finalizers, not_before):
    _validate_cleanup_actions(actions)
    _validate_cleanup_actions(finalizers)
    if type(not_before) not in {int, float}:
        raise ApiResponseError("video output cleanup plan was invalid")
    try:
        if not float("-inf") < float(not_before) < float("inf"):
            raise ValueError("cleanup delay was not finite")
    except (OverflowError, ValueError) as exc:
        raise ApiResponseError("video output cleanup plan was invalid") from exc
    plan = {
        "actions": list(actions),
        "finalizers": list(finalizers),
        "not_before": not_before,
    }
    try:
        payload = base64.b64encode(
            json.dumps(
                plan, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
            ).encode("utf-8")
        ).decode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ApiResponseError("video output cleanup plan was invalid") from exc
    if len(payload.encode("ascii")) > MAX_CLEANUP_PLAN_ENV_BYTES:
        raise ApiResponseError("video output cleanup plan is too large")
    return payload


def _cleanup_child_command():
    return [
        sys.executable,
        "-I",
        "-S",
        "-c",
        _DETACHED_CLEANUP_EXECUTOR,
    ]


def _cleanup_child_environment(payload):
    if not isinstance(payload, str):
        raise ApiResponseError("video output cleanup plan was invalid")
    environment = {}
    for name, value in os.environ.items():
        if name.upper() in {"SYSTEMROOT", "WINDIR"}:
            environment[name] = value
    environment[_CLEANUP_PLAN_ENV_VAR] = payload
    return environment


def _open_file_descriptors():
    descriptors = set()
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            try:
                descriptor = int(name)
                os.fstat(descriptor)
            except (TypeError, ValueError, OSError):
                continue
            descriptors.add(descriptor)
        break
    return descriptors


def _cleanup_child_file_actions():
    actions = [
        (os.POSIX_SPAWN_CLOSE, descriptor)
        for descriptor in sorted(_open_file_descriptors())
        if descriptor > 2
    ]
    actions.extend(
        (
            (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0o600),
            (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0o600),
            (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0o600),
        )
    )
    return actions


def _spawn_posix_cleanup_process(command, environment):
    spawn = getattr(os, "posix_spawn", None)
    if not callable(spawn):
        raise ApiResponseError("video output cleanup could not be started safely")
    actions = _cleanup_child_file_actions()
    try:
        return spawn(
            command[0], command, environment, file_actions=actions, setsid=True,
        )
    except TypeError:
        # Python 3.10 can lack the setsid keyword; the helper calls setsid itself.
        pass
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            raise ApiResponseError("video output cleanup could not be started safely") from exc
    try:
        return spawn(command[0], command, environment, file_actions=actions)
    except OSError as exc:
        raise ApiResponseError("video output cleanup could not be started safely") from exc


def _spawn_cleanup_process_without_posix_spawn(command, environment):
    mode = getattr(os, "P_NOWAIT", None)
    waitable = mode is not None
    if mode is None:
        mode = getattr(os, "P_DETACH", None)
    if mode is None:
        raise ApiResponseError("video output cleanup could not be started safely")
    try:
        return os.spawnve(mode, command[0], command, environment), waitable
    except OSError as exc:
        raise ApiResponseError("video output cleanup could not be started safely") from exc


def _reap_detached_cleanup_processes():
    for process_id in list(_DETACHED_CLEANUP_PIDS):
        try:
            completed, _status = os.waitpid(process_id, os.WNOHANG)
        except (ChildProcessError, OSError):
            completed = process_id
        if completed == process_id:
            _DETACHED_CLEANUP_PIDS.discard(process_id)


def _remember_detached_cleanup_pid(process_id):
    _reap_detached_cleanup_processes()
    _DETACHED_CLEANUP_PIDS.add(process_id)

    def reap():
        try:
            os.waitpid(process_id, 0)
        except (ChildProcessError, OSError):
            pass
        finally:
            _DETACHED_CLEANUP_PIDS.discard(process_id)

    # This thread only reaps the detached process; the helper owns cleanup.
    threading.Thread(target=reap, daemon=True).start()


def _wait_for_detached_cleanup_pid(
    process_id, *, deadline=None, deadline_message=None, monotonic=time.monotonic,
):
    while True:
        try:
            completed, status = os.waitpid(process_id, os.WNOHANG)
        except InterruptedError:
            continue
        except ChildProcessError:
            return 0
        if completed == process_id:
            try:
                return os.waitstatus_to_exitcode(status)
            except AttributeError:
                return status
        if deadline is None:
            try:
                _completed, status = os.waitpid(process_id, 0)
            except InterruptedError:
                continue
            try:
                return os.waitstatus_to_exitcode(status)
            except AttributeError:
                return status
        remaining = deadline - monotonic()
        if remaining <= 0:
            _remember_detached_cleanup_pid(process_id)
            raise _deadline_error(deadline_message)
        time.sleep(min(0.01, remaining))


def _wait_for_non_posix_cleanup_pid(
    process_id, *, deadline=None, deadline_message=None, monotonic=time.monotonic,
):
    def wait_for_exit():
        try:
            _completed, status = os.waitpid(process_id, 0)
        except (ChildProcessError, OSError) as exc:
            raise ApiResponseError("video output cleanup could not be completed safely") from exc
        try:
            return os.waitstatus_to_exitcode(status)
        except AttributeError:
            return status

    if deadline is None:
        return wait_for_exit()

    result = queue.Queue(maxsize=1)

    def reap():
        try:
            result.put((True, wait_for_exit()))
        except BaseException as exc:
            result.put((False, exc))
        finally:
            with _NON_POSIX_CLEANUP_PIDS_LOCK:
                _NON_POSIX_CLEANUP_PIDS.discard(process_id)

    # Windows ignores WNOHANG, so this worker owns the one blocking wait/reap.
    with _NON_POSIX_CLEANUP_PIDS_LOCK:
        _NON_POSIX_CLEANUP_PIDS.add(process_id)
    try:
        threading.Thread(target=reap, daemon=True).start()
    except RuntimeError as exc:
        with _NON_POSIX_CLEANUP_PIDS_LOCK:
            _NON_POSIX_CLEANUP_PIDS.discard(process_id)
        raise ApiResponseError("video output cleanup could not be completed safely") from exc

    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _deadline_error(deadline_message)
    try:
        succeeded, value = result.get(timeout=remaining)
    except queue.Empty as exc:
        raise _deadline_error(deadline_message) from exc
    if monotonic() >= deadline:
        raise _deadline_error(deadline_message)
    if not succeeded:
        raise value
    return value


def _execute_cleanup_plan(
    actions, *, finalizers=(), deadline=None, deadline_message=None,
    monotonic=time.monotonic, not_before=None,
):
    if os.name == "posix":
        _reap_detached_cleanup_processes()
    actions = [action for action in actions if action is not None]
    finalizers = [action for action in finalizers if action is not None]
    if not actions and not finalizers:
        return
    payload = _encode_cleanup_plan(
        actions, finalizers, 0 if not_before is None else not_before,
    )
    command = _cleanup_child_command()
    environment = _cleanup_child_environment(payload)
    if os.name == "posix":
        process_id = _spawn_posix_cleanup_process(command, environment)
        status = _wait_for_detached_cleanup_pid(
            process_id,
            deadline=deadline,
            deadline_message=deadline_message,
            monotonic=monotonic,
        )
        if deadline is not None and monotonic() >= deadline:
            raise _deadline_error(deadline_message)
        if status != 0:
            raise ApiResponseError("video output cleanup could not be completed safely")
        return
    process_id, waitable = _spawn_cleanup_process_without_posix_spawn(
        command, environment,
    )
    if not waitable:
        if deadline is not None and monotonic() >= deadline:
            raise _deadline_error(deadline_message)
        raise ApiResponseError("video output cleanup could not be monitored safely")
    status = _wait_for_non_posix_cleanup_pid(
        process_id,
        deadline=deadline,
        deadline_message=deadline_message,
        monotonic=monotonic,
    )
    if deadline is not None and monotonic() >= deadline:
        raise _deadline_error(deadline_message)
    if status != 0:
        raise ApiResponseError("video output cleanup could not be completed safely")


def _rollback_cleanup_actions(staged, backups, promoted):
    actions = [
        _cleanup_unlink_action(part)
        for part, _destination, _size, _url in staged
    ]
    for destination, backup in reversed(backups):
        expected_destination = promoted.get(destination)
        if backup is not None:
            action = (
                _cleanup_restore_action(backup, destination, expected_destination)
                if expected_destination is not None
                else _cleanup_unlink_action(backup)
            )
        elif expected_destination is not None:
            action = _cleanup_unlink_action(destination, expected_destination)
        else:
            action = None
        actions.append(action)
    return actions


def _copy_download_to_output(
    response, output, *, deadline=None, deadline_message=None,
    monotonic=time.monotonic,
):
    def cancel():
        _cancel_stream(response)
        _cancel_stream(output)

    def copy():
        total = 0
        while True:
            _remaining_timeout(1, deadline, monotonic, deadline_message)
            chunk = response.read(min(CHUNK_SIZE, MAX_MEDIA_BYTES - total + 1))
            _remaining_timeout(1, deadline, monotonic, deadline_message)
            if not isinstance(chunk, (bytes, bytearray)):
                raise ApiResponseError("could not read video download")
            total += len(chunk)
            if total > MAX_MEDIA_BYTES:
                raise ApiResponseError("video download exceeds the size limit")
            if not chunk:
                break
            written = output.write(chunk)
            if written is not None and written != len(chunk):
                raise ApiResponseError("could not write video download")
            _remaining_timeout(1, deadline, monotonic, deadline_message)
        output.flush()
        _remaining_timeout(1, deadline, monotonic, deadline_message)
        os.fsync(output.fileno())
        _remaining_timeout(1, deadline, monotonic, deadline_message)
        return total

    return _run_with_deadline(
        copy,
        deadline,
        monotonic,
        cancel=cancel,
        message=deadline_message,
    )


def _stage_download(
    url, destination, timeout, resource_key=None, *, deadline=None,
    deadline_message=None, monotonic=time.monotonic,
):
    _validate_public_url(url, "result URL")
    request = urllib.request.Request(url, method="GET")
    if resource_key is not None:
        request.add_unredirected_header("Authorization", f"Bearer {resource_key}")
    descriptor, raw_part = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part", dir=destination.parent)
    part = Path(raw_part)
    try:
        open_options = {}
        if deadline is not None:
            open_options = {
                "deadline": deadline,
                "deadline_message": deadline_message,
                "monotonic": monotonic,
            }
        with os.fdopen(descriptor, "wb") as output, _open_public_request(
            request,
            _remaining_timeout(timeout, deadline, monotonic, deadline_message),
            **open_options,
        ) as response:
            length = header_value(response.headers, "Content-Length")
            try:
                if length is not None and int(length) > MAX_MEDIA_BYTES:
                    raise ApiResponseError("video download exceeds the size limit")
            except ValueError:
                pass
            total = _copy_download_to_output(
                response,
                output,
                deadline=deadline,
                deadline_message=deadline_message,
                monotonic=monotonic,
            )
        if total == 0:
            raise ApiResponseError("video download was empty")
        return part, total
    except Exception as exc:
        try:
            _execute_cleanup_plan(
                [_cleanup_unlink_action(part)],
                deadline=deadline,
                deadline_message=deadline_message,
                monotonic=monotonic,
            )
        except ApiResponseError as cleanup_error:
            raise cleanup_error from exc
        raise


def download_video_items(
    items, output_dir, timeout, resource_key=None, *, output_path=None,
    deadline=None, deadline_message=None, monotonic=time.monotonic,
) -> list[dict]:
    """Download task result videos, then atomically promote their staged files."""
    _validate_timeout(timeout)
    if not isinstance(items, (list, tuple)) or not items:
        raise ApiUsageError("at least one video result is required")
    if resource_key is not None and (not isinstance(resource_key, str) or not resource_key):
        raise ApiUsageError("resource_key must be a non-empty token")
    destination_root = Path(output_dir).expanduser()
    fixed_destination = None
    if output_path is not None:
        if len(items) != 1:
            raise ApiUsageError("output_path requires exactly one video result")
        fixed_destination = Path(output_path).expanduser()
        destination_root = fixed_destination.parent
    destination_root.mkdir(parents=True, exist_ok=True)
    if not destination_root.is_dir():
        raise ApiUsageError("output_dir must be a directory")
    destination_root = destination_root.resolve()
    if fixed_destination is not None:
        fixed_destination = fixed_destination.resolve()
    staged = []
    promoted = {}
    backups = []
    used_names = set()
    deadline_message = deadline_message or "operation deadline exceeded"
    prepared = []
    for index, item in enumerate(items):
        _remaining_timeout(timeout, deadline, monotonic, deadline_message)
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ApiUsageError("each video result must contain a URL")
        url_auth = item.get("url_auth")
        if url_auth not in (None, "none", "resource_api_key"):
            raise ApiUsageError("video result has an unsupported url_auth value")
        key = resource_key if url_auth == "resource_api_key" else None
        if url_auth == "resource_api_key" and key is None:
            raise ApiUsageError("resource_key is required for this video result")
        _validate_public_url(item["url"], "result URL")
        destination = fixed_destination or _unique_output_path(
            destination_root,
            _safe_output_name(item.get("filename") or item.get("name"), index),
            used_names,
        )
        prepared.append((item, key, destination))
    _remaining_timeout(timeout, deadline, monotonic, deadline_message)
    _preflight_cleanup_plan_capacity(
        [destination for _item, _key, destination in prepared]
    )
    _remaining_timeout(timeout, deadline, monotonic, deadline_message)
    locks = _acquire_cleanup_locks([destination for _item, _key, destination in prepared])
    try:
        for item, key, destination in prepared:
            _remaining_timeout(timeout, deadline, monotonic, deadline_message)
            part, size = _stage_download(
                item["url"], destination, timeout, key,
                deadline=deadline, deadline_message=deadline_message, monotonic=monotonic,
            )
            staged.append((part, destination, size, item["url"]))
        for part, destination, _size, _url in staged:
            _remaining_timeout(timeout, deadline, monotonic, deadline_message)
            backup = None
            if os.path.lexists(destination):
                backup = _backup_destination(destination)
            backups.append((destination, backup))
            _remaining_timeout(timeout, deadline, monotonic, deadline_message)
            os.replace(part, destination)
            promoted_identity = _path_identity(destination)
            if promoted_identity is None:
                raise ApiResponseError("could not promote staged video output")
            promoted[destination] = promoted_identity
            _remaining_timeout(timeout, deadline, monotonic, deadline_message)
    except Exception as exc:
        try:
            _execute_cleanup_plan(
                _rollback_cleanup_actions(
                    staged,
                    backups,
                    promoted,
                ),
                finalizers=[_cleanup_unlink_action(lock) for lock in locks],
                deadline=deadline,
                deadline_message=deadline_message,
                monotonic=monotonic,
            )
        except ApiResponseError as cleanup_error:
            raise cleanup_error from exc
        raise
    _execute_cleanup_plan(
        [_cleanup_unlink_action(backup) for _destination, backup in backups if backup is not None],
        finalizers=[_cleanup_unlink_action(lock) for lock in locks],
        deadline=deadline,
        deadline_message=deadline_message,
        monotonic=monotonic,
    )
    return [
        {"path": str(destination.resolve()), "bytes_written": size, "url": _sanitize_url(url)}
        for _part, destination, size, url in staged
    ]
