#!/usr/bin/env python3
"""Fetch checksum-locked, non-secret Warehouse image build artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import ssl
import sys
import urllib.request


SCRIPT_DIR = Path(__file__).resolve().parent
LOCK_FILE = (
    SCRIPT_DIR.parent / "derived-images.lock.json"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch and verify locked public Warehouse build artifacts."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Verify existing files without network access.",
    )
    args = parser.parse_args()

    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    ssl_context = ssl.create_default_context()

    for filename, metadata in lock["artifacts"].items():
        target = args.output / filename
        expected = metadata["sha256"]

        if target.exists():
            actual = sha256_file(target)
            if actual != expected:
                print(
                    f"FAIL: {target} has sha256:{actual}; expected sha256:{expected}",
                    file=sys.stderr,
                )
                return 1
            print(f"PASS: existing {filename} sha256:{actual}")
            continue

        if args.check_only:
            print(f"FAIL: missing locked artifact {target}", file=sys.stderr)
            return 1

        url = metadata["url"]
        if not url.startswith("https://"):
            print(f"FAIL: non-HTTPS artifact URL: {url}", file=sys.stderr)
            return 1

        temporary = args.output / f".{filename}.partial-{os.getpid()}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "aidp-warehouse-build/1.0"},
        )
        print(f"FETCH: {filename}")
        try:
            with urllib.request.urlopen(
                request, context=ssl_context, timeout=120
            ) as response, temporary.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)

            actual = sha256_file(temporary)
            if actual != expected:
                print(
                    f"FAIL: {filename} has sha256:{actual}; expected sha256:{expected}",
                    file=sys.stderr,
                )
                temporary.unlink(missing_ok=True)
                return 1
            temporary.replace(target)
            print(f"PASS: {filename} sha256:{actual}")
        finally:
            temporary.unlink(missing_ok=True)

    print(f"PASS: {len(lock['artifacts'])} locked artifacts verified in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
