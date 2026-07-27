#!/usr/bin/env python3
import sys

from supertoken_image import main as image_main


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--timeout" not in arguments and not any(
        argument.startswith("--timeout=") for argument in arguments
    ):
        arguments.extend(["--timeout", "180"])
    return image_main(["generate", *arguments], legacy_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
