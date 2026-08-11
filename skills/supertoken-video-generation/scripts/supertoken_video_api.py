"""Small standard-library HTTP and media helpers for SuperToken video tasks."""

import http.client
import ipaddress
import json
import math
import os
import queue
import re
import socket
import tempfile
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from supertoken_video_config import normalize_base_url


MAX_API_BODY_BYTES = 4 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_MEDIA_BYTES = 512 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
MAX_DIAGNOSTIC_DECODE_WORK = 64 * 1024
# Leonardo's public CDN rejects Python urllib's default User-Agent.
VIDEO_DOWNLOAD_USER_AGENT = "Mozilla/5.0"


@dataclass(frozen=True)
class ApiResponse:
    status: int
    headers: dict
    body: bytes


class ApiUsageError(ValueError):
    """Raised for invalid local input before an HTTP request is made."""


class ApiResponseError(RuntimeError):
    """Raised for an HTTP or media failure without echoing remote data."""


def _deadline_error(message=None):
    error = ApiResponseError(message or "operation deadline exceeded")
    error.deadline_exceeded = True
    return error


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PresignedUploadRequest(urllib.request.Request):
    """Do not add a Content-Type header that a presigned request did not sign."""

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
_LOCAL_HOSTS = {"localhost", "local", "localdomain", "broadcasthost"}
_LOCAL_SUFFIXES = (".localhost", ".local", ".localdomain")


