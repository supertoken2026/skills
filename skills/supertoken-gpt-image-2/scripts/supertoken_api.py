#!/usr/bin/env python3
import base64
import binascii
import json
import re
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


def _open(request, timeout):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return ApiResponse(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        return ApiResponse(exc.code, dict(exc.headers), exc.read())


def request_json(method, url, api_key, timeout, payload=None, headers=None):
    body = None if payload is None else json.dumps(
        payload, ensure_ascii=False
    ).encode("utf-8")
    request_headers = {"Authorization": f"Bearer {api_key}"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    return _open(request, timeout)


def request_multipart(method, url, api_key, timeout, fields, files, headers=None):
    body, content_type = encode_multipart(fields, files)
    request_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": content_type,
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    return _open(request, timeout)


KEY_PATTERN = re.compile(r"(?:sk-|ak_|wk-)[A-Za-z0-9_-]{8,}")


def sanitize_diagnostic(body, *secrets):
    text = body.decode("utf-8", errors="replace")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return KEY_PATTERN.sub("[REDACTED]", text)[:1000]


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
    if status in {502, 503}:
        identifier = request_id(headers)
        suffix = f" 请求 ID：{identifier}。" if identifier else ""
        return f"SuperToken 图片服务暂时不可用（HTTP {status}）。{suffix}".strip()
    return f"SuperToken 图片请求失败（HTTP {status}）。"
