#!/usr/bin/env python3
"""Store SuperToken video credentials after hidden interactive entry."""

import argparse
import getpass
import sys

from supertoken_video_config import (
    ConfigError,
    MODEL_KEY_ENV,
    RESOURCE_KEY_ENV,
    save_key,
)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "invalid setup arguments\n")


def parse_args(argv=None):
    parser = _ArgumentParser(description="Configure SuperToken video credentials.")
    parser.add_argument("--with-resource-key", action="store_true")
    return parser.parse_args(argv)


def _prompt(label):
    value = getpass.getpass(f"{label}: ").strip()
    if not value:
        raise ConfigError(f"{label} is required")
    return value


def main(argv=None):
    try:
        args = parse_args(argv)
        save_key(_prompt("SuperToken model Token"), MODEL_KEY_ENV)
        if args.with_resource_key:
            save_key(_prompt("SuperToken resource Key"), RESOURCE_KEY_ENV)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("SuperToken video credentials saved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
