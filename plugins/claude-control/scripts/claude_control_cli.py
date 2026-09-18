#!/usr/bin/env python3
"""Portable entrypoint; the package is resolved beside this script."""

import sys

if __name__ == "__main__":
    if sys.version_info < (3, 10):
        raise SystemExit("Claude Control requires Python 3.10 or newer.")
    from claude_control.cli import main

    raise SystemExit(main())
