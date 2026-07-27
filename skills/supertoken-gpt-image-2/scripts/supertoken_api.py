#!/usr/bin/env python3
import base64
import binascii
import http.client
import ipaddress
import json
import os
import queue
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApiResponse:
    status: int
    headers: dict
    body: bytes


@dataclass(frozen=True)
class MultipartFile:
    field: str
    path: Path
    content_type: str


class ApiUsageError(ValueError):
    pass


class ApiResponseError(RuntimeError):
    pass


MAX_IMAGES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_MULTIPART_BYTES = 100 * 1024 * 1024
MAX_API_BODY_BYTES = 384 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 1 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
FORMAT_SUFFIX = {"png": ".png", "jpeg": ".jpeg", "webp": ".webp"}


@dataclass(frozen=True)
class ValidatedImage:
    path: Path
    format: str
    size: int


@dataclass(frozen=True)
class SavedImage:
    path: Path
    bytes_written: int
    format: str
    source_url: str | None


@dataclass(frozen=True)
class ResolvedAddress:
    family: int
    socket_type: int
    protocol: int
    socket_address: tuple


def detect_image_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    raise ApiUsageError("图片格式必须是 PNG、JPEG 或 WebP。")


def validate_local_images(paths):
    if not paths or len(paths) > MAX_IMAGES:
        raise ApiUsageError("参考图片最多 10 张，且至少需要 1 张。")
    validated = []
    total = 0
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise ApiUsageError(f"图片文件不存在：{path}。")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ApiUsageError(f"单个图片文件不能超过 20 MiB：{path}。")
        total += size
        if total > MAX_MULTIPART_BYTES:
            raise ApiUsageError("multipart 文件总量不能超过 100 MiB。")
        with path.open("rb") as stream:
            image_format = detect_image_format(stream.read(12))
        validated.append(ValidatedImage(path.resolve(), image_format, size))
    return validated


def _deadline_error(deadline_message):
    error = ApiResponseError(deadline_message or "响应读取超过等待时限。")
    error.deadline_exceeded = True
    return error


def _run_with_deadline(
    operation,
    deadline,
    deadline_message,
    monotonic,
    cancel=None,
):
    if deadline is None:
        return operation()
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _deadline_error(deadline_message)

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
        raise _deadline_error(deadline_message) from exc
    if monotonic() >= deadline:
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass
        raise _deadline_error(deadline_message)
    if not succeeded:
        raise value
    return value


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


def _resolve_result_addresses(
    host,
    port,
    *,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    monotonic = monotonic or time.monotonic
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        try:
            resolved = _run_with_deadline(
                lambda: socket.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                ),
                deadline,
                deadline_message,
                monotonic,
            )
        except ApiResponseError:
            raise
        except (OSError, UnicodeError) as exc:
            raise ApiResponseError("图片下载地址解析失败。") from exc
        addresses = []
        for family, socket_type, protocol, _canonical_name, socket_address in resolved:
            try:
                parsed_address = ipaddress.ip_address(
                    socket_address[0].split("%", 1)[0]
                )
            except (AttributeError, IndexError, TypeError, ValueError) as exc:
                raise ApiResponseError("图片下载地址解析失败。") from exc
            if _is_unsafe_address(parsed_address):
                raise ApiResponseError("图片下载地址指向不安全的网络地址。")
            addresses.append(ResolvedAddress(
                family, socket_type, protocol, socket_address
            ))
        if not addresses:
            raise ApiResponseError("图片下载地址解析失败。")
        return tuple(dict.fromkeys(addresses))

    if _is_unsafe_address(literal):
        raise ApiResponseError("图片下载地址指向不安全的网络地址。")
    if literal.version == 4:
        return (ResolvedAddress(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            (str(literal), port),
        ),)
    return (ResolvedAddress(
        socket.AF_INET6,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        (str(literal), port, 0, 0),
    ),)


def _validate_result_url(
    url,
    *,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    if not isinstance(url, str) or not url or any(
        character.isspace()
        or ord(character) < 32
        or 0x7F <= ord(character) <= 0x9F
        for character in url
    ):
        raise ApiResponseError("图片下载地址无效。")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ApiResponseError("图片下载地址无效。") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "#" in url
        or port == 0
    ):
        raise ApiResponseError("图片下载地址无效。")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise ApiResponseError("图片下载地址无效。") from exc
    port = port or 443
    addresses = _resolve_result_addresses(
        ascii_host,
        port,
        deadline=deadline,
        deadline_message=deadline_message,
        monotonic=monotonic,
    )
    return parsed, ascii_host, port, addresses


