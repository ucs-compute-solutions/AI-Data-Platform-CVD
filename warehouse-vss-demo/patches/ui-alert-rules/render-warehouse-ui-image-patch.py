#!/usr/bin/env python3
"""Print a guarded JSON Patch for the two Warehouse UI container images.

This tool does not contact OpenShift or write a file. Pipe its output to
``oc patch --patch-file=/dev/stdin`` only after reviewing the image digests.
"""

import argparse
import json
import re


IMAGE_RE = re.compile(r"^[a-zA-Z0-9._:-]+(?:/[a-zA-Z0-9._:-]+)+@sha256:[0-9a-f]{64}$")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-image", required=True)
    parser.add_argument("--to-image", required=True)
    args = parser.parse_args()
    for label, value in (("from", args.from_image), ("to", args.to_image)):
        if not IMAGE_RE.fullmatch(value):
            parser.error(f"{label} image must be a complete registry/repository@sha256 digest")
    if args.from_image == args.to_image:
        parser.error("the current and target images are identical")

    patch = [
        {"op": "test", "path": "/spec/template/spec/initContainers/0/name", "value": "stage-writable-public"},
        {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": "ui"},
        {"op": "test", "path": "/spec/template/spec/initContainers/0/image", "value": args.from_image},
        {"op": "test", "path": "/spec/template/spec/containers/0/image", "value": args.from_image},
        {"op": "replace", "path": "/spec/template/spec/initContainers/0/image", "value": args.to_image},
        {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": args.to_image},
    ]
    print(json.dumps(patch, separators=(",", ":")))


if __name__ == "__main__":
    main()
