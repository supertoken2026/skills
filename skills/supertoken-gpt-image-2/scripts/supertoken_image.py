#!/usr/bin/env python3
import argparse
import base64
import binascii
import getpass
import json
import math
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
    validate_api_key,
    normalize_api_base,
)


MAX_BASE64_FILE_BYTES = (
    4 * ((api.MAX_FILE_BYTES + 2) // 3)
    + len(b"data:image/jpeg;base64,")
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
    parser.add_argument("--wait-timeout", type=int)
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
    wait_timeout = getattr(args, "wait_timeout", None)
    if wait_timeout is not None and wait_timeout <= 0:
        raise api.ApiUsageError("--wait-timeout 必须大于 0。")
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
        unsupported = [
            name for name, value in (
                ("--resource-api-key", args.resource_api_key),
                ("--wait-timeout", args.wait_timeout),
                ("--idempotency-key", args.idempotency_key),
                ("--output-compression", args.output_compression),
                ("--client-reference-id", args.client_reference_id),
                ("--metadata-json", args.metadata_json),
            ) if value is not None
        ]
        if unsupported:
            raise api.ApiUsageError(
                f"同步模式不支持参数：{', '.join(unsupported)}。"
            )
        return
    unsupported = []
    if args.param:
        unsupported.append("--param")
    if args.json_params is not None:
        unsupported.append("--json-params")
    if args.raw_json is not None:
        unsupported.append("--raw-json")
    if unsupported:
        raise api.ApiUsageError(
            f"异步模式不支持参数：{', '.join(unsupported)}。"
        )
    if not args.wait:
        unused_wait_options = [
            name for name, value in (
                ("--resource-api-key", args.resource_api_key),
                ("--wait-timeout", args.wait_timeout),
            ) if value is not None
        ]
        if unused_wait_options:
            raise api.ApiUsageError(
                "仅创建异步任务时不支持参数："
                f"{', '.join(unused_wait_options)}。"
            )
    elif args.wait_timeout is None:
        args.wait_timeout = 900
    if (
        args.idempotency_key is not None
        and not re.fullmatch(r"[!-~]{1,128}", args.idempotency_key)
    ):
        raise api.ApiUsageError(
            "Idempotency-Key 必须包含 1 到 128 个 ASCII 可见非空白字符。"
        )
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
        path = Path(args.json_params).expanduser()
        try:
            extra = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise api.ApiUsageError(
                f"无法读取或解析 --json-params 文件：{path}。"
            ) from exc
        if not isinstance(extra, dict):
            raise api.ApiUsageError("--json-params 必须包含 JSON 对象。")
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
            with path.open("rb") as stream:
                raw_encoded = stream.read(MAX_BASE64_FILE_BYTES + 1)
            if len(raw_encoded) > MAX_BASE64_FILE_BYTES:
                raise api.ApiUsageError(
                    "Base64 图片文件不能超过 20 MiB 图片的编码上限。"
                )
            encoded = raw_encoded.decode("utf-8").strip()
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
        elif (
            async_mode
            and kind == "url"
            and parsed_mask.scheme in {"http", "https"}
            and parsed_mask.netloc
        ):
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
    if not 200 <= response.status < 300:
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
        }
        for item in saved
    ]


def resolve_runtime(args, key_kind=MODEL_KEY):
    current = load_config()
    base_url = normalize_api_base(
        args.base_url or current.get("base_url") or DEFAULT_BASE_URL
    )
    explicit_key = (
        getattr(args, "api_key", None) if key_kind == MODEL_KEY
        else getattr(args, "resource_api_key", None)
    )
    key = (
        validate_api_key(explicit_key, key_kind)
        if explicit_key else get_api_key(key_kind)
    )
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
    items = data.get("data")
    if not isinstance(items, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or not item["id"]
        for item in items
    ):
        raise api.ApiResponseError("SuperToken 返回的模型列表格式无效。")
    model_ids = [item["id"] for item in items]
    if not args.all:
        model_ids = [value for value in model_ids if "gpt-image-2" in value]
    model_ids = [api.sanitize_server_text(value, api_key) for value in model_ids]
    print(json.dumps({"models": model_ids}, ensure_ascii=False, indent=2))


def run_sync_generate(args, base_url, api_key, legacy_output=False):
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
    items = data.get("data")
    if legacy_output:
        api.validate_image_item_count(items, payload["n"])
        saved = api.save_image_items(
            items[:1],
            Path(args.output),
            args.timeout,
            preserve_requested_path=True,
            expected_count=1,
        )
        content_type = api.header_value(response.headers, "Content-Type")
        if content_type is not None:
            content_type = api.sanitize_server_text(content_type, api_key)
        print(json.dumps({
            "status": response.status,
            "base_url": base_url,
            "model": payload["model"],
            "output": str(Path(args.output).expanduser()),
            "bytes": saved[0].bytes_written,
            "content_type": content_type,
        }, ensure_ascii=False, indent=2))
        return
    saved = api.save_image_items(
        items, Path(args.output), args.timeout, expected_count=payload["n"]
    )
    print(json.dumps({
        "mode": "sync",
        "operation": "generation",
        "model": payload["model"],
        "outputs": output_rows(saved),
    }, ensure_ascii=False, indent=2))


