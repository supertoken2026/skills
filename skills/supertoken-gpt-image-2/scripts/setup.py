#!/usr/bin/env python3
import argparse
import getpass
import sys

from supertoken_config import (
    DEFAULT_BASE_URL,
    ConfigError,
    MODEL_KEY,
    RESOURCE_KEY,
    build_config,
    config_path,
    save_api_key,
    save_config,
)


class ChineseArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "参数错误：请使用 --help 查看可用参数。\n")


def parse_args(argv=None):
    parser = ChineseArgumentParser(description="配置 SuperToken GPT Image 2。")
    parser.add_argument("--api-key", help="SuperToken API Key；未提供时会安全提示输入。")
    parser.add_argument("--resource-api-key", help="SuperToken Resource API Key。")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="SuperToken 兼容 OpenAI 的图片 API 基址。",
    )
    parser.add_argument(
        "--allow-plaintext-key-store",
        action="store_true",
        help="系统安全存储不可用时，允许将 Key 写入权限为 0600 的本地文件。",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    api_key = args.api_key or getpass.getpass("SuperToken API Key：").strip()
    if not api_key:
        print("需要 SuperToken API Key。", file=sys.stderr)
        return 2

    try:
        value = build_config(base_url=args.base_url)
        backend = save_api_key(api_key, args.allow_plaintext_key_store, MODEL_KEY)
        resource_backend = None
        if args.resource_api_key:
            resource_backend = save_api_key(
                args.resource_api_key,
                args.allow_plaintext_key_store,
                RESOURCE_KEY,
            )
        save_config(value)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"配置已保存到：{config_path()}")
    print(f"模型 API Key 已保存到：{backend}")
    if resource_backend:
        print(f"资源 API Key 已保存到：{resource_backend}")
    print("默认模型：gpt-image-2-count")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
