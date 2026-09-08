#!/usr/bin/env python3
"""Regenerate the bundled offline manuals; --check fails on CLI drift."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from side_dog.cli import build_parser
from side_dog.manual import manual_pages

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
directory = ROOT / "side_dog" / "man"
for name, content in manual_pages(build_parser()).items():
    path = directory / name
    if args.check:
        if not path.exists() or path.read_text() != content:
            raise SystemExit(
                f"Manual is stale: {name}; run python scripts/build_man_pages.py"
            )
    else:
        directory.mkdir(exist_ok=True)
        path.write_text(content)