def run_sync_edit(args, base_url, api_key, inputs):
    payload = build_edit_payload(args)
    if payload["model"] == "gpt-image-2-count" and payload["n"] != 1:
        raise api.ApiUsageError("gpt-image-2-count 的 --n 只能为 1。")
    endpoint = api.endpoint_url(base_url, "/v1/images/edits")
    if inputs.kind == "local":
        files = [
            api.MultipartFile(
                "image", item.path, f"image/{item.format}", item.data,
            )
            for item in inputs.values
        ]
        if inputs.mask:
            files.append(api.MultipartFile(
                "mask", inputs.mask.path, f"image/{inputs.mask.format}",
                inputs.mask.data,
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
    saved = api.save_image_items(
        data.get("data"), Path(args.output), args.timeout,
        expected_count=payload["n"],
    )
    print(json.dumps({
        "mode": "sync",
        "operation": "edit",
        "model": payload["model"],
        "outputs": output_rows(saved),
    }, ensure_ascii=False, indent=2))


class TaskFailed(api.ApiResponseError):
    def __init__(self, task_id, task, secrets=()):
        self.task = task
        try:
            error = task_error_summary(task, f"异步任务 {task_id}", secrets)
        except api.ApiResponseError as exc:
            super().__init__(str(exc))
            return
        super().__init__(
            f"异步任务 {task_id} 失败：{error['code']} - {error['message']}；"
            f"retryable={error['retryable']}"
        )


TASK_STATUSES = {"queued", "in_progress", "succeeded", "failed"}
TASK_ERROR_FIELDS = {"code", "message", "retryable"}


def valid_task_id(value, secrets=()):
    return (
        isinstance(value, str)
        and re.fullmatch(r"task_[A-Za-z0-9_-]+", value) is not None
        and api.sanitize_server_text(value, *secrets) == value
    )


def task_error_summary(task, context, secrets=()):
    error = task.get("error")
    if (
        not isinstance(error, dict)
        or set(error) != TASK_ERROR_FIELDS
        or not isinstance(error.get("code"), str)
        or not isinstance(error.get("message"), str)
        or not isinstance(error.get("retryable"), bool)
    ):
        raise api.ApiResponseError(f"{context} 返回的 error 格式无效。")
    return {
        "code": api.sanitize_diagnostic(error["code"].encode("utf-8"), *secrets),
        "message": api.sanitize_diagnostic(
            error["message"].encode("utf-8"), *secrets
        ),
        "retryable": error["retryable"],
    }


def validate_task_response(task, context, expected_id=None, secrets=()):
    task_id = task.get("id")
    if (
        not valid_task_id(task_id, secrets)
        or (expected_id is not None and task_id != expected_id)
    ):
        if expected_id is not None:
            raise api.ApiResponseError(f"任务 {expected_id} 的响应身份无效。")
        raise api.ApiResponseError(f"{context}中的任务 ID 无效。")
    status = task.get("status")
    if status not in TASK_STATUSES:
        raise api.ApiResponseError(f"{context}中的任务状态无效。")
    if "progress" in task:
        progress = task["progress"]
        if (
            isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not math.isfinite(progress)
            or not 0 <= progress <= 100
        ):
            raise api.ApiResponseError(f"{context}中的任务进度无效。")
    error = task.get("error")
    if status == "failed" or error is not None:
        task_error_summary(task, context, secrets)
    result = task.get("result")
    if result is not None and not isinstance(result, dict):
        raise api.ApiResponseError(f"{context}中的 result 格式无效。")
    return task


def task_summary(task_id, task, secrets=()):
    summary = {"task_id": task_id, "status": task["status"]}
    if "progress" in task:
        summary["progress"] = task["progress"]
    if task["status"] == "failed":
        summary["error"] = task_error_summary(
            task, f"异步任务 {task_id}", secrets
        )
    return summary


def numeric_retry_after(headers):
    raw = api.header_value(headers, "Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value) if value.is_integer() else value


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


def query_task(
    base_url,
    resource_key,
    task_id,
    timeout,
    *,
    deadline=None,
    deadline_message=None,
    monotonic=None,
):
    if not valid_task_id(task_id, (resource_key,)):
        raise api.ApiUsageError("任务 ID 格式无效。")
    deadline_options = {}
    if deadline is not None:
        deadline_options = {
            "deadline": deadline,
            "deadline_message": deadline_message,
            "monotonic": monotonic,
        }
    response = api.request_json(
        "GET",
        api.endpoint_url(base_url, f"/v1/image/tasks/{task_id}"),
        resource_key,
        timeout,
        **deadline_options,
    )
    if not 200 <= response.status < 300:
        message = api.classify_http_error(
            response.status, response.headers, RESOURCE_KEY
        )
        detail = api.sanitize_diagnostic(response.body, resource_key)
        error = api.ApiResponseError(f"任务 {task_id} 查询失败：{message}\n{detail}")
        error.status = response.status
        error.headers = response.headers
        raise error
    task = api.parse_json_response(response, (resource_key,))
    validate_task_response(
        task,
        f"任务 {task_id} 的响应",
        expected_id=task_id,
        secrets=(resource_key,),
    )
    return task, response.headers


def poll_task(
    base_url,
    resource_key,
    task_id,
    timeout,
    wait_timeout,
    initial_retry_after=2,
    sleep=time.sleep,
    monotonic=time.monotonic,
    *,
    deadline=None,
):
    if wait_timeout <= 0:
        raise api.ApiUsageError("--wait-timeout 必须大于 0。")
    if deadline is None:
        deadline = monotonic() + wait_timeout
    deadline_message = f"等待任务 {task_id} 超过 {wait_timeout} 秒。"
    interval = retry_delay(initial_retry_after)
    consecutive_failures = 0
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            task, headers = query_task(
                base_url,
                resource_key,
                task_id,
                min(timeout, remaining),
                deadline=deadline,
                deadline_message=deadline_message,
                monotonic=monotonic,
            )
            consecutive_failures = 0
        except urllib.error.URLError as exc:
            consecutive_failures += 1
            if consecutive_failures > 3:
                raise terminal_task_error(task_id, exc, resource_key) from exc
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(interval, remaining))
            continue
        except api.ApiResponseError as exc:
            if getattr(exc, "deadline_exceeded", False):
                raise
            if getattr(exc, "status", None) not in {429, 502, 503}:
                raise terminal_task_error(task_id, exc, resource_key) from exc
            consecutive_failures += 1
            if consecutive_failures > 3:
                raise terminal_task_error(task_id, exc, resource_key) from exc
            retry_after = api.header_value(
                getattr(exc, "headers", {}), "Retry-After", interval
            )
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(retry_delay(retry_after, interval), remaining))
            continue
        status = task.get("status")
        if status == "succeeded":
            return task
        if status == "failed":
            raise TaskFailed(task_id, task, (resource_key,))
        if status not in {"queued", "in_progress"}:
            safe_status = api.sanitize_diagnostic(
                str(status).encode("utf-8"), resource_key
            )
            raise api.ApiResponseError(
                f"异步任务 {task_id} 返回了未知状态：{safe_status}。"
            )
        interval = retry_delay(
            api.header_value(headers, "Retry-After", interval), interval
        )
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
    raise api.ApiResponseError(deadline_message)


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
        api.MultipartFile(
            "image", item.path, f"image/{item.format}", item.data,
        )
        for item in inputs.values
    ]
    if inputs.mask:
        files.append(api.MultipartFile(
            "mask", inputs.mask.path, f"image/{inputs.mask.format}",
            inputs.mask.data,
        ))
    return fields, files


