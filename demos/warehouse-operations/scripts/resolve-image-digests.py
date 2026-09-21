#!/usr/bin/env python3
"""Resolve the Warehouse source-lock image tags to immutable registry digests.

The script delegates registry access to ``oc image info``. It can use the
operator's existing registry configuration or pass a temporary registry-auth
file directly to ``oc``. It never reads or prints registry credentials. Output
contains only image references, platform, and digests.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-lock",
        default=(
            "demos/warehouse-operations/openshift/"
            "nvidia-warehouse/source-lock.yaml"
        ),
        help="Warehouse source-lock YAML containing the images list",
    )
    parser.add_argument(
        "--platform",
        default="linux/amd64",
        help="Platform passed to oc image info (default: linux/amd64)",
    )
    parser.add_argument(
        "--output",
        help="Optional JSON output path; stdout is always emitted",
    )
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help=(
            "Resolve only this source-locked image; repeat for more than one "
            "image. The value must exist under images: in the source lock."
        ),
    )
    parser.add_argument(
        "--registry-config",
        help=(
            "Path to a temporary Docker/containers registry-auth file passed "
            "to oc image info. The file is never read or printed by this script."
        ),
    )
    return parser.parse_args()


def read_images(path: pathlib.Path) -> list[str]:
    images: list[str] = []
    in_images = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if raw_line == "images:":
            in_images = True
            continue
        if in_images and raw_line and not raw_line.startswith((" ", "\t")):
            break
        if not in_images:
            continue
        match = re.match(r"^\s+-\s+(\S+)\s*$", raw_line)
        if match:
            images.append(match.group(1))
    if not images:
        raise ValueError(f"no images found under images: in {path}")
    return images


def normalize_reference(reference: str) -> str:
    first = reference.split("/", 1)[0]
    if "/" not in reference:
        return f"docker.io/library/{reference}"
    if "." not in first and ":" not in first and first != "localhost":
        return f"docker.io/{reference}"
    return reference


def resolve(
    reference: str,
    platform: str,
    registry_config: str | None = None,
) -> dict[str, Any]:
    normalized = normalize_reference(reference)
    command = [
        "oc",
        "image",
        "info",
        normalized,
        f"--filter-by-os={platform}",
    ]
    if registry_config:
        command.append(f"--registry-config={registry_config}")
    command.extend(["-o", "json"])
    process = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        message = process.stderr.strip().splitlines()
        return {
            "source": reference,
            "normalized": normalized,
            "platform": platform,
            "status": "error",
            "error": message[-1] if message else "oc image info failed",
        }
    metadata = json.loads(process.stdout)
    digest = metadata.get("listDigest") or metadata.get("digest")
    platform_digest = metadata.get("digest")
    if not digest:
        return {
            "source": reference,
            "normalized": normalized,
            "platform": platform,
            "status": "error",
            "error": "registry metadata did not include a digest",
        }
    return {
        "source": reference,
        "normalized": normalized,
        "platform": platform,
        "status": "resolved",
        "digest": digest,
        "platformDigest": platform_digest,
        "manifestListDigest": metadata.get("listDigest"),
    }


def main() -> int:
    args = parse_args()
    source_lock = pathlib.Path(args.source_lock)
    locked_images = read_images(source_lock)
    requested_images = args.image or locked_images
    unknown_images = sorted(set(requested_images) - set(locked_images))
    if unknown_images:
        raise SystemExit(
            "requested image is not in the source lock: " + ", ".join(unknown_images)
        )
    if args.registry_config and not pathlib.Path(args.registry_config).is_file():
        raise SystemExit(f"registry config is not a file: {args.registry_config}")
    results = [
        resolve(image, args.platform, args.registry_config)
        for image in requested_images
    ]
    payload = {
        "sourceLock": str(source_lock),
        "platform": args.platform,
        "resolved": sum(item["status"] == "resolved" for item in results),
        "failed": sum(item["status"] != "resolved" for item in results),
        "images": results,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(rendered)
    if args.output:
        pathlib.Path(args.output).write_text(rendered, encoding="utf-8")
    return 0 if payload["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
