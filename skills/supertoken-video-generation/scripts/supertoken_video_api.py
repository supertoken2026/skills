"""Small, defensive HTTP and file primitives for SuperToken video workflows."""

import ipaddress
import json
import os
import re
import tempfile
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


@dataclass(frozen=True)
class ApiResponse:
    status: int
    headers: dict
    body: bytes


class ApiUsageError(ValueError):
    """Raised for unsafe input."""


class ApiResponseError(RuntimeError):
    """Raised for remote HTTP or media failures without exposing server data."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())
_KEY_PATTERN = re.compile(r"\b(?:sk|ak|wk)[_-][A-Za-z0-9._~-]+", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SENSITIVE_PARAMETER_PATTERN = re.compile(
    r"\b(?:access[_-]?token|api[_-]?key|authorization|credential|key|secret|signature|sig|token|x-amz-[a-z0-9-]+)=[^\s&]+",
    re.IGNORECASE,
)


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
    text = value
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[REDACTED]")
    text = _URL_PATTERN.sub(lambda match: _sanitize_url(match.group(0)), text)
    text = _KEY_PATTERN.sub("[REDACTED]", text)
    text = _SENSITIVE_PARAMETER_PATTERN.sub("[REDACTED]", text)
    return "".join(char for char in text if char == "\n" or 32 <= ord(char) < 127)


def _sanitize_json(value, secrets=(), field_name=None):
    if isinstance(field_name, str) and re.search(
        r"(?:key|token|secret|signature|sig|authorization|credential|url)", field_name, re.I
    ):
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


def _bounded_read(stream, limit, label, headers=None) -> bytes:
    length = header_value(headers or {}, "Content-Length")
    try:
        if length is not None and int(length) > limit:
            raise ApiResponseError(f"{label} exceeds the size limit")
    except ValueError:
        pass
    chunks = []
    total = 0
    while True:
        requested = min(CHUNK_SIZE, limit - total + 1)
        try:
            chunk = stream.read(requested)
        except (OSError, TypeError, ValueError) as exc:
            raise ApiResponseError(f"could not read {label}") from exc
        if not isinstance(chunk, (bytes, bytearray)) or len(chunk) > requested:
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


def _open_request(request, timeout) -> ApiResponse:
    """Open one authenticated request; the opener never follows redirects."""
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = _response_status(response)
            headers = dict(response.headers or {})
            body = _bounded_read(response, MAX_API_BODY_BYTES, "API response", headers)
    except urllib.error.HTTPError as exc:
        try:
            _bounded_read(exc, MAX_ERROR_BODY_BYTES, "API error response", exc.headers or {})
        finally:
            exc.close()
        raise ApiResponseError(f"SuperToken request failed (HTTP {exc.code})") from None
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ApiResponseError("SuperToken request could not be completed") from exc
    if 300 <= status < 400:
        raise ApiResponseError("SuperToken request was redirected and was not followed")
    if not 200 <= status < 300:
        raise ApiResponseError(f"SuperToken request failed (HTTP {status})")
    return ApiResponse(status, headers, body)


def request_json(method, url, api_key, timeout, payload=None, headers=None) -> ApiResponse:
    """Send one authenticated JSON request without following redirects."""
    _validate_authenticated_url(url)
    _validate_timeout(timeout)
    if not isinstance(method, str) or not method:
        raise ApiUsageError("method must be a non-empty string")
    if not isinstance(api_key, str) or not api_key or any(char.isspace() for char in api_key):
        raise ApiUsageError("API key must be a non-empty token")
    try:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
    return _open_request(request, timeout)


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
        or port == 0
    ):
        raise ApiUsageError(f"{label} must be a clean absolute HTTPS URL")
    address = _numeric_ipv4_address(host)
    if address is None:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return parsed
    if not address.is_global:
        raise ApiUsageError(f"{label} must not use a private or unsafe literal address")
    return parsed


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


def _open_public_request(request, timeout):
    try:
        response = _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            _bounded_read(exc, MAX_ERROR_BODY_BYTES, "media error response", exc.headers or {})
        finally:
            exc.close()
        raise ApiResponseError(f"media request failed (HTTP {exc.code})") from None
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ApiResponseError("media request could not be completed") from exc
    status = _response_status(response)
    if 300 <= status < 400:
        response.close()
        raise ApiResponseError("media request was redirected and was not followed")
    if not 200 <= status < 300:
        response.close()
        raise ApiResponseError(f"media request failed (HTTP {status})")
    return response


def upload_media_files(upload_url, paths, timeout, headers=None, *, max_file_bytes=MAX_MEDIA_BYTES) -> list[dict]:
    """Upload local media to a temporary HTTPS URL and return upload metadata."""
    _validate_public_url(upload_url, "upload_url")
    _validate_timeout(timeout)
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ApiUsageError("at least one media file is required")
    if not isinstance(max_file_bytes, int) or max_file_bytes <= 0:
        raise ApiUsageError("max_file_bytes must be positive")
    results = []
    for raw_path in paths:
        source, data = _read_local_file(raw_path, max_file_bytes)
        request = urllib.request.Request(upload_url, data=data, method="PUT")
        request.add_header("Content-Type", "application/octet-stream")
        for name, value in (headers or {}).items():
            _validate_header(name, value)
            if name.lower() == "authorization":
                raise ApiUsageError("upload authorization must be encoded in the temporary upload URL")
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


def _stage_download(url, destination, timeout, resource_key=None):
    _validate_public_url(url, "result URL")
    request = urllib.request.Request(url, method="GET")
    if resource_key is not None:
        request.add_unredirected_header("Authorization", f"Bearer {resource_key}")
    descriptor, raw_part = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part", dir=destination.parent)
    part = Path(raw_part)
    try:
        with os.fdopen(descriptor, "wb") as output, _open_public_request(request, timeout) as response:
            length = header_value(response.headers, "Content-Length")
            try:
                if length is not None and int(length) > MAX_MEDIA_BYTES:
                    raise ApiResponseError("video download exceeds the size limit")
            except ValueError:
                pass
            total = 0
            while True:
                chunk = response.read(min(CHUNK_SIZE, MAX_MEDIA_BYTES - total + 1))
                if not isinstance(chunk, (bytes, bytearray)):
                    raise ApiResponseError("could not read video download")
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise ApiResponseError("video download exceeds the size limit")
                if not chunk:
                    break
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total == 0:
            raise ApiResponseError("video download was empty")
        return part, total
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def download_video_items(items, output_dir, timeout, resource_key=None) -> list[dict]:
    """Download task result videos, then atomically promote their staged files."""
    _validate_timeout(timeout)
    if not isinstance(items, (list, tuple)) or not items:
        raise ApiUsageError("at least one video result is required")
    if resource_key is not None and (not isinstance(resource_key, str) or not resource_key):
        raise ApiUsageError("resource_key must be a non-empty token")
    destination_root = Path(output_dir).expanduser()
    destination_root.mkdir(parents=True, exist_ok=True)
    if not destination_root.is_dir():
        raise ApiUsageError("output_dir must be a directory")
    staged = []
    promoted = []
    backups = []
    used_names = set()
    try:
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise ApiUsageError("each video result must contain a URL")
            url_auth = item.get("url_auth")
            if url_auth not in (None, "resource_api_key"):
                raise ApiUsageError("video result has an unsupported url_auth value")
            key = resource_key if url_auth == "resource_api_key" else None
            if url_auth == "resource_api_key" and key is None:
                raise ApiUsageError("resource_key is required for this video result")
            destination = _unique_output_path(
                destination_root,
                _safe_output_name(item.get("filename") or item.get("name"), index),
                used_names,
            )
            part, size = _stage_download(item["url"], destination, timeout, key)
            staged.append((part, destination, size, item["url"]))
        for part, destination, _size, _url in staged:
            backup = None
            if os.path.lexists(destination):
                descriptor, raw_backup = tempfile.mkstemp(
                    prefix=f".{destination.name}.", suffix=".backup", dir=destination.parent
                )
                os.close(descriptor)
                backup = Path(raw_backup)
                os.replace(destination, backup)
            backups.append((destination, backup))
            os.replace(part, destination)
            promoted.append(destination)
    except Exception:
        for part, _destination, _size, _url in staged:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
        for destination, backup in reversed(backups):
            try:
                if backup is not None and os.path.lexists(backup):
                    os.replace(backup, destination)
                elif destination in promoted:
                    destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    for _destination, backup in backups:
        if backup is not None:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
    return [
        {"path": str(destination.resolve()), "bytes_written": size, "url": _sanitize_url(url)}
        for _part, destination, size, url in staged
    ]