def create_async_task(args, base_url, api_key, inputs=None, output_secrets=()):
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
        validate_task_response(
            task, "异步创建响应", secrets=(api_key, *output_secrets)
        )
        return task, response.headers, idempotency_key
    except (urllib.error.URLError, api.ApiResponseError, OSError):
        safe_key = api.sanitize_server_text(
            idempotency_key, api_key, *output_secrets
        )
        print(f"Idempotency-Key：{safe_key}", file=sys.stderr)
        raise


def _async_result(
    task_id, task, output, timeout, operation=None, model=None,
    expected_count=None, deadline=None, wait_timeout=None,
    monotonic=None,
):
    result = task.get("result")
    if not isinstance(result, dict):
        raise api.ApiResponseError(
            f"异步任务 {task_id} 返回的 result 不是对象。"
        )
    try:
        saved = api.save_image_items(
            result.get("images"), Path(output), timeout,
            expected_count=expected_count,
            deadline=deadline,
            deadline_message=(
                f"等待任务 {task_id} 超过 {wait_timeout} 秒。"
                if wait_timeout is not None else None
            ),
            monotonic=monotonic,
        )
    except api.ApiResponseError as exc:
        if getattr(exc, "deadline_exceeded", False):
            raise
        raise api.ApiResponseError(
            f"异步任务 {task_id} 的结果无效：{exc}"
        ) from exc
    value = {
        "task_id": task_id,
        "status": task.get("status"),
        "outputs": output_rows(saved),
    }
    if "progress" in task:
        value["progress"] = task["progress"]
    if operation is not None and model is not None:
        value.update({
            "mode": "async", "operation": operation, "model": model,
        })
    return value


