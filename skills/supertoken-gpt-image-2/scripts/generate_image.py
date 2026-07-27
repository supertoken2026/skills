#!/usr/bin/env python3
import sys

from supertoken_image import main as image_main


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    return image_main(["generate", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
