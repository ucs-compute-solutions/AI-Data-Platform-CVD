#!/usr/bin/env python3
"""Review, stage, or locally build the selected Warehouse runtime images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile


WAREHOUSE = Path(__file__).resolve().parents[3]
PATCHES = WAREHOUSE / "patches"
PLATFORM = "linux/amd64"
SPECS = {
    "nvstreamer": {
        "commit": "ac7d872f1a5ba5e5641562c0aa9a4a37138da390",
        "tree": "d6a103d986f10fc23bc1e73b0ea2523f5e45d7f8",
        "subtree": "services/vios", "patchDir": "nvstreamer-sync",
        "dockerfile": "services/vios/docker/Dockerfile",
        "dockerfileSha256": "3e13bcc444e0c9e8384997cee848c5514533ec053d2303e876b7998bd9084323",
        "patches": {
            "0001-admit-late-readers-and-count-distinct-files.patch": "2738f2d7b8b2b295efaabc4a553a5d86c15ffd155c2d2a9f1870ff135bf6e85c",
            "0002-align-late-reader-seek-base-time.patch": "b250683ede3c0bc6062d4ae4b79ade5f8e276e449eab4c5ef46b8a20cdb9848e",
            "0003-count-eos-by-active-session.patch": "dd4b232c26e092ec5232afcfc342c8725f4d26314f79265d174c00dff94c3a07",
            "0004-match-eos-to-replay-seek-epoch.patch": "a876b1b02c4ebdd99478d08d0c1ce93b5372a40fb03ba8962aa8025c15ef0067",
        },
        "overlaySha256": "acf7dfa5b5ffafcd39ad63b854262dadf9dfd4130e39c6b35f88d1e68486687c",
    },
    "ui": {
        "commit": "7640d917047cf7b0fd3085eefb8282754b56bc94",
        "subtree": "services/ui", "patchDir": "ui-alert-rules",
        "dockerfile": "services/ui/Dockerfile",
        "patches": {
            "0001-preserve-vios-sensor-id.patch": "8f8aeb68d0cecec64c861d6875ec1bafd089a4ce4f6614215e99defdde33b6f8",
            "0002-isolate-alert-report-conversation.patch": "cb4742f911d35e87e1ecfee4892c899e1a41e5558a06921c1ebe74befd170baf",
        },
    },
}


def run(argv, *, cwd=None, binary=False):
    env = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(argv, cwd=cwd, env=env, check=True,
                            capture_output=True, text=not binary)
    return result.stdout if binary else result.stdout.strip()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_hash(path, expected):
    if not path.is_file() or path.is_symlink() or sha(path) != expected:
        raise ValueError(f"checksum mismatch or missing regular file: {path}")


def check_tag(tag):
    if ("@" in tag or not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag)
            or tag.rsplit(":", 1)[1].lower() == "latest"):
        raise ValueError("--tag requires an explicit fixed image tag; latest is prohibited")
    return tag


def parse_mappings(values):
    mappings = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--base-image requires UPSTREAM=REPOSITORY@sha256:DIGEST")
        source, target = value.split("=", 1)
        if (not source or source in mappings or not re.fullmatch(
                r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", target)):
            raise ValueError("base-image mappings must be unique immutable SHA256 references")
        mappings[source] = target
    return mappings


def pin_dockerfile(text, mappings):
    """Resolve supported default ARGs and pin every external FROM image."""
    defaults = {"TARGETARCH": "amd64", "TARGETPLATFORM": PLATFORM,
                "BUILD_TYPE": "prod", "MODULE": "nvstreamer"}
    aliases, used, external = set(), set(), []
    output = []

    def expand(value):
        def replace(match):
            key = match.group(1) or match.group(2)
            if key not in defaults:
                raise ValueError(f"unresolved Dockerfile FROM variable: {key}")
            return defaults[key]
        for _ in range(10):
            result = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace, value)
            if result == value:
                return result
            value = result
        raise ValueError("recursive Dockerfile FROM variables")

    for line in text.splitlines(keepends=True):
        arg = re.match(r"\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*?))?\s*$", line)
        if arg and arg.group(2) is not None:
            defaults[arg.group(1)] = arg.group(2).strip().strip('"\'')
        match = re.match(r"(\s*FROM\s+(?:--[^\s]+\s+)*)(\S+)(.*)", line, re.I)
        if not match:
            output.append(line)
            continue
        original = match.group(2)
        resolved = expand(original)
        suffix = match.group(3)
        if resolved.lower() not in aliases and resolved != "scratch":
            external.append(resolved)
            if resolved in mappings:
                pinned = mappings[resolved]
                used.add(resolved)
            elif re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", resolved):
                pinned = resolved
            else:
                raise ValueError(f"missing immutable --base-image mapping for {resolved}")
            line = match.group(1) + pinned + suffix + ("\n" if line.endswith("\n") else "")
        alias = re.search(r"\s+AS\s+([\w.-]+)", suffix, re.I)
        if alias:
            aliases.add(alias.group(1).lower())
        output.append(line)
    if not external:
        raise ValueError("Dockerfile has no external base image")
    if set(mappings) != used:
        raise ValueError("unused base-image mappings: " + ", ".join(sorted(set(mappings) - used)))
    return "".join(output), external


def validate(source, component):
    source = source.resolve(strict=True)
    spec = SPECS[component]
    if run(["git", "-C", str(source), "rev-parse", "--show-toplevel"]) != str(source):
        raise ValueError("--source must identify the checkout root")
    if run(["git", "-C", str(source), "rev-parse", "HEAD"]) != spec["commit"]:
        raise ValueError("source checkout does not match the pinned commit")
    if run(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"]):
        raise ValueError("source checkout is dirty; use a separate clean exact checkout")
    if "tree" in spec and run(["git", "-C", str(source), "rev-parse",
                               "HEAD:" + spec["subtree"]]) != spec["tree"]:
        raise ValueError("NvStreamer services/vios tree differs from the selected source")
    patch_dir = PATCHES / spec["patchDir"]
    for filename, expected in spec["patches"].items():
        check_hash(patch_dir / filename, expected)
    if component == "nvstreamer":
        check_hash(source / spec["dockerfile"], spec["dockerfileSha256"])
        check_hash(patch_dir / "Dockerfile.runtime-packages", spec["overlaySha256"])
    else:
        lock = json.loads((patch_dir / "report-conversation-source-lock.json").read_text())
        if lock["source_commit"] != spec["commit"] or lock["patch_sha256"] != spec["patches"][lock["patch"]]:
            raise ValueError("UI source lock identity differs")
        for item in lock["build_inputs"]:
            check_hash(source / item["path"], item["sha256"])
        for item in lock["files"]:
            path = source / item["path"]
            if item["original_sha256"]:
                check_hash(path, item["original_sha256"])
            elif path.exists():
                raise ValueError(f"unexpected new UI source file: {item['path']}")
    return spec


def stage_source(source, stage, spec):
    """Export tracked files and verify materialized Git LFS objects, never source writes."""
    archive = run(["git", "-C", str(source), "archive", "HEAD", spec["subtree"]], binary=True)
    import io
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle.getmembers():
            destination = stage / member.name
            if not destination.resolve().is_relative_to(stage.resolve()):
                raise ValueError("archive contains an escaping path")
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as stream, destination.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                destination.chmod(member.mode & 0o777)
            elif member.issym():
                if not (destination.parent / member.linkname).resolve().is_relative_to(stage.resolve()):
                    raise ValueError("archive contains an escaping symlink")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(member.linkname)
            else:
                raise ValueError("unsupported archive member")
    for path in (stage / spec["subtree"]).rglob("*"):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024:
            continue
        text = path.read_bytes()
        if text.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            match = re.fullmatch(rb"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n", text)
            materialized = source / path.relative_to(stage)
            if (not match or materialized.is_symlink() or not materialized.is_file()
                    or materialized.stat().st_size != int(match.group(2))
                    or sha(materialized) != match.group(1).decode()):
                raise ValueError(f"Git LFS object missing or mismatched; run git lfs pull: {path.relative_to(stage)}")
            shutil.copyfile(materialized, path)
    patch_dir = PATCHES / spec["patchDir"]
    # Use a temporary empty repository so a parent checkout cannot
    # alter git apply's path prefix. No Git configuration or network is used.
    run(["git", "init", "--quiet", str(stage)])
    for filename in spec["patches"]:
        run(["git", "-C", str(stage), "apply", "--check", str(patch_dir / filename)])
        run(["git", "-C", str(stage), "apply", str(patch_dir / filename)])
    run(["git", "-C", str(stage), "diff", "--check"])
    if spec["patchDir"] == "ui-alert-rules":
        lock = json.loads((patch_dir / "report-conversation-source-lock.json").read_text())
        for item in lock["files"]:
            check_hash(stage / item["path"], item["patched_sha256"])


def commands(component, stage, tag):
    spec = SPECS[component]
    common = ["docker", "buildx", "build", "--platform", PLATFORM, "--load",
              "--label", "org.opencontainers.image.revision=" + spec["commit"]]
    if component == "ui":
        return [common + ["--build-arg", "BUILD_TYPE=prod", "--file", str(stage / spec["dockerfile"]),
                          "--tag", tag, str(stage)]]
    intermediate = tag + "-source"
    return [common + ["--target", "runtime-amd64", "--build-arg", "MODULE=nvstreamer",
                      "--file", str(stage / spec["dockerfile"]), "--tag", intermediate,
                      str(stage / "services/vios")],
            common + ["--build-arg", "BASE_IMAGE=" + intermediate,
                      "--file", str(stage / "runtime-overlay/Dockerfile"),
                      "--tag", tag, str(stage / "runtime-overlay")]]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=sorted(SPECS), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--base-image", action="append", default=[], metavar="UPSTREAM=IMMUTABLE_IMAGE")
    parser.add_argument("--output", type=Path, help="new staging directory; required for prepare/execute")
    parser.add_argument("--prepare", action="store_true", help="stage verified source without building")
    parser.add_argument("--execute", action="store_true", help="stage and build local images; never push")
    args = parser.parse_args(argv)
    try:
        check_tag(args.tag)
        if args.component == "nvstreamer":
            check_tag(args.tag + "-source")
        mappings = parse_mappings(args.base_image)
        source = args.source.resolve(strict=True)
        spec = validate(source, args.component)
        docker_text = (source / spec["dockerfile"]).read_text()
        pinned, bases = pin_dockerfile(docker_text, mappings)
        receipt = {"component": args.component, "sourceCommit": spec["commit"],
                   "platform": PLATFORM, "tag": args.tag, "patches": spec["patches"],
                   "upstreamBaseImages": bases, "baseImageOverrides": mappings,
                   "status": "plan-only", "published": False,
                   "commands": commands(args.component, Path("<NEW_STAGING_DIRECTORY>"), args.tag),
                   "dependencyResolution": "NVIDIA Dockerfile retains network package installers; base/source pins do not pin APT/NodeSource/PIP responses"}
        if not args.prepare and not args.execute:
            print(json.dumps(receipt, indent=2))
            print("PLAN ONLY: source and patch hashes checked; no directory, image, or cluster changes.")
            print("Add --output NEW_DIRECTORY --prepare to apply/check patches privately; --execute builds locally.")
            return 0
        if args.output is None:
            raise ValueError("--output NEW_DIRECTORY is required")
        stage = args.output.resolve()
        if stage == source or stage.is_relative_to(source) or source.is_relative_to(stage):
            raise ValueError("staging directory must not overlap the input checkout")
        if stage.exists() or stage.is_symlink():
            raise ValueError("staging directory already exists; choose a new directory")
        stage.mkdir(parents=True, mode=0o700)
        stage_source(source, stage, spec)
        (stage / spec["dockerfile"]).write_text(pinned)
        if args.component == "nvstreamer":
            overlay = stage / "runtime-overlay"
            overlay.mkdir()
            shutil.copyfile(PATCHES / "nvstreamer-sync/Dockerfile.runtime-packages", overlay / "Dockerfile")
        recipe = commands(args.component, stage, args.tag)
        receipt.update(status="prepared", commands=recipe,
                       stagedDockerfileSha256=sha(stage / spec["dockerfile"]))
        if args.execute:
            for command in recipe:
                print("BUILD:", shlex.join(command), flush=True)
                subprocess.run(command, check=True)
            image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", args.tag])
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise ValueError("Docker returned no immutable local image ID")
            receipt.update(status="local-build-pass", localImageID=image_id)
        (stage / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))
        print("No image was pushed. Record the registry digest after a separately reviewed push before deployment.")
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("FAIL:", error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