def run_async_create(args, base_url, api_key, inputs=None, wait_runtime=None):
    output_secrets = (() if wait_runtime is None else (wait_runtime[1],))
    task, headers, idempotency_key = create_async_task(
        args, base_url, api_key, inputs, output_secrets
    )
    operation = "generation" if inputs is None else "edit"
    model = args.model or DEFAULT_MODEL
    if not args.wait:
        value = {
            "mode": "async",
            "operation": operation,
            "model": model,
            "task_id": task.get("id"),
            "status": task.get("status"),
            "idempotency_key": api.sanitize_server_text(
                idempotency_key, api_key, *output_secrets
            ),
        }
        if "progress" in task:
            value["progress"] = task["progress"]
        location = api.header_value(headers, "Location")
        if location is not None:
            value["location"] = api.sanitize_url(location, api_key)
        retry_after = numeric_retry_after(headers)
        if retry_after is not None:
            value["retry_after"] = retry_after
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    resource_base_url, resource_key = wait_runtime
    deadline = time.monotonic() + args.wait_timeout
    completed = poll_task(
        resource_base_url,
        resource_key,
        task["id"],
        args.timeout,
        args.wait_timeout,
        api.header_value(headers, "Retry-After", 2),
        deadline=deadline,
    )
    result = _async_result(
        task["id"], completed, args.output, args.timeout, operation, model,
        expected_count=args.count,
        deadline=deadline,
        wait_timeout=args.wait_timeout,
        monotonic=time.monotonic,
    )
    result["idempotency_key"] = api.sanitize_server_text(
        idempotency_key, api_key, resource_key
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def run_task_command(args, base_url, resource_key):
    try:
        task, _headers = query_task(
            base_url, resource_key, args.task_id, args.timeout
        )
    except (urllib.error.URLError, api.ApiResponseError) as exc:
        raise terminal_task_error(args.task_id, exc, resource_key) from exc
    print(json.dumps(
        task_summary(args.task_id, task, (resource_key,)),
        ensure_ascii=False,
        indent=2,
    ))


def run_wait_command(args, base_url, resource_key):
    deadline = time.monotonic() + args.wait_timeout
    task = poll_task(
        base_url,
        resource_key,
        args.task_id,
        args.timeout,
        args.wait_timeout,
        deadline=deadline,
    )
    print(json.dumps(
        _async_result(
            args.task_id,
            task,
            args.output,
            args.timeout,
            deadline=deadline,
            wait_timeout=args.wait_timeout,
            monotonic=time.monotonic,
        ),
        ensure_ascii=False,
        indent=2,
    ))


def main(argv=None, legacy_output=False):
    args = parse_args(argv)
    active_secrets = []
    try:
        validate_mode_args(args)
        if args.command == "models":
            _current, base_url, api_key = resolve_runtime(args)
            active_secrets.append(api_key)
            run_models(args, base_url, api_key)
        elif args.command == "generate":
            if args.async_mode:
                wait_runtime = None
                if args.wait:
                    _current, resource_base_url, resource_key = resolve_runtime(
                        args, RESOURCE_KEY
                    )
                    active_secrets.append(resource_key)
                    wait_runtime = (resource_base_url, resource_key)
                _current, base_url, api_key = resolve_runtime(args)
                active_secrets.append(api_key)
                run_async_create(args, base_url, api_key, wait_runtime=wait_runtime)
                return 0
            _current, base_url, api_key = resolve_runtime(args)
            active_secrets.append(api_key)
            run_sync_generate(args, base_url, api_key, legacy_output)
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
                    active_secrets.append(resource_key)
                    wait_runtime = (resource_base_url, resource_key)
                _current, base_url, api_key = resolve_runtime(args)
                active_secrets.append(api_key)
                run_async_create(
                    args, base_url, api_key, inputs, wait_runtime=wait_runtime
                )
                return 0
            _current, base_url, api_key = resolve_runtime(args)
            active_secrets.append(api_key)
            run_sync_edit(args, base_url, api_key, inputs)
        elif args.command == "task":
            _current, base_url, resource_key = resolve_runtime(args, RESOURCE_KEY)
            active_secrets.append(resource_key)
            run_task_command(args, base_url, resource_key)
        elif args.command == "wait":
            _current, base_url, resource_key = resolve_runtime(args, RESOURCE_KEY)
            active_secrets.append(resource_key)
            run_wait_command(args, base_url, resource_key)
    except (ConfigError, api.ApiUsageError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (urllib.error.URLError, api.ApiResponseError, binascii.Error, OSError) as exc:
        print(
            api.sanitize_diagnostic(
                str(exc).encode("utf-8", errors="replace"), *active_secrets
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