def _decode_json_unicode_escapes(value: str) -> str:
    def replace(match):
        slashes = match.group(1)
        escaped = match.group(2)
        retained = "\\" * (len(slashes) // 2)
        return retained + (chr(int(escaped[1:], 16)) if len(slashes) % 2 else escaped)

    return _JSON_UNICODE_ESCAPE_PATTERN.sub(replace, value)


def _decoded_text_variants(value: str):
    """Yield bounded percent and JSON-escape decoding layers for redaction."""
    current = value
    remaining = MAX_DIAGNOSTIC_DECODE_WORK
    while True:
        yield current
        if len(current) > remaining:
            yield None
            return
        remaining -= len(current)
        decoded = urllib.parse.unquote(_decode_json_unicode_escapes(current))
        if decoded == current:
            return
        current = decoded


def _contains_reversible_secret(value: str, secrets=()) -> bool:
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
    return any(
        candidate is None or _SENSITIVE_FIELD_PATTERN.search(candidate)
        for candidate in _decoded_text_variants(value)
    )


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
    path = "/".join(
        "[REDACTED]" if _contains_reversible_secret(segment) else segment
        for segment in parsed.path.split("/")
    )
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _sanitize_text(value: str, secrets=()) -> str:
    if _contains_reversible_secret(value, secrets):
        return "[REDACTED]"
    for secret in secrets:
        if isinstance(secret, str) and secret:
            value = value.replace(secret, "[REDACTED]")
    value = _URL_PATTERN.sub(lambda match: _sanitize_url(match.group(0)), value)
    value = _KEY_PATTERN.sub("[REDACTED]", value)
    value = _SENSITIVE_PARAMETER_PATTERN.sub("[REDACTED]", value)
    return "".join(char for char in value if char == "\n" or 32 <= ord(char) < 127)


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
    """Return a short diagnostic without credentials or signed URL components."""
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


def endpoint_url(base_url: str, route: str) -> str:
    if not isinstance(route, str) or not route.startswith("/v1/"):
        raise ApiUsageError("route must begin with /v1/")
    normalized = normalize_base_url(base_url)
    return f"{normalized}{route[3:]}" if normalized.endswith("/v1") else f"{normalized}{route}"


def header_value(headers, name, default=None):
    if not hasattr(headers, "items"):
        return default
    wanted = str(name).lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return value
    return default


def _validate_timeout(timeout):
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ApiUsageError("timeout must be a positive number")


def _validate_header(name, value):
    if not isinstance(name, str) or not name or not isinstance(value, str):
        raise ApiUsageError("HTTP headers must be non-empty strings")
    if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
        raise ApiUsageError("HTTP headers must not contain line breaks")


def _validate_https_url(url, label):
    if not isinstance(url, str) or any(ord(char) < 33 or ord(char) > 126 for char in url):
        raise ApiUsageError(f"{label} must be a clean absolute HTTPS URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ApiUsageError(f"{label} must be a clean absolute HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port == 0
    ):
        raise ApiUsageError(f"{label} must be a clean absolute HTTPS URL")
    return parsed


def validate_public_url(url, label="URL") -> str:
    """Validate a public HTTPS URL used by an upload or result transfer."""
    parsed = _validate_https_url(url, label)
    try:
        host = parsed.hostname.encode("idna").decode("ascii").rstrip(".").lower()
    except (AttributeError, UnicodeError, ValueError) as exc:
        raise ApiUsageError(f"{label} must be a clean absolute HTTPS URL") from exc
    if (
        not host
        or host in _LOCAL_HOSTS
        or host.endswith(_LOCAL_SUFFIXES)
        or "." not in host
    ):
        raise ApiUsageError(f"{label} must not use a local or reserved hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            # inet_aton also recognizes legacy numeric IPv4 forms such as 127.1.
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return url
    if not address.is_global:
        raise ApiUsageError(f"{label} must not use a private or unsafe literal address")
    return url


def _set_read_timeout(stream, deadline, deadline_message, monotonic, label):
    if deadline is None:
        return
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _deadline_error(deadline_message)
    active_socket = getattr(getattr(getattr(stream, "fp", None), "raw", None), "_sock", None)
    settimeout = getattr(active_socket, "settimeout", None)
    if not callable(settimeout):
        return
    gettimeout = getattr(active_socket, "gettimeout", None)
    try:
        existing_timeout = gettimeout() if callable(gettimeout) else None
        if existing_timeout is not None:
            remaining = min(remaining, existing_timeout)
        settimeout(remaining)
    except (OSError, TypeError, ValueError) as exc:
        raise ApiResponseError(f"could not read {label}") from exc


def _close_stream(stream):
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except OSError:
            pass


def _open_with_deadline(
    request, timeout, *, deadline=None, deadline_message=None, monotonic=None,
):
    """Bound opener stages that socket timeouts do not reliably cover, such as DNS."""
    clock = monotonic or time.monotonic
    effective_timeout = _effective_timeout(timeout, deadline, deadline_message, clock)
    if deadline is None:
        return _OPENER.open(request, timeout=effective_timeout)

    remaining = deadline - clock()
    if remaining <= 0:
        raise _deadline_error(deadline_message)
    result = queue.Queue(maxsize=1)
    cancelled = threading.Event()
    state = {"stream": None, "error": None}
    lock = threading.Lock()

    def open_request():
        try:
            stream = _OPENER.open(request, timeout=effective_timeout)
        except BaseException as exc:
            with lock:
                should_close = cancelled.is_set()
                if not should_close:
                    state["error"] = exc
                    result.put((False, exc))
            if should_close:
                _close_stream(exc)
            return
        with lock:
            state["stream"] = stream
            should_close = cancelled.is_set()
            if not should_close:
                result.put((True, stream))
        if should_close:
            _close_stream(stream)

    threading.Thread(target=open_request, daemon=True).start()
    try:
        succeeded, value = result.get(timeout=remaining)
    except queue.Empty as exc:
        with lock:
            cancelled.set()
            value = state["stream"] if state["stream"] is not None else state["error"]
        _close_stream(value)
        raise _deadline_error(deadline_message) from exc
    if clock() >= deadline:
        with lock:
            cancelled.set()
        _close_stream(value)
        raise _deadline_error(deadline_message)
    if not succeeded:
        raise value
    return value


def _read_limited(
    stream, limit, label, headers=None, *, deadline=None,
    deadline_message=None, monotonic=None,
) -> bytes:
    clock = monotonic or time.monotonic
    length = header_value(headers or {}, "Content-Length")
    try:
        if length is not None and int(length) > limit:
            raise ApiResponseError(f"{label} exceeds the size limit")
    except ValueError:
        pass
    chunks = []
    total = 0
    while True:
        if deadline is not None and clock() >= deadline:
            raise _deadline_error(deadline_message)
        try:
            _set_read_timeout(stream, deadline, deadline_message, clock, label)
            chunk = stream.read(min(CHUNK_SIZE, limit - total + 1))
        except socket.timeout as exc:
            if deadline is not None and clock() >= deadline:
                raise _deadline_error(deadline_message) from exc
            raise ApiResponseError(f"could not read {label}") from exc
        except (OSError, TypeError, ValueError, http.client.HTTPException) as exc:
            raise ApiResponseError(f"could not read {label}") from exc
        if deadline is not None and clock() >= deadline:
            raise _deadline_error(deadline_message)
        if not isinstance(chunk, (bytes, bytearray)):
            raise ApiResponseError(f"could not read {label}")
        total += len(chunk)
        if total > limit:
            raise ApiResponseError(f"{label} exceeds the size limit")
        if not chunk:
            return b"".join(chunks)
        chunks.append(bytes(chunk))


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
    """Open one authenticated API request without following redirects."""
    clock = monotonic or time.monotonic
    try:
        with _open_with_deadline(
            request,
            timeout,
            deadline=deadline,
            deadline_message=deadline_message,
            monotonic=clock,
        ) as response:
            status = _response_status(response)
            if 300 <= status < 400:
                raise ApiResponseError("SuperToken request was redirected and was not followed")
            if not 200 <= status < 300:
                raise ApiResponseError(f"SuperToken request failed (HTTP {status})")
            return ApiResponse(
                status,
                dict(getattr(response, "headers", {}) or {}),
                _read_limited(
                    response,
                    MAX_API_BODY_BYTES,
                    "API response",
                    getattr(response, "headers", {}),
                    deadline=deadline,
                    deadline_message=deadline_message,
                    monotonic=clock,
                ),
            )
    except urllib.error.HTTPError as exc:
        try:
            _read_limited(
                exc,
                MAX_ERROR_BODY_BYTES,
                "API error response",
                exc.headers or {},
                deadline=deadline,
                deadline_message=deadline_message,
                monotonic=clock,
            )
        except ApiResponseError as read_error:
            if getattr(read_error, "deadline_exceeded", False):
                raise
        finally:
            exc.close()
        if 300 <= exc.code < 400:
            raise ApiResponseError("SuperToken request was redirected and was not followed") from None
        raise ApiResponseError(f"SuperToken request failed (HTTP {exc.code})") from None
    except ApiResponseError:
        raise
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise ApiResponseError("SuperToken request could not be completed") from exc


def _effective_timeout(timeout, deadline=None, deadline_message=None, monotonic=None):
    _validate_timeout(timeout)
    if deadline is None:
        return timeout
    clock = monotonic or time.monotonic
    remaining = deadline - clock()
    if remaining <= 0:
        raise _deadline_error(deadline_message)
    return min(timeout, remaining)


def request_json(
    method, url, api_key, timeout, payload=None, headers=None, *,
    deadline=None, deadline_message=None, monotonic=None,
) -> ApiResponse:
    """Send one authenticated JSON request; redirects are always rejected."""
    _validate_https_url(url, "authenticated request URL")
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


def _open_public_request(
    request, timeout, *, deadline=None, deadline_message=None, monotonic=None,
):
    """Open a validated presigned or result URL without following redirects."""
    clock = monotonic or time.monotonic
    try:
        response = _open_with_deadline(
            request,
            timeout,
            deadline=deadline,
            deadline_message=deadline_message,
            monotonic=clock,
        )
    except urllib.error.HTTPError as exc:
        try:
            _read_limited(
                exc,
                MAX_ERROR_BODY_BYTES,
                "media error response",
                exc.headers or {},
                deadline=deadline,
                deadline_message=deadline_message,
                monotonic=clock,
            )
        except ApiResponseError as read_error:
            if getattr(read_error, "deadline_exceeded", False):
                raise
        finally:
            exc.close()
        if 300 <= exc.code < 400:
            raise ApiResponseError("media request was redirected and was not followed") from None
        raise ApiResponseError(f"media request failed (HTTP {exc.code})") from None
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise ApiResponseError("media request could not be completed") from exc
    status = _response_status(response)
    if 300 <= status < 400:
        response.close()
        raise ApiResponseError("media request was redirected and was not followed")
    if not 200 <= status < 300:
        response.close()
        raise ApiResponseError(f"media request failed (HTTP {status})")
    return response


def _read_local_file(path, max_file_bytes):
    source = Path(path).expanduser()
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ApiUsageError("media file could not be read") from exc
    if not source.is_file() or size < 0:
        raise ApiUsageError("media file could not be read")
    if size > max_file_bytes:
        raise ApiUsageError("media file exceeds the size limit")
    try:
        with source.open("rb") as handle:
            data = handle.read(max_file_bytes + 1)
    except OSError as exc:
        raise ApiUsageError("media file could not be read") from exc
    if len(data) > max_file_bytes:
        raise ApiUsageError("media file exceeds the size limit")
    return source, data


def upload_media_files(
    upload_url, paths, timeout, headers=None, *, method="PUT", max_file_bytes=MAX_MEDIA_BYTES,
) -> list[dict]:
    """Upload local files to a server-provided presigned HTTPS URL."""
    validate_public_url(upload_url, "upload_url")
    _validate_timeout(timeout)
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ApiUsageError("at least one media file is required")
    if not isinstance(max_file_bytes, int) or max_file_bytes <= 0:
        raise ApiUsageError("max_file_bytes must be positive")
    if not isinstance(method, str) or not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Z]+", method):
        raise ApiUsageError("upload method must be an uppercase HTTP token")
    if headers is not None and not hasattr(headers, "items"):
        raise ApiUsageError("upload headers must be an object")
    results = []
    for raw_path in paths:
        source, data = _read_local_file(raw_path, max_file_bytes)
        request = _PresignedUploadRequest(upload_url, data=data, method=method)
        for name, value in (headers or {}).items():
            _validate_header(name, value)
            request.add_header(name, value)
        with _open_public_request(request, timeout) as response:
            _read_limited(response, MAX_ERROR_BODY_BYTES, "upload response", getattr(response, "headers", {}))
        results.append(
            {
                "path": str(source.resolve()),
                "bytes_written": len(data),
                "upload_url": _sanitize_url(upload_url),
            }
        )
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


def _copy_download(
    response, output, *, deadline=None, deadline_message=None, monotonic=time.monotonic,
):
    length = header_value(getattr(response, "headers", {}), "Content-Length")
    try:
        if length is not None and int(length) > MAX_MEDIA_BYTES:
            raise ApiResponseError("video download exceeds the size limit")
    except ValueError:
        pass
    total = 0
    while True:
        if deadline is not None and monotonic() >= deadline:
            raise _deadline_error(deadline_message)
        try:
            _set_read_timeout(response, deadline, deadline_message, monotonic, "video download")
            chunk = response.read(min(CHUNK_SIZE, MAX_MEDIA_BYTES - total + 1))
        except socket.timeout as exc:
            if deadline is not None and monotonic() >= deadline:
                raise _deadline_error(deadline_message) from exc
            raise ApiResponseError("could not read video download") from exc
        except (OSError, TypeError, ValueError, http.client.HTTPException) as exc:
            raise ApiResponseError("could not read video download") from exc
        if deadline is not None and monotonic() >= deadline:
            raise _deadline_error(deadline_message)
        if not isinstance(chunk, (bytes, bytearray)):
            raise ApiResponseError("could not read video download")
        total += len(chunk)
        if total > MAX_MEDIA_BYTES:
            raise ApiResponseError("video download exceeds the size limit")
        if not chunk:
            break
        try:
            output.write(chunk)
        except OSError as exc:
            raise ApiResponseError("could not write video download") from exc
    if total == 0:
        raise ApiResponseError("video download was empty")
    return total


def _download_one(
    url, destination, timeout, resource_key=None, *, deadline=None,
    deadline_message=None, monotonic=time.monotonic,
):
    validate_public_url(url, "result URL")
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", VIDEO_DOWNLOAD_USER_AGENT)
    if resource_key is not None:
        request.add_unredirected_header("Authorization", f"Bearer {resource_key}")
    part = None
    try:
        with _open_public_request(
            request,
            timeout,
            deadline=deadline,
            deadline_message=deadline_message,
            monotonic=monotonic,
        ) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".part",
                dir=destination.parent,
                delete=False,
            ) as output:
                part = Path(output.name)
                size = _copy_download(
                    response,
                    output,
                    deadline=deadline,
                    deadline_message=deadline_message,
                    monotonic=monotonic,
                )
        if deadline is not None and monotonic() >= deadline:
            raise _deadline_error(deadline_message)
        os.replace(part, destination)
        part = None
        return size
    except ApiResponseError:
        raise
    except OSError as exc:
        raise ApiResponseError("video download could not be completed") from exc
    finally:
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass


