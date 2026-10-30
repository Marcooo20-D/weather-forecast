#!/usr/bin/env python3
"""Read-only compatibility command for the actual public release gate."""
import argparse
import json
import tempfile
from pathlib import Path

from site_tools.public_site import build_site


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("outputs"))
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="langit-check-") as directory:
        stats = build_site(args.root, Path(directory) / "public")
    print(json.dumps({"status": "valid", **stats}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
