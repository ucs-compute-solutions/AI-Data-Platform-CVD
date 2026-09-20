#!/usr/bin/env python3
"""Run dependency-free safety and consistency checks for the CVD companion."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_ROOT = ROOT / "workloads/vast-native-vss/source-assets/locks"
VAST_PATCH_ROOT = ROOT / "workloads/vast-native-vss/source-assets/patches"
VAST_SOURCE_LOCK = ROOT / "workloads/vast-native-vss/source-lock.yaml"
NVIDIA_SOURCE_LOCK = ROOT / "workloads/nvidia-vss-3.2.1-search/source-lock.yaml"
DOCUMENT_RAG_SOURCE_LOCK = ROOT / "workloads/document-rag/source-lock.yaml"

TEXT_SUFFIXES = {
    "",
    ".csv",
    ".env",
    ".gitignore",
    ".in",
    ".j2",
    ".json",
    ".lock",
    ".md",
    ".patch",
    ".py",
    ".sh",
    ".tpl",
    ".txt",
    ".yaml",
    ".yml",
}

FORBIDDEN = {
    "local macOS home path": re.compile("/" + "Users/"),
    "deployment-client home path": re.compile("/home/" + "vastclient"),
    "internal environment name": re.compile(r"\b" + "AA" + r"10\b", re.I),
    "internal environment prefix": re.compile("aa" + "10-", re.I),
    "authoring assistant name": re.compile("Chat" + "GPT", re.I),
    "authoring tool name": re.compile("Cod" + "ex", re.I),
    "private key": re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "NGC API key": re.compile(r"nvapi-[A-Za-z0-9_-]{20,}"),
}

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.name in {"LICENSE", "SHA256SUMS"} or path.suffix in TEXT_SUFFIXES:
            files.append(path)
    return sorted(files)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"expected UTF-8 text: {path.relative_to(ROOT)}") from exc


def check_forbidden(files: list[Path], errors: list[str]) -> None:
    for path in files:
        text = read_text(path)
        for label, pattern in FORBIDDEN.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                errors.append(f"{path.relative_to(ROOT)}:{line}: {label}")


def check_markdown_links(errors: list[str]) -> None:
    for path in sorted(ROOT.rglob("*.md")):
        text = read_text(path)
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().split()[0].strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if "<" in target or ">" in target:
                continue
            relative = target.split("#", 1)[0]
            if relative and not (path.parent / relative).resolve().exists():
                errors.append(
                    f"{path.relative_to(ROOT)}: missing Markdown target {target}"
                )


def check_python(errors: list[str]) -> None:
    for path in sorted(ROOT.rglob("*.py")):
        try:
            ast.parse(read_text(path), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")


def check_json(errors: list[str]) -> None:
    for path in sorted(ROOT.rglob("*.json")):
        try:
            json.loads(read_text(path))
        except json.JSONDecodeError as exc:
            errors.append(f"{path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_lock_hashes(errors: list[str]) -> None:
    checksum_file = LOCK_ROOT / "SHA256SUMS"
    for line_number, line in enumerate(read_text(checksum_file).splitlines(), 1):
        if not line.strip():
            continue
        try:
            expected, relative = line.split(maxsplit=1)
        except ValueError:
            errors.append(f"{checksum_file.relative_to(ROOT)}:{line_number}: invalid entry")
            continue
        target = LOCK_ROOT / relative.strip()
        if not target.is_file():
            errors.append(f"{target.relative_to(ROOT)}: checksum target missing")
        elif sha256(target) != expected:
            errors.append(f"{target.relative_to(ROOT)}: checksum mismatch")

    expected_patch_hashes = set(
        re.findall(r"sha256:\s*([0-9a-f]{64})", read_text(VAST_SOURCE_LOCK))
    )
    actual_patch_hashes = {sha256(path) for path in VAST_PATCH_ROOT.glob("*.patch")}
    if actual_patch_hashes != expected_patch_hashes:
        errors.append("VAST patch hashes do not match source-lock.yaml")

    nvidia_lock = read_text(NVIDIA_SOURCE_LOCK)
    path_match = re.search(r"^openshiftPatchPath:\s*(\S+)\s*$", nvidia_lock, re.M)
    hash_match = re.search(
        r"^openshiftPatchSha256:\s*([0-9a-f]{64})\s*$", nvidia_lock, re.M
    )
    if not path_match or not hash_match:
        errors.append("NVIDIA source-lock.yaml is missing the OpenShift patch lock")
    else:
        nvidia_patch = ROOT / path_match.group(1)
        if not nvidia_patch.is_file():
            errors.append(f"{nvidia_patch.relative_to(ROOT)}: patch target missing")
        elif sha256(nvidia_patch) != hash_match.group(1):
            errors.append(f"{nvidia_patch.relative_to(ROOT)}: checksum mismatch")

    document_lock = read_text(DOCUMENT_RAG_SOURCE_LOCK)
    for path_key, hash_key in (
        ("validateLocalNimsPath", "validateLocalNimsSha256"),
        ("validateDocumentRagPath", "validateDocumentRagSha256"),
        ("probeEmbeddingPath", "probeEmbeddingSha256"),
    ):
        path_value = re.search(
            rf'^{path_key}:\s*["\']?([^"\'\s]+)["\']?\s*$', document_lock, re.M
        )
        hash_value = re.search(
            rf'^{hash_key}:\s*["\']?([0-9a-f]{{64}})["\']?\s*$',
            document_lock,
            re.M,
        )
        if not path_value or not hash_value:
            errors.append(f"Document RAG source lock is missing {path_key}/{hash_key}")
            continue
        target = ROOT / path_value.group(1)
        if not target.is_file():
            errors.append(f"{target.relative_to(ROOT)}: source-lock target missing")
        elif sha256(target) != hash_value.group(1):
            errors.append(f"{target.relative_to(ROOT)}: checksum mismatch")


def main() -> int:
    errors: list[str] = []
    files = text_files()
    check_forbidden(files, errors)
    check_markdown_links(errors)
    check_python(errors)
    check_json(errors)
    check_lock_hashes(errors)

    if errors:
        print("Repository validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    markdown_count = sum(1 for _ in ROOT.rglob("*.md"))
    print(
        "PASS: repository safety, links, Python, JSON, and source-lock checks "
        f"({len(files)} text files; {markdown_count} Markdown files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
