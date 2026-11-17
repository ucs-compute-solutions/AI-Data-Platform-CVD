#!/usr/bin/env python3
"""Check this source folder offline; never contacts or changes a cluster."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
MANIFEST = "package-manifest.json"
PYTHON_SUITES = (
    "tests", "deployment/tests", "patches/agent-report/tests",
    "patches/nvstreamer-sync/tests", "clickthrough/browser-only", "images/tests",
)


def load_json(path):
    def constant(_):
        raise ValueError("non-finite JSON number")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=constant)


def source_path(root, name):
    if not isinstance(name, str) or not name:
        raise ValueError("invalid source-relative manifest path")
    relative = PurePosixPath(name)
    if ("\\" in name or relative.as_posix() != name
            or relative.is_absolute() or any(x in {".", ".."} for x in name.split("/"))):
        raise ValueError("invalid source-relative manifest path")
    result = root.joinpath(*relative.parts)
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("source path escapes the package")
    if any(item.is_symlink() for item in (result, *result.parents) if item.is_relative_to(root)):
        raise ValueError("symlinks are not source payload")
    return result


def file_entry(path):
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "executable": bool(path.stat().st_mode & 0o111)}


def check_manifest(root):
    manifest = load_json(root / MANIFEST)
    if (manifest.get("format") != 1 or manifest.get("published") is not False
            or manifest.get("freshInstallAccepted") is not False
            or manifest.get("portableHourLifecycleAccepted") is not False
            or not isinstance(manifest.get("files"), dict) or not manifest["files"]):
        raise ValueError("invalid candidate status or file inventory")
    for name, expected in manifest["files"].items():
        path = source_path(root, name)
        if name == MANIFEST or not path.is_file() or file_entry(path) != expected:
            raise ValueError("missing or changed source: " + name)
    return manifest


def check_links(root, files):
    checked = 0
    for name in files:
        if not name.endswith(".md"):
            continue
        path = source_path(root, name)
        # Public docs use ordinary Markdown links, including directory links.
        text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
        for match in re.finditer(r"\[[^\]\n]*\]\((<[^>]+>|[^\s)]+)(?:\s+\"[^\"]*\")?\)", text):
            target = match.group(1).strip("<>")
            parts = urlsplit(target)
            if parts.scheme in {"http", "https", "mailto"}:
                continue
            if parts.scheme or parts.netloc or target.startswith("/"):
                raise ValueError("non-portable link in " + name)
            local = unquote(parts.path)
            resolved = (path.parent / local).resolve()
            if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
                raise ValueError("missing or escaping local link in " + name + ": " + target)
            checked += 1
    return checked


def check_content(root, files):
    counts = {"python": 0, "json": 0, "bash": 0}
    attribution = re.compile(r"\b(?:chat" + r"gpt|co" + r"dex)\b", re.I)
    credential = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|nvapi-[A-Za-z0-9_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
    bash = shutil.which("bash")
    for name in files:
        path = source_path(root, name)
        text = path.read_text(encoding="utf-8")
        if attribution.search(text):
            raise ValueError("authoring-environment reference in " + name)
        # These two paths belong to NVIDIA container images, not a workstation.
        portable_text = text.replace("/home/vst/", "").replace("/home/appuser/", "")
        if re.search(r"/(?:Users|home)/[A-Za-z0-9_.-]+/", portable_text):
            raise ValueError("workstation-specific path in " + name)
        if credential.search(text):
            raise ValueError("possible credential in " + name + "; value suppressed")
        if name.endswith(".py"):
            compile(text, name, "exec")
            counts["python"] += 1
        elif name.endswith(".json"):
            load_json(path)
            counts["json"] += 1
        elif name.endswith(".sh"):
            if not bash:
                raise ValueError("bash is required to validate shell syntax")
            subprocess.run([bash, "-n", str(path)], check=True)
            counts["bash"] += 1
    return counts


def run_tests(root, node):
    for directory in PYTHON_SUITES:
        subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s",
                        directory, "-p", "test_*.py"], cwd=root, check=True)
    runtime = node or shutil.which("node")
    if not runtime:
        raise ValueError("Node.js is required for --tests; use --node PATH if needed")
    javascript = sorted((root / "clickthrough").glob("*.js"))
    for path in javascript:
        subprocess.run([str(runtime), "--check", str(path)], check=True)
    subprocess.run([str(runtime), "--test", *map(str, sorted(
        (root / "clickthrough/tests").glob("*.cjs")))], cwd=root, check=True)
    # The selected EOS/seek model also has a dependency-free C++ regression.
    compiler = shutil.which("c++") or shutil.which("g++")
    if not compiler:
        raise ValueError("A C++17 compiler is required for --tests")
    with tempfile.TemporaryDirectory(prefix="warehouse-model-test-") as directory:
        binary = Path(directory) / "replay-model"
        subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        str(root / "patches/nvstreamer-sync/tests/test_replay_barrier_model.cpp"),
                        "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", action="store_true", help="Also run Python, JavaScript and C++ offline regressions")
    parser.add_argument("--node", help="Node.js executable when not on PATH")
    args = parser.parse_args()
    try:
        manifest = check_manifest(ROOT)
        links = check_links(ROOT, manifest["files"])
        syntax = check_content(ROOT, manifest["files"])
        print(f"PASS: {len(manifest['files'])} source hashes, {links} local links; syntax {syntax}", flush=True)
        if args.tests:
            run_tests(ROOT, args.node)
        print("PASS: offline source checks; no cluster changes. Fresh-site/hour acceptance remains pending.")
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("FAIL: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
