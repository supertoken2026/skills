#!/usr/bin/env python3
import argparse
import sys

from supertoken_image import main as image_main


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="使用 SuperToken GPT Image 2 生成图片。"
    )
    parser.add_argument("--prompt", required=True, help="图片提示词。")
    parser.add_argument("--output", required=True, help="生成图片的保存路径。")
    parser.add_argument("--api-key", help="仅在本次运行中使用的 SuperToken API Key，不保存。")
    parser.add_argument("--base-url", help="覆盖 SuperToken 图片 API 基址。")
    parser.add_argument("--model", help="覆盖默认模型。")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", default="low")
    parser.add_argument(
        "--format", dest="output_format", choices=["png", "jpeg", "webp"]
    )
    parser.add_argument(
        "--background", choices=["transparent", "opaque", "auto"]
    )
    parser.add_argument(
        "--param", action="append", default=[], help="额外参数，格式为 key=value。"
    )
    parser.add_argument("--json-params", help="包含额外参数对象的 JSON 文件。")
    parser.add_argument("--raw-json", help="保存脱敏后的原始响应，便于排查问题。")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--allow-plaintext-key-store",
        action="store_true",
        help="系统安全存储不可用时，允许将 Key 写入权限为 0600 的本地文件。",
    )
    return parser.parse_args(argv)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parse_args(arguments)
    if "--timeout" not in arguments and not any(
        argument.startswith("--timeout=") for argument in arguments
    ):
        arguments.extend(["--timeout", "180"])
    return image_main(["generate", *arguments], legacy_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