def download_image(
    url,
    timeout,
    *,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    monotonic = monotonic or time.monotonic
    parsed, host, port, addresses = _validate_result_url(
        url,
        deadline=deadline,
        deadline_message=deadline_message,
        monotonic=monotonic,
    )
    request_target = urllib.parse.urlunsplit((
        "", "", parsed.path or "/", parsed.query, ""
    ))
    try:
        with _open_result_url(
            host=host,
            port=port,
            addresses=addresses,
            request_target=request_target,
            timeout=timeout,
            deadline=deadline,
            deadline_message=deadline_message,
            monotonic=monotonic,
        ) as response:
            status = getattr(response, "status", None)
            if isinstance(status, int) and 300 <= status < 400:
                raise ApiResponseError("图片下载不允许重定向。")
            if isinstance(status, int) and not 200 <= status < 300:
                raise ApiResponseError("图片下载请求失败。")
            return bounded_read(
                response,
                MAX_DOWNLOAD_BYTES,
                "图片下载响应",
                response.headers,
                deadline=deadline,
                deadline_message=deadline_message,
                monotonic=monotonic,
            )
    except urllib.error.HTTPError as exc:
        try:
            if 300 <= exc.code < 400:
                raise ApiResponseError("图片下载不允许重定向。") from exc
            raise ApiResponseError("图片下载请求失败。") from exc
        finally:
            exc.close()
    except ApiResponseError:
        raise
    except (
        ValueError,
        OSError,
        urllib.error.URLError,
        http.client.HTTPException,
    ) as exc:
        raise ApiResponseError("图片下载请求失败。") from exc


def _item_bytes(
    item,
    timeout,
    *,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    encoded = item.get("b64_json")
    if encoded is not None:
        if not isinstance(encoded, (str, bytes, bytearray)) or not encoded:
            raise ApiResponseError("图片响应中的 Base64 无效。")
        try:
            value = base64.b64decode(encoded, validate=True)
        except (binascii.Error, TypeError, UnicodeError, ValueError) as exc:
            raise ApiResponseError("图片响应中的 Base64 无效。") from exc
        if len(value) > MAX_DOWNLOAD_BYTES:
            raise ApiResponseError("单张图片结果超过 64 MiB 限制。")
        return value, None
    url = item.get("url")
    if url is not None:
        if not isinstance(url, str) or not url:
            raise ApiResponseError("图片响应中的 URL 无效。")
        if deadline is None:
            return download_image(url, timeout), url
        return download_image(
            url,
            timeout,
            deadline=deadline,
            deadline_message=deadline_message,
            monotonic=monotonic,
        ), url
    raise ApiResponseError("图片响应中没有 url 或 b64_json。")


def _final_output_path(
    requested, index, count, image_format, preserve_requested_path=False,
):
    requested = Path(requested).expanduser()
    if preserve_requested_path:
        return Path(os.path.abspath(requested))
    suffix = FORMAT_SUFFIX[image_format]
    stem = requested.stem
    if count > 1:
        stem = f"{stem}-{index + 1}"
    return Path(os.path.abspath(requested.with_name(stem + suffix)))


def _unique_temp_path(final, suffix):
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{final.name}.", suffix=suffix, dir=final.parent
    )
    return descriptor, Path(raw_path)


def _write_staged_file(final, data):
    descriptor, part = _unique_temp_path(final, ".part")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if part.stat().st_size == 0:
            raise ApiResponseError("图片结果为空。")
        return part
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        part.unlink(missing_ok=True)
        raise


def _backup_destination(final):
    for _attempt in range(100):
        backup = final.parent / f".{final.name}.{uuid.uuid4().hex}.backup"
        try:
            if final.is_symlink():
                os.symlink(
                    os.readlink(final),
                    backup,
                    target_is_directory=final.is_dir(),
                )
            else:
                os.link(final, backup)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ApiResponseError("无法为现有图片输出创建安全备份。") from exc
        return backup
    raise ApiResponseError("无法为现有图片输出分配安全备份路径。")


def validate_image_item_count(items, expected_count=None):
    if not isinstance(items, list) or not items:
        raise ApiResponseError("图片响应中没有有效的结果。")
    if len(items) > MAX_IMAGES:
        raise ApiResponseError("图片响应最多包含 10 个结果。")
    if expected_count is not None and len(items) != expected_count:
        raise ApiResponseError(
            f"图片响应数量与请求不符：预期 {expected_count} 个结果。"
        )


def save_image_items(
    items,
    output_path,
    timeout,
    preserve_requested_path=False,
    expected_count=None,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    validate_image_item_count(items, expected_count)
    monotonic = monotonic or time.monotonic
    deadline_message = deadline_message or "图片结果下载超过等待时限。"
    staged = []
    backups = []
    promoted = []
    total = 0
    try:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ApiResponseError("图片响应项不是对象。")
            item_timeout = timeout
            if deadline is not None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise _deadline_error(deadline_message)
                item_timeout = min(timeout, remaining)
            data, source_url = _item_bytes(
                item,
                item_timeout,
                deadline=deadline,
                deadline_message=deadline_message,
                monotonic=monotonic,
            )
            if deadline is not None and deadline - monotonic() <= 0:
                raise _deadline_error(deadline_message)
            total += len(data)
            if total > MAX_OUTPUT_BYTES:
                raise ApiResponseError("图片结果总大小超过 256 MiB 限制。")
            try:
                image_format = detect_image_format(data)
            except ApiUsageError as exc:
                raise ApiResponseError(
                    "图片响应不是可识别的 PNG、JPEG 或 WebP。"
                ) from exc
            final = _final_output_path(
                output_path,
                index,
                len(items),
                image_format,
                preserve_requested_path,
            )
            final.parent.mkdir(parents=True, exist_ok=True)
            if not final.is_symlink() and final.is_dir():
                raise ApiResponseError("图片输出路径不能是目录。")
            part = _write_staged_file(final, data)
            staged.append((part, final, data, image_format, source_url))

        for part, final, data, image_format, source_url in staged:
            backup = None
            if os.path.lexists(final):
                backup = _backup_destination(final)
            backups.append((final, backup))
            os.replace(part, final)
            promoted.append(final)
        return [
            SavedImage(final, len(data), image_format, source_url)
            for _part, final, data, image_format, source_url in staged
        ]
    except Exception:
        for final, backup in reversed(backups):
            if backup is not None and os.path.lexists(backup):
                os.replace(backup, final)
            elif final in promoted:
                final.unlink(missing_ok=True)
        raise
    finally:
        for part, *_rest in staged:
            part.unlink(missing_ok=True)
        for _final, backup in backups:
            if backup is not None:
                if os.path.lexists(backup):
                    backup.unlink()


def endpoint_url(base_url, route):
    base = base_url.rstrip("/")
    if not route.startswith("/v1/"):
        raise ApiUsageError("API 路径必须以 /v1/ 开头。")
    if base.endswith("/image-wrapper/v1"):
        if route not in {"/v1/images/generations", "/v1/images/edits"}:
            raise ApiUsageError("旧版图片基址只支持同步生成和编辑。")
        return base + route.removeprefix("/v1")
    if base.endswith("/v1"):
        return base + route.removeprefix("/v1")
    return base + route


def _quoted_header_value(value):
    value = str(value)
    if "\r" in value or "\n" in value:
        raise ApiUsageError("multipart 字段名和文件名不能包含换行符。")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def encode_multipart(fields, files, boundary=None):
    boundary = boundary or uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{_quoted_header_value(name)}"'
            .encode() + b"\r\n\r\n",
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    for item in files:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{_quoted_header_value(item.field)}"; '
                f'filename="{_quoted_header_value(item.path.name)}"\r\n'
            ).encode(),
            f"Content-Type: {item.content_type}\r\n\r\n".encode(),
            item.path.read_bytes(),
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_AUTHENTICATED_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _open_url(request, timeout):
    return _AUTHENTICATED_OPENER.open(request, timeout=timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self._resolved_address = address

    def connect(self):
        address = self._resolved_address
        raw_socket = socket.socket(
            address.family, address.socket_type, address.protocol
        )
        try:
            raw_socket.settimeout(self.timeout)
            if self.source_address:
                raw_socket.bind(self.source_address)
            raw_socket.connect(address.socket_address)
            try:
                raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self.sock = self._context.wrap_socket(
                raw_socket, server_hostname=self.host
            )
        except BaseException:
            raw_socket.close()
            raise


class _ResultResponseContext:
    def __init__(self, connection, response):
        self.connection = connection
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.response.close()
        finally:
            self.connection.close()
        return False


def _open_result_url(
    *,
    host,
    port,
    addresses,
    request_target,
    timeout,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    monotonic = monotonic or time.monotonic
    cancelled = threading.Event()
    current = {"connection": None}

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
            connection_timeout = timeout
            if deadline is not None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise _deadline_error(deadline_message)
                connection_timeout = min(timeout, remaining)
            connection = _PinnedHTTPSConnection(
                host, port, address, connection_timeout
            )
            current["connection"] = connection
            try:
                connection.request(
                    "GET",
                    request_target,
                    headers={"Accept-Encoding": "identity", "Connection": "close"},
                )
                response = connection.getresponse()
            except (OSError, UnicodeError, http.client.HTTPException) as exc:
                connection.close()
                last_error = exc
                continue
            return _ResultResponseContext(connection, response)
        if last_error is not None:
            raise last_error
        raise OSError("no approved result address")

    return _run_with_deadline(
        open_approved_address,
        deadline,
        deadline_message,
        monotonic,
        cancel=cancel,
    )


def _content_length(headers):
    value = header_value(headers or {}, "Content-Length")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _check_deadline(deadline, deadline_message, monotonic):
    if deadline is not None and monotonic() >= deadline:
        raise _deadline_error(deadline_message)


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


def bounded_read(
    stream,
    limit,
    label,
    headers=None,
    *,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    length = _content_length(headers)
    if length is not None and length > limit:
        raise ApiResponseError(f"{label}超过大小限制。")
    monotonic = monotonic or time.monotonic

    def read_chunks():
        read = getattr(stream, "read1", None) if deadline is not None else None
        if not callable(read):
            read = stream.read
        chunks = []
        total = 0
        while True:
            requested = min(64 * 1024, limit - total + 1)
            _check_deadline(deadline, deadline_message, monotonic)
            try:
                chunk = read(requested)
            except TypeError as exc:
                raise ApiResponseError(f"{label}无法按限制读取。") from exc
            _check_deadline(deadline, deadline_message, monotonic)
            if not isinstance(chunk, (bytes, bytearray)):
                raise ApiResponseError(f"{label}读取结果无效。")
            if len(chunk) > requested or total + len(chunk) > limit:
                raise ApiResponseError(f"{label}超过大小限制。")
            if not chunk:
                return b"".join(chunks)
            chunks.append(bytes(chunk))
            total += len(chunk)

    return _run_with_deadline(
        read_chunks,
        deadline,
        deadline_message,
        monotonic,
        cancel=lambda: _cancel_stream(stream),
    )


def _open_once(
    request,
    timeout,
    *,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    try:
        with _open_url(request, timeout) as response:
            headers = dict(response.headers)
            limit = (
                MAX_API_BODY_BYTES
                if 200 <= response.status < 300 else MAX_ERROR_BODY_BYTES
            )
            body = bounded_read(
                response,
                limit,
                "SuperToken 响应",
                headers,
                deadline=deadline,
                deadline_message=deadline_message,
                monotonic=monotonic,
            )
            return ApiResponse(response.status, headers, body)
    except urllib.error.HTTPError as exc:
        try:
            headers = dict(exc.headers or {})
            body = bounded_read(
                exc,
                MAX_ERROR_BODY_BYTES,
                "SuperToken 错误响应",
                headers,
                deadline=deadline,
                deadline_message=deadline_message,
                monotonic=monotonic,
            )
            return ApiResponse(exc.code, headers, body)
        finally:
            exc.close()


def _open(
    request,
    timeout,
    *,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    monotonic = monotonic or time.monotonic
    return _run_with_deadline(
        lambda: _open_once(
            request,
            timeout,
            deadline=deadline,
            deadline_message=deadline_message,
            monotonic=monotonic,
        ),
        deadline,
        deadline_message,
        monotonic,
    )


def _validate_authenticated_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ApiUsageError("认证 API 请求必须使用有效的绝对 HTTPS 地址。") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ApiUsageError("认证 API 请求必须使用有效的绝对 HTTPS 地址。")


def request_json(
    method,
    url,
    api_key,
    timeout,
    payload=None,
    headers=None,
    *,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    _validate_authenticated_url(url)
    body = None if payload is None else json.dumps(
        payload, ensure_ascii=False
    ).encode("utf-8")
    request_headers = {}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    request.add_unredirected_header("Authorization", f"Bearer {api_key}")
    for name, value in (headers or {}).items():
        if name.lower() in {"authorization", "idempotency-key"}:
            request.add_unredirected_header(name, value)
        else:
            request.add_header(name, value)
    return _open(
        request,
        timeout,
        deadline=deadline,
        deadline_message=deadline_message,
        monotonic=monotonic,
    )


def request_multipart(method, url, api_key, timeout, fields, files, headers=None):
    _validate_authenticated_url(url)
    body, content_type = encode_multipart(fields, files)
    request_headers = {"Content-Type": content_type}
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    request.add_unredirected_header("Authorization", f"Bearer {api_key}")
    for name, value in (headers or {}).items():
        if name.lower() in {"authorization", "idempotency-key"}:
            request.add_unredirected_header(name, value)
        else:
            request.add_header(name, value)
    return _open(request, timeout)


KEY_PATTERN = re.compile(r"(?:sk-|ak_|wk-)[A-Za-z0-9_-]{8,}")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
IMAGE_BASE64_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+/=])(?:iVBORw0KGg|/9j/|UklGR)[A-Za-z0-9+/=]{16,}"
)


def sanitize_url(value, *secrets):
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "[REDACTED URL]"
    if parsed.scheme and host:
        display_host = host.lower()
        if ":" in display_host:
            display_host = f"[{display_host}]"
        if port is not None:
            display_host = f"{display_host}:{port}"
        return urllib.parse.urlunsplit((
            parsed.scheme.lower(),
            display_host,
            _sanitize_text(parsed.path, secrets),
            "",
            "",
        ))
    return _sanitize_text(parsed.path, secrets) or "[REDACTED URL]"


def _sanitize_text(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = KEY_PATTERN.sub("[REDACTED]", text)
    text = URL_PATTERN.sub(
        lambda match: sanitize_url(match.group(0), *secrets), text
    )
    text = IMAGE_BASE64_PATTERN.sub("[REDACTED IMAGE DATA]", text)
    return "".join(
        character
        for character in text
        if character == "\n"
        or not (ord(character) < 32 or 0x7F <= ord(character) <= 0x9F)
    )


def sanitize_server_text(value, *secrets):
    if not isinstance(value, str):
        return "[REDACTED]"
    return _sanitize_text(value, secrets)


def _sanitize_json(value, secrets, key=None):
    if isinstance(key, str) and key.lower() == "b64_json":
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            sanitize_server_text(str(item_key), *secrets): _sanitize_json(
                item_value, secrets, str(item_key)
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json(item, secrets) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, secrets)
    return value


def sanitize_diagnostic(body, *secrets):
    text = body.decode("utf-8", errors="replace")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return _sanitize_text(text, secrets)[:1000]
    return json.dumps(
        _sanitize_json(value, secrets), ensure_ascii=False
    )[:1000]


def sanitize_request_id(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        return "[REDACTED]"
    sanitized = _sanitize_text(value, ())
    if sanitized != value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        return "[REDACTED]"
    return value


def parse_json_response(response, secrets=()):
    try:
        value = json.loads(response.body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        detail = sanitize_diagnostic(response.body, *secrets)
        raise ApiResponseError(
            f"SuperToken 返回了非 JSON 内容（HTTP {response.status}）：{detail}"
        ) from exc
    if not isinstance(value, dict):
        raise ApiResponseError("SuperToken 返回的 JSON 不是对象。")
    return value


def request_id(headers):
    lowered = {str(key).lower(): value for key, value in headers.items()}
    return next(
        (lowered[name] for name in ("x-request-id", "request-id", "cf-ray")
         if lowered.get(name)),
        None,
    )


def header_value(headers, name, default=None):
    target = name.lower()
    return next(
        (value for key, value in headers.items() if str(key).lower() == target),
        default,
    )


def classify_http_error(status, headers, key_kind, model=None):
    env_name = (
        "SUPERTOKEN_RESOURCE_API_KEY"
        if key_kind == "resource"
        else "SUPERTOKEN_API_KEY"
    )
    if status == 400:
        return "请求字段无效，请检查错误参数和请求结构。"
    if status == 401:
        return f"{env_name} 无效或已经失效。"
    if status == 403:
        target = f"模型 {model}" if model else "当前资源"
        return f"{env_name} 无权访问{target}。"
    if status == 409:
        return "同一 Idempotency-Key 对应了不同请求，请为新请求使用新的幂等键。"
    if status == 413:
        return "图片文件或 multipart 请求总大小超过限制。"
    if status == 429:
        return "请求频率或账户额度受限。"
    if 500 <= status <= 599:
        identifier = request_id(headers)
        if identifier:
            identifier = sanitize_request_id(identifier)
        suffix = f" 请求 ID：{identifier}。" if identifier else ""
        return f"SuperToken 图片服务暂时不可用（HTTP {status}）。{suffix}".strip()
    return f"SuperToken 图片请求失败（HTTP {status}）。"
