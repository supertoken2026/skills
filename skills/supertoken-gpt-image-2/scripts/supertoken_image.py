#!/usr/bin/env python3
import argparse
import base64
import binascii
import getpass
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path

import supertoken_api as api
from supertoken_config import (
    API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    MODEL_KEY,
    RESOURCE_API_KEY_ENV,
    RESOURCE_KEY,
    ConfigError,
    build_config,
    get_api_key,
    load_config,
    save_api_key,
    save_config,
)


def add_image_options(parser, include_images=False):
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output")
    parser.add_argument("--api-key")
    parser.add_argument("--resource-api-key")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--n", dest="count", type=int, default=1)
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", default="low")
    parser.add_argument("--format", dest="output_format", choices=["png", "jpeg", "webp"])
    parser.add_argument("--background", choices=["transparent", "opaque", "auto"])
    parser.add_argument("--param", action="append", default=[])
    parser.add_argument("--json-params")
    parser.add_argument("--raw-json")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--wait-timeout", type=int, default=900)
    parser.add_argument("--async", dest="async_mode", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--client-reference-id")
    parser.add_argument("--metadata-json")
    parser.add_argument("--allow-plaintext-key-store", action="store_true")
    if include_images:
        parser.add_argument("--image", action="append", default=[])
        parser.add_argument("--image-base64-file", action="append", default=[])
        parser.add_argument("--mask")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="使用 SuperToken GPT Image 2 图片服务。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    models = subparsers.add_parser("models", help="列出可用模型。")
    models.add_argument("--all", action="store_true")
    models.add_argument("--api-key")
    models.add_argument("--base-url")
    models.add_argument("--timeout", type=int, default=300)
    models.add_argument("--allow-plaintext-key-store", action="store_true")

    generate = subparsers.add_parser("generate", help="生成图片。")
    add_image_options(generate)

    edit = subparsers.add_parser("edit", help="编辑图片。")
    add_image_options(edit, include_images=True)

    task = subparsers.add_parser("task", help="查询异步任务。")
    task.add_argument("task_id")
    task.add_argument("--resource-api-key")
    task.add_argument("--base-url")
    task.add_argument("--timeout", type=int, default=300)

    wait = subparsers.add_parser("wait", help="等待异步任务完成。")
    wait.add_argument("task_id")
    wait.add_argument("--output", required=True)
    wait.add_argument("--resource-api-key")
    wait.add_argument("--base-url")
    wait.add_argument("--timeout", type=int, default=300)
    wait.add_argument("--wait-timeout", type=int, default=900)

    return parser.parse_args(argv)


def validate_mode_args(args):
    if args.command not in {"generate", "edit"}:
        return
    if args.wait and not args.async_mode:
        raise api.ApiUsageError("--wait 只能与 --async 一起使用。")
    if args.async_mode and not args.wait and args.output:
        raise api.ApiUsageError("只创建异步任务时不要传 --output。")
    if (not args.async_mode or args.wait) and not args.output:
        raise api.ApiUsageError("当前模式需要 --output。")
    if not 1 <= args.count <= 10:
        raise api.ApiUsageError("--n 必须在 1 到 10 之间。")
    if not args.async_mode:
        return
    if args.idempotency_key is not None and len(args.idempotency_key) > 128:
        raise api.ApiUsageError("--idempotency-key 最多 128 个字符。")
    if (
        args.client_reference_id is not None
        and len(args.client_reference_id) > 191
    ):
        raise api.ApiUsageError("--client-reference-id 最多 191 个字符。")
    if (
        args.output_compression is not None
        and not 0 <= args.output_compression <= 100
    ):
        raise api.ApiUsageError("--output-compression 必须在 0 到 100 之间。")
    args.metadata = None
    if args.metadata_json is not None:
        args.metadata = json.loads(args.metadata_json)
        if not isinstance(args.metadata, dict):
            raise api.ApiUsageError("--metadata-json 必须是 JSON 对象。")


def parse_value(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def merge_extra_params(payload, args):
    if args.json_params:
        extra = json.loads(Path(args.json_params).expanduser().read_text(encoding="utf-8"))
        if not isinstance(extra, dict):
            raise ValueError("--json-params 必须包含 JSON 对象。")
        payload.update(extra)

    for item in args.param:
        if "=" not in item:
            raise ValueError("--param 必须使用 key=value 格式。")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError("--param 的 key 不能为空。")
        payload[key] = parse_value(value)


def build_generation_payload(args):
    payload = {
        "model": args.model or DEFAULT_MODEL,
        "prompt": args.prompt,
        "n": args.count,
        "size": args.size,
        "quality": args.quality,
    }
    if args.output_format:
        payload["output_format"] = args.output_format
    if args.background:
        payload["background"] = args.background
    merge_extra_params(payload, args)
    return payload


def _async_output(args):
    value = {
        "count": args.count,
        "size": args.size,
        "quality": args.quality,
    }
    if args.output_format:
        value["format"] = args.output_format
    if getattr(args, "output_compression", None) is not None:
        value["compression"] = args.output_compression
    if args.background:
        value["background"] = args.background
    return value


def _add_async_task_fields(payload, args):
    if getattr(args, "client_reference_id", None) is not None:
        payload["client_reference_id"] = args.client_reference_id
    if getattr(args, "metadata", None) is not None:
        payload["metadata"] = args.metadata
    return payload


def build_async_generation_payload(args):
    return _add_async_task_fields({
        "model": args.model or DEFAULT_MODEL,
        "operation": "generation",
        "input": {"prompt": args.prompt},
        "output": _async_output(args),
    }, args)


@dataclass(frozen=True)
class EditInputs:
    kind: str
    values: list
    mask: object | None


def classify_edit_inputs(image_values, base64_files, mask_value, async_mode):
    families = {"local": [], "url": [], "base64": []}

    def append_base64(encoded):
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise api.ApiUsageError("Base64 图片内容无效。") from exc
        if len(decoded) > api.MAX_FILE_BYTES:
            raise api.ApiUsageError("单个 Base64 图片不能超过 20 MiB。")
        api.detect_image_format(decoded[:12])
        families["base64"].append(encoded)

    for raw_value in image_values:
        parsed = urllib.parse.urlparse(raw_value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            families["url"].append(raw_value)
        elif raw_value.startswith("data:image/") and ";base64," in raw_value:
            append_base64(raw_value.split(";base64,", 1)[1])
        else:
            path = Path(raw_value).expanduser()
            try:
                exists = path.exists()
            except OSError as exc:
                raise api.ApiUsageError(f"无法识别图片输入：{raw_value}。") from exc
            if not exists:
                raise api.ApiUsageError(f"无法识别图片输入：{raw_value}。")
            families["local"].append(path)

    for raw_path in base64_files:
        path = Path(raw_path).expanduser()
        try:
            encoded = path.read_text(encoding="utf-8").strip()
            if encoded.startswith("data:image/") and ";base64," in encoded:
                encoded = encoded.split(";base64,", 1)[1]
            append_base64(encoded)
        except (binascii.Error, OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, api.ApiUsageError):
                raise
            raise api.ApiUsageError(f"无法读取或解析 Base64 图片文件：{path}。") from exc

    if sum(len(values) for values in families.values()) > api.MAX_IMAGES:
        raise api.ApiUsageError("参考图片最多 10 张。")
    used = [name for name, values in families.items() if values]
    if len(used) != 1:
        raise api.ApiUsageError("一次编辑只能使用本地文件、URL 或 Base64 中的一种。")
    kind = used[0]
    if async_mode and kind == "base64":
        raise api.ApiUsageError("异步编辑暂不支持 Base64；请改用同步编辑或先预上传。")

    mask = None
    if mask_value:
        parsed_mask = urllib.parse.urlparse(mask_value)
        if kind == "local":
            mask_path = Path(mask_value).expanduser()
            try:
                mask_exists = mask_path.exists()
            except OSError as exc:
                raise api.ApiUsageError("同步 Mask 必须是本地文件；异步 URL 编辑可使用 URL Mask。") from exc
            if mask_exists:
                mask = api.validate_local_images([mask_path])[0]
            else:
                raise api.ApiUsageError("同步 Mask 必须是本地文件；异步 URL 编辑可使用 URL Mask。")
        elif async_mode and kind == "url" and parsed_mask.scheme in {"http", "https"}:
            mask = mask_value
        else:
            raise api.ApiUsageError("同步 Mask 必须是本地文件；异步 URL 编辑可使用 URL Mask。")

    values = (
        api.validate_local_images(families[kind]) if kind == "local"
        else families[kind]
    )
    if kind == "local" and mask:
        total = sum(item.size for item in values) + mask.size
        if total > api.MAX_MULTIPART_BYTES:
            raise api.ApiUsageError("包括 Mask 在内的 multipart 文件总量不能超过 100 MiB。")
    return EditInputs(kind, values, mask)


def build_edit_payload(args):
    payload = {
        "model": args.model or DEFAULT_MODEL,
        "prompt": args.prompt,
        "size": args.size,
        "quality": args.quality,
    }
    if args.output_format:
        payload["output_format"] = args.output_format
    if args.background:
        payload["background"] = args.background
    merge_extra_params(payload, args)
    return payload


def build_async_url_edit_payload(args, inputs):
    input_value = {
        "prompt": args.prompt,
        "images": [{"url": value} for value in inputs.values],
    }
    if inputs.mask:
        input_value["mask"] = {"url": inputs.mask}
    return _add_async_task_fields({
        "model": args.model or DEFAULT_MODEL,
        "operation": "edit",
        "input": input_value,
        "output": _async_output(args),
    }, args)


def write_raw_diagnostic(path, body, *secrets):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = api.sanitize_diagnostic(body, *secrets) + "\n"
    part = Path(f"{path}.part")
    part.unlink(missing_ok=True)
    try:
        part.write_text(text, encoding="utf-8")
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def require_success(response, key_kind, model, *secrets):
    if response.status >= 400:
        detail = api.sanitize_diagnostic(response.body, *secrets)
        message = api.classify_http_error(response.status, response.headers, key_kind, model)
        raise api.ApiResponseError(f"{message}\n{detail}")
    return api.parse_json_response(response, secrets)


def output_rows(saved):
    return [
        {
            "path": str(item.path),
            "bytes": item.bytes_written,
            "format": item.format,
            "source_url": item.source_url,
        }
        for item in saved
    ]


def resolve_runtime(args, key_kind=MODEL_KEY):
    current = load_config()
    base_url = (args.base_url or current.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    key = (
        getattr(args, "api_key", None) if key_kind == MODEL_KEY
        else getattr(args, "resource_api_key", None)
    ) or get_api_key(key_kind)
    if (
        not key
        and key_kind == MODEL_KEY
        and sys.stdin.isatty()
        and sys.stderr.isatty()
    ):
        key = getpass.getpass("SuperToken API Key：").strip()
        if key:
            backend = save_api_key(
                key,
                getattr(args, "allow_plaintext_key_store", False),
                MODEL_KEY,
            )
            print(f"模型 API Token 已保存到：{backend}", file=sys.stderr)
    if not key:
        env_name = API_KEY_ENV if key_kind == MODEL_KEY else RESOURCE_API_KEY_ENV
        raise ConfigError(f"没有找到 Key。请设置 {env_name} 或运行 setup.py。")
    if not current or args.base_url:
        persistent_base = (
            DEFAULT_BASE_URL
            if base_url.endswith("/image-wrapper/v1")
            else base_url
        )
        save_config(build_config(
            base_url=persistent_base,
            model=current.get("model", DEFAULT_MODEL),
        ))
    return current, base_url, key


def run_models(args, base_url, api_key):
    response = api.request_json(
        "GET", api.endpoint_url(base_url, "/v1/models"), api_key, args.timeout
    )
    data = require_success(response, MODEL_KEY, None, api_key)
    model_ids = [
        item["id"] for item in data.get("data", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if not args.all:
        model_ids = [value for value in model_ids if "gpt-image-2" in value]
    print(json.dumps({"models": model_ids}, ensure_ascii=False, indent=2))


def run_sync_generate(args, base_url, api_key):
    payload = build_generation_payload(args)
    if payload["model"] == "gpt-image-2-count" and payload["n"] != 1:
        raise api.ApiUsageError("gpt-image-2-count 的 --n 只能为 1。")
    response = api.request_json(
        "POST", api.endpoint_url(base_url, "/v1/images/generations"),
        api_key, args.timeout, payload,
    )
    if args.raw_json:
        write_raw_diagnostic(Path(args.raw_json), response.body, api_key)
    data = require_success(response, MODEL_KEY, payload["model"], api_key)
    saved = api.save_image_items(data.get("data"), Path(args.output), args.timeout)
    print(json.dumps({
        "mode": "sync",
        "operation": "generation",
        "model": payload["model"],
        "outputs": output_rows(saved),
    }, ensure_ascii=False, indent=2))


def run_sync_edit(args, base_url, api_key, inputs):
    payload = build_edit_payload(args)
    endpoint = api.endpoint_url(base_url, "/v1/images/edits")
    if inputs.kind == "local":
        files = [
            api.MultipartFile("image", item.path, f"image/{item.format}")
            for item in inputs.values
        ]
        if inputs.mask:
            files.append(api.MultipartFile(
                "mask", inputs.mask.path, f"image/{inputs.mask.format}",
            ))
        response = api.request_multipart(
            "POST", endpoint, api_key, args.timeout, list(payload.items()), files,
        )
    else:
        payload["image"] = (
            [{"b64_json": encoded} for encoded in inputs.values]
            if inputs.kind == "base64" else inputs.values
        )
        response = api.request_json(
            "POST", endpoint, api_key, args.timeout, payload,
        )
    if args.raw_json:
        write_raw_diagnostic(Path(args.raw_json), response.body, api_key)
    data = require_success(response, MODEL_KEY, payload["model"], api_key)
    saved = api.save_image_items(data.get("data"), Path(args.output), args.timeout)
    print(json.dumps({
        "mode": "sync",
        "operation": "edit",
        "model": payload["model"],
        "outputs": output_rows(saved),
    }, ensure_ascii=False, indent=2))


class TaskFailed(api.ApiResponseError):
    def __init__(self, task, secrets=()):
        self.task = task
        error = task.get("error") or {}
        safe_code = api.sanitize_diagnostic(
            str(error.get("code", "unknown")).encode("utf-8"), *secrets
        )
        safe_message = api.sanitize_diagnostic(
            str(error.get("message", "未知错误")).encode("utf-8"), *secrets
        )
        super().__init__(
            f"异步任务 {task.get('id')} 失败：{safe_code} - {safe_message}；"
            f"retryable={error.get('retryable', False)}"
        )


def retry_delay(value, fallback=2):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(2, min(30, parsed))


def terminal_task_error(task_id, exc, resource_key):
    detail = api.sanitize_diagnostic(
        str(exc).encode("utf-8"), resource_key
    )
    error = api.ApiResponseError(f"任务 {task_id} 查询终止：{detail}")
    error.status = getattr(exc, "status", None)
    error.headers = getattr(exc, "headers", {})
    return error


def query_task(base_url, resource_key, task_id, timeout):
    if not re.fullmatch(r"task_[A-Za-z0-9_-]+", task_id):
        raise api.ApiUsageError("任务 ID 格式无效。")
    response = api.request_json(
        "GET",
        api.endpoint_url(base_url, f"/v1/image/tasks/{task_id}"),
        resource_key,
        timeout,
    )
    if response.status >= 400:
        message = api.classify_http_error(
            response.status, response.headers, RESOURCE_KEY
        )
        detail = api.sanitize_diagnostic(response.body, resource_key)
        error = api.ApiResponseError(f"任务 {task_id} 查询失败：{message}\n{detail}")
        error.status = response.status
        error.headers = response.headers
        raise error
    return api.parse_json_response(response, (resource_key,)), response.headers


def poll_task(
    base_url,
    resource_key,
    task_id,
    timeout,
    wait_timeout,
    initial_retry_after=2,
    sleep=time.sleep,
    monotonic=time.monotonic,
):
    started = monotonic()
    interval = retry_delay(initial_retry_after)
    consecutive_failures = 0
    while monotonic() - started < wait_timeout:
        try:
            task, headers = query_task(base_url, resource_key, task_id, timeout)
            consecutive_failures = 0
        except urllib.error.URLError as exc:
            consecutive_failures += 1
            if consecutive_failures > 3:
                raise terminal_task_error(task_id, exc, resource_key) from exc
            sleep(interval)
            continue
        except api.ApiResponseError as exc:
            if getattr(exc, "status", None) not in {429, 502, 503}:
                raise terminal_task_error(task_id, exc, resource_key) from exc
            consecutive_failures += 1
            if consecutive_failures > 3:
                raise terminal_task_error(task_id, exc, resource_key) from exc
            retry_after = api.header_value(
                getattr(exc, "headers", {}), "Retry-After", interval
            )
            sleep(retry_delay(retry_after, interval))
            continue
        status = task.get("status")
        if status == "succeeded":
            return task
        if status == "failed":
            raise TaskFailed(task, (resource_key,))
        if status not in {"queued", "in_progress"}:
            raise api.ApiResponseError(
                f"异步任务 {task_id} 返回了未知状态：{status}。"
            )
        interval = retry_delay(
            api.header_value(headers, "Retry-After", interval), interval
        )
        sleep(interval)
    raise api.ApiResponseError(f"等待任务 {task_id} 超过 {wait_timeout} 秒。")


def _multipart_async_edit(args, inputs):
    fields = [
        ("model", args.model or DEFAULT_MODEL),
        ("operation", "edit"),
        ("prompt", args.prompt),
        ("n", args.count),
        ("size", args.size),
        ("quality", args.quality),
    ]
    if args.output_format:
        fields.append(("output_format", args.output_format))
    if args.output_compression is not None:
        fields.append(("output_compression", args.output_compression))
    if args.background:
        fields.append(("background", args.background))
    if args.client_reference_id is not None:
        fields.append(("client_reference_id", args.client_reference_id))
    if args.metadata is not None:
        fields.append(("metadata", json.dumps(args.metadata, ensure_ascii=False)))
    files = [
        api.MultipartFile("image", item.path, f"image/{item.format}")
        for item in inputs.values
    ]
    if inputs.mask:
        files.append(api.MultipartFile(
            "mask", inputs.mask.path, f"image/{inputs.mask.format}"
        ))
    return fields, files


def create_async_task(args, base_url, api_key, inputs=None):
    idempotency_key = (
        uuid.uuid4().hex
        if args.idempotency_key is None else args.idempotency_key
    )
    headers = {"Idempotency-Key": idempotency_key}
    endpoint = api.endpoint_url(base_url, "/v1/image/tasks")
    model = args.model or DEFAULT_MODEL
    if model == "gpt-image-2-count" and args.count != 1:
        raise api.ApiUsageError("gpt-image-2-count 的 --n 只能为 1。")
    try:
        if inputs is not None and inputs.kind == "local":
            fields, files = _multipart_async_edit(args, inputs)
            response = api.request_multipart(
                "POST", endpoint, api_key, args.timeout, fields, files,
                headers=headers,
            )
        else:
            payload = (
                build_async_generation_payload(args)
                if inputs is None else build_async_url_edit_payload(args, inputs)
            )
            response = api.request_json(
                "POST", endpoint, api_key, args.timeout, payload, headers=headers,
            )
        task = require_success(response, MODEL_KEY, model, api_key)
        task_id = task.get("id")
        if not isinstance(task_id, str):
            raise api.ApiResponseError("异步创建响应中没有任务 ID。")
        return task, response.headers, idempotency_key
    except (urllib.error.URLError, api.ApiResponseError, OSError):
        print(f"Idempotency-Key：{idempotency_key}", file=sys.stderr)
        raise


def _async_result(task, output, timeout, operation=None, model=None):
    result = task.get("result") or {}
    saved = api.save_image_items(result.get("images"), Path(output), timeout)
    return {
        "mode": "async",
        "operation": task.get("operation", operation),
        "model": task.get("model", model),
        "task_id": task.get("id"),
        "status": task.get("status"),
        "progress": task.get("progress"),
        "outputs": output_rows(saved),
    }


def run_async_create(args, base_url, api_key, inputs=None, wait_runtime=None):
    task, headers, idempotency_key = create_async_task(
        args, base_url, api_key, inputs
    )
    operation = "generation" if inputs is None else "edit"
    model = args.model or DEFAULT_MODEL
    if not args.wait:
        print(json.dumps({
            "mode": "async",
            "operation": operation,
            "model": model,
            "task_id": task.get("id"),
            "status": task.get("status"),
            "progress": task.get("progress"),
            "idempotency_key": idempotency_key,
            "location": api.header_value(headers, "Location"),
            "retry_after": api.header_value(headers, "Retry-After"),
        }, ensure_ascii=False, indent=2))
        return
    resource_base_url, resource_key = wait_runtime
    completed = poll_task(
        resource_base_url,
        resource_key,
        task["id"],
        args.timeout,
        args.wait_timeout,
        api.header_value(headers, "Retry-After", 2),
    )
    result = _async_result(
        completed, args.output, args.timeout, operation, model
    )
    result["idempotency_key"] = idempotency_key
    print(json.dumps(result, ensure_ascii=False, indent=2))


def run_task_command(args, base_url, resource_key):
    task, _headers = query_task(
        base_url, resource_key, args.task_id, args.timeout
    )
    print(json.dumps(task, ensure_ascii=False, indent=2))


def run_wait_command(args, base_url, resource_key):
    task = poll_task(
        base_url,
        resource_key,
        args.task_id,
        args.timeout,
        args.wait_timeout,
    )
    print(json.dumps(
        _async_result(task, args.output, args.timeout),
        ensure_ascii=False,
        indent=2,
    ))


def main(argv=None):
    args = parse_args(argv)
    try:
        validate_mode_args(args)
        if args.command == "models":
            _current, base_url, api_key = resolve_runtime(args)
            run_models(args, base_url, api_key)
        elif args.command == "generate":
            if args.async_mode:
                wait_runtime = None
                if args.wait:
                    _current, resource_base_url, resource_key = resolve_runtime(
                        args, RESOURCE_KEY
                    )
                    wait_runtime = (resource_base_url, resource_key)
                _current, base_url, api_key = resolve_runtime(args)
                run_async_create(args, base_url, api_key, wait_runtime=wait_runtime)
                return 0
            _current, base_url, api_key = resolve_runtime(args)
            run_sync_generate(args, base_url, api_key)
        elif args.command == "edit":
            inputs = classify_edit_inputs(
                args.image, args.image_base64_file, args.mask, args.async_mode,
            )
            if args.async_mode:
                wait_runtime = None
                if args.wait:
                    _current, resource_base_url, resource_key = resolve_runtime(
                        args, RESOURCE_KEY
                    )
                    wait_runtime = (resource_base_url, resource_key)
                _current, base_url, api_key = resolve_runtime(args)
                run_async_create(
                    args, base_url, api_key, inputs, wait_runtime=wait_runtime
                )
                return 0
            _current, base_url, api_key = resolve_runtime(args)
            run_sync_edit(args, base_url, api_key, inputs)
        elif args.command == "task":
            _current, base_url, resource_key = resolve_runtime(args, RESOURCE_KEY)
            run_task_command(args, base_url, resource_key)
        elif args.command == "wait":
            _current, base_url, resource_key = resolve_runtime(args, RESOURCE_KEY)
            run_wait_command(args, base_url, resource_key)
    except (ConfigError, api.ApiUsageError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (urllib.error.URLError, api.ApiResponseError, binascii.Error, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
