#!/usr/bin/env python3
"""Build the procedure page: copy web/src into an output directory and stamp
every asset with the build id so the served page proves which build it came
from.  Stdlib only — runs identically locally and inside the verify
container.
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
ASSETS = ("index.html", "app.js", "style.css")


def build(out_dir: str, stamp: str) -> dict:
    if not os.path.isdir(SRC_DIR):
        raise SystemExit(f"missing page source directory: {SRC_DIR}")
    os.makedirs(out_dir, exist_ok=True)
    for name in ASSETS:
        src = os.path.join(SRC_DIR, name)
        if not os.path.isfile(src):
            raise SystemExit(f"missing page asset: {src}")
        with open(src, "r", encoding="utf-8") as handle:
            content = handle.read()
        content = content.replace("__BUILD_STAMP__", stamp)
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
            handle.write(content)
    manifest = {
        "stamp": stamp,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "assets": sorted(ASSETS),
    }
    with open(os.path.join(out_dir, "build.json"), "w",
              encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="build the procedure page")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--stamp", default=None,
                        help="build stamp (default: utc timestamp)")
    args = parser.parse_args()
    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    manifest = build(args.out, stamp)
    print(f"page built: stamp={manifest['stamp']} "
          f"assets={len(manifest['assets'])} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