def download_video_items(
    items, output_dir, timeout, resource_key=None, *, output_path=None,
    deadline=None, deadline_message=None, monotonic=time.monotonic,
) -> list[dict]:
    """Save result videos atomically, one same-directory part file at a time."""
    timeout = _effective_timeout(timeout, deadline, deadline_message, monotonic)
    if not isinstance(items, (list, tuple)) or not items:
        raise ApiUsageError("at least one video result is required")
    if resource_key is not None and (not isinstance(resource_key, str) or not resource_key):
        raise ApiUsageError("resource_key must be a non-empty token")
    if output_path is not None and len(items) != 1:
        raise ApiUsageError("output_path requires exactly one video result")
    fixed_destination = Path(output_path).expanduser() if output_path is not None else None
    destination_root = fixed_destination.parent if fixed_destination is not None else Path(output_dir).expanduser()
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ApiUsageError("output_dir could not be created") from exc
    if not destination_root.is_dir():
        raise ApiUsageError("output_dir must be a directory")
    destination_root = destination_root.resolve()
    if fixed_destination is not None:
        fixed_destination = fixed_destination.resolve()
    used_names = set()
    saved = []
    for index, item in enumerate(items):
        timeout = _effective_timeout(timeout, deadline, deadline_message, monotonic)
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ApiUsageError("each video result must contain a URL")
        url_auth = item.get("url_auth")
        if url_auth not in (None, "none", "resource_api_key"):
            raise ApiUsageError("video result has an unsupported url_auth value")
        key = resource_key if url_auth == "resource_api_key" else None
        if url_auth == "resource_api_key" and key is None:
            raise ApiUsageError("resource_key is required for this video result")
        destination = fixed_destination or _unique_output_path(
            destination_root,
            _safe_output_name(item.get("filename") or item.get("name"), index),
            used_names,
        )
        size = _download_one(
            item["url"],
            destination,
            timeout,
            key,
            deadline=deadline,
            deadline_message=deadline_message,
            monotonic=monotonic,
        )
        saved.append(
            {
                "path": str(destination.resolve()),
                "bytes_written": size,
                "url": _sanitize_url(item["url"]),
            }
        )
    return saved
