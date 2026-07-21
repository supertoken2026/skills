#!/usr/bin/env python3
import argparse
import base64
import binascii
import getpass
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from supertoken_config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ConfigError,
    build_config,
    config_path,
    get_api_key,
    load_config,
    save_api_key,
    save_config,
)


class GenerationError(RuntimeError):
    pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="使用 SuperToken GPT Image 2 生成图片。")
    parser.add_argument("--prompt", required=True, help="图片提示词。")
    parser.add_argument("--output", required=True, help="生成图片的保存路径。")
    parser.add_argument("--api-key", help="仅在本次运行中使用的 SuperToken API Key，不保存。")
    parser.add_argument("--base-url", help="覆盖 SuperToken 图片 API 基址。")
    parser.add_argument("--model", help="覆盖默认模型。")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", default="low")
    parser.add_argument("--format", dest="output_format", choices=["png", "jpeg", "webp"])
    parser.add_argument("--background", choices=["transparent", "opaque", "auto"])
    parser.add_argument("--param", action="append", default=[], help="额外参数，格式为 key=value。")
    parser.add_argument("--json-params", help="包含额外参数对象的 JSON 文件。")
    parser.add_argument("--raw-json", help="保存脱敏后的原始响应，便于排查问题。")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--allow-plaintext-key-store",
        action="store_true",
        help="系统安全存储不可用时，允许将 Key 写入权限为 0600 的本地文件。",
    )
    return parser.parse_args(argv)


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


def resolve_model(args):
    return args.model or DEFAULT_MODEL


def build_payload(args):
    payload = {
        "model": resolve_model(args),
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


def request_json(url, api_key, payload, timeout):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def sanitize_diagnostic(body, api_key):
    text = body.decode("utf-8", errors="replace")
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-[REDACTED]", text)
    return text[:1000]


def classify_http_error(status, model, headers):
    if status == 401:
        return "SuperToken API Key 无效或已经失效。请检查 SUPERTOKEN_API_KEY。"
    if status == 403:
        return f"当前密钥没有 {model} 的访问权限。请在 SuperToken 控制台确认模型权限。"
    if status == 429:
        return "请求频率过高或当前额度不足。请稍后重试，并检查账户额度。"
    if status >= 500:
        request_id = next(
            (
                value
                for key, value in headers.items()
                if key.lower() in {"x-request-id", "request-id", "cf-ray"}
            ),
            None,
        )
        suffix = f" 请求 ID：{request_id}。" if request_id else ""
        return f"SuperToken 服务暂时异常（HTTP {status}）。{suffix}".strip()
    return f"图片生成失败（HTTP {status}）。请检查请求参数。"


def write_raw_diagnostics(path, body, api_key):
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sanitize_diagnostic(body, api_key) + "\n", encoding="utf-8")


def download_url(url, output_part_path, timeout):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise GenerationError("图片下载地址必须使用 HTTPS。")
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        output_part_path.write_bytes(response.read())


def save_response_image(item, output_path, timeout):
    output_path = output_path.expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = Path(f"{output_path}.part")
    part_path.unlink(missing_ok=True)
    try:
        if item.get("b64_json"):
            image_bytes = base64.b64decode(item["b64_json"], validate=True)
            part_path.write_bytes(image_bytes)
        elif item.get("url"):
            download_url(item["url"], part_path, timeout)
        else:
            raise GenerationError("响应中没有 data[0].url 或 data[0].b64_json。")
        if not part_path.exists() or part_path.stat().st_size == 0:
            raise GenerationError("生成结果为空，未写入目标文件。")
        part_path.replace(output_path)
        return output_path.stat().st_size
    except Exception:
        part_path.unlink(missing_ok=True)
        raise


def can_prompt():
    return sys.stdin.isatty() and sys.stderr.isatty()


def should_refresh_config(args, current):
    return (
        bool(args.base_url)
        or not current.get("base_url")
        or current.get("model") != DEFAULT_MODEL
    )


def first_run_setup(args, current):
    if not can_prompt():
        return current, None

    changed = should_refresh_config(args, current)
    if changed:
        current = build_config(base_url=args.base_url or current.get("base_url") or DEFAULT_BASE_URL)

    api_key = args.api_key or get_api_key()
    if not api_key:
        print("首次使用时，需要先配置 SuperToken GPT Image 2。", file=sys.stderr)
        api_key = getpass.getpass("SuperToken API Key：").strip()
        if not api_key:
            raise ConfigError("需要 SuperToken API Key。")
        backend = save_api_key(api_key, allow_plaintext=args.allow_plaintext_key_store)
        print(f"API Key 已保存到：{backend}", file=sys.stderr)

    if changed:
        save_config(current)
        print(f"配置已保存到：{config_path()}", file=sys.stderr)

    return current, api_key


def main(argv=None):
    args = parse_args(argv)
    current = load_config()
    prompted_api_key = None
    try:
        current, prompted_api_key = first_run_setup(args, current)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    api_key = args.api_key or prompted_api_key or get_api_key()
    if not api_key:
        print(
            "没有找到 SuperToken API Key。请设置 SUPERTOKEN_API_KEY，或运行 setup.py。",
            file=sys.stderr,
        )
        return 2

    base_url = (args.base_url or current.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    endpoint = f"{base_url}/images/generations"
    output_path = Path(args.output)
    try:
        payload = build_payload(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        status, headers, body = request_json(endpoint, api_key, payload, args.timeout)
    except urllib.error.URLError as exc:
        print(f"无法连接 SuperToken 图片服务：{exc.reason}", file=sys.stderr)
        return 1

    if args.raw_json:
        write_raw_diagnostics(Path(args.raw_json), body, api_key)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(f"SuperToken 返回了非 JSON 内容（HTTP {status}）。", file=sys.stderr)
        print(sanitize_diagnostic(body, api_key), file=sys.stderr)
        if status >= 400:
            print(classify_http_error(status, payload["model"], headers), file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print("SuperToken 返回的 JSON 不是对象。", file=sys.stderr)
        return 1

    if status >= 400:
        error_body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        print(sanitize_diagnostic(error_body, api_key), file=sys.stderr)
        print(classify_http_error(status, payload["model"], headers), file=sys.stderr)
        return 1

    items = data.get("data")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        print("SuperToken 响应中没有有效的 data[0]。", file=sys.stderr)
        return 1
    item = items[0]
    try:
        written = save_response_image(item, output_path, args.timeout)
    except (GenerationError, OSError, binascii.Error, urllib.error.URLError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "status": status,
                "base_url": base_url,
                "model": payload["model"],
                "output": str(output_path.expanduser()),
                "bytes": written,
                "content_type": headers.get("Content-Type") or headers.get("content-type"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
