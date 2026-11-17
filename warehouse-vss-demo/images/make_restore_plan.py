#!/usr/bin/env python3
"""Prepare offline file-to-registry mappings or verify their published pins.

Never publishes images, starts containers or changes a cluster. Target checks
use registry-only `oc image info` after an operator runs the reviewed mirror.
"""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from verify_file_mirror import inventory_images


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def generate(inventory, prefix):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/[a-z0-9]+(?:[._/-][a-z0-9]+)*", prefix):
        raise ValueError("Use a credential-free registry hostname/namespace prefix, without scheme or trailing slash")
    rows = inventory_images(inventory)
    mappings, overrides = {}, {}
    for row in rows:
        repository = prefix + "/" + row["fileRepository"].rsplit("/", 1)[-1]
        pin = repository + "@" + row["platformDigest"]
        source = "file://" + row["fileRepository"] + "@" + row["platformDigest"]
        # Digest-specific tags avoid replacing an unrelated existing tag.
        target = repository + ":preserved-" + row["platformDigest"].split(":")[1][:16]
        if source in mappings and mappings[source] != target:
            raise ValueError("Ambiguous file-to-registry mapping")
        mappings[source] = target
        overrides[row["packagePin"]] = pin
    return {"format": 1, "operation": "image-mirror-plan-only", "platform": "linux/amd64",
            "inventorySha256": digest(inventory), "registryPrefix": prefix,
            "mappings": [source + "=" + mappings[source] for source in sorted(mappings)],
            "expectedImageOverrides": overrides, "registryWrite": False,
            "targetDigestsVerified": False}


def write_plan(inventory, prefix, output):
    if output.exists():
        raise ValueError("Choose a new plan directory; existing plans are retained")
    plan = generate(inventory, prefix)
    plan["planSha256"] = digest(plan)
    output.mkdir(parents=True, exist_ok=False)
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    (output / "mirror-inputs.txt").write_text("\n".join(plan["mappings"]) + "\n")
    return plan


def verify_plan(directory, registry_config=None, certificate_authority=None, runner=subprocess.run):
    path = directory / "plan.json"
    plan = json.loads(path.read_text())
    body = {key: value for key, value in plan.items() if key != "planSha256"}
    if (plan.get("planSha256") != digest(body) or plan.get("operation") != "image-mirror-plan-only"
            or plan.get("format") != 1 or plan.get("targetDigestsVerified") is not False):
        raise ValueError("Restore plan changed or is not a plan-only recipe")
    # Reconstruct every mapping from the pinned source inventory, not arbitrary
    # user-supplied command strings. This also detects omitted/extra images.
    inventory = json.loads((Path(__file__).parent / "required-images.json").read_text())
    expected = generate(inventory, plan["registryPrefix"])
    if any(plan.get(key) != value for key, value in expected.items()):
        raise ValueError("Plan differs from the shipped required image inventory")
    if (directory / "mirror-inputs.txt").read_text() != "\n".join(plan["mappings"]) + "\n":
        raise ValueError("Mirror mappings changed after review")
    output = directory / "verified-image-overrides.json"
    if output.exists():
        raise ValueError("Verified overrides already exist; retain the prior receipt")
    def check(pin):
        args = ["oc", "image", "info", pin, "--filter-by-os=linux/amd64", "-o", "json"]
        if registry_config: args.append("--registry-config=" + str(registry_config))
        if certificate_authority: args.append("--certificate-authority=" + str(certificate_authority))
        result = runner(args, capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise ValueError("A target image is not readable; registry output suppressed")
        value = json.loads(result.stdout)
        if (value.get("digest") != pin.rsplit("@", 1)[1]
                or value.get("config", {}).get("os") != "linux"
                or value.get("config", {}).get("architecture") != "amd64"):
            raise ValueError("A target image digest/platform differs from the preserved image")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(check, sorted(set(plan["expectedImageOverrides"].values()))))
    with output.open("x") as stream:
        json.dump(plan["expectedImageOverrides"], stream, indent=2)
        stream.write("\n")
    return {"status": "target-digests-verified", "sourcePins": len(plan["expectedImageOverrides"]),
            "platformImages": len(set(plan["expectedImageOverrides"].values())),
            "registryWrite": False, "clusterWrite": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--registry-prefix")
    mode.add_argument("--verify-plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--registry-config", type=Path)
    parser.add_argument("--certificate-authority", type=Path)
    args = parser.parse_args()
    try:
        if args.verify_plan:
            if args.output: raise ValueError("Verification writes only the receipt inside its reviewed plan")
            result = verify_plan(args.verify_plan, args.registry_config, args.certificate_authority)
        else:
            if not args.output: raise ValueError("Plan preparation requires a new --output directory")
            if args.registry_config or args.certificate_authority:
                raise ValueError("Offline plan preparation does not use credentials or registry connections")
            inventory = json.loads((Path(__file__).parent / "required-images.json").read_text())
            plan = write_plan(inventory, args.registry_prefix, args.output)
            result = {"status": "plan-only", "mappings": len(plan["mappings"]),
                      "sourcePins": len(plan["expectedImageOverrides"]), "registryWrite": False}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("FAIL: Image restore plan or target verification failed; no image was published by this tool", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
