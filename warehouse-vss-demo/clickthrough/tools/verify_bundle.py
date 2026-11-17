#!/usr/bin/env python3
"""Verify an assembled captured walkthrough without changing any files."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath


def load_builder():
    spec = importlib.util.spec_from_file_location("warehouse_bundle_builder", Path(__file__).with_name("build_demo.py"))
    if spec is None or spec.loader is None:
        raise ValueError("The build_demo.py companion is required for strict manifest validation")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def package_path(value: object) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(character in value for character in "\\:%?#")
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))
            or PurePosixPath(value).as_posix() != value):
        raise ValueError("Checksum paths must be normalized relative files")
    return value


def read_json(path: Path):
    def invalid_constant(value):
        raise ValueError(f"Invalid JSON numeric constant: {value}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)


def owned_file(root: Path, relative: str) -> Path:
    path = (root / package_path(relative)).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"File escapes the package directory or is not a regular file: {relative}")
    return path


def check_header(path: Path) -> str | None:
    suffix = path.suffix.lower()
    with path.open("rb") as source:
        header = source.read(1024)
    if suffix == ".mp4":
        if len(header) < 12 or header[4:8] != b"ftyp":
            raise ValueError(f"MP4 ftyp header missing: {path.name}")
        return "MP4/ISO BMFF ftyp"
    if suffix == ".pdf":
        if not header.startswith(b"%PDF-"):
            raise ValueError(f"PDF header missing: {path.name}")
        return "PDF"
    if suffix in {".jpg", ".jpeg"}:
        if not header.startswith(b"\xff\xd8\xff"):
            raise ValueError(f"JPEG header missing: {path.name}")
        return "JPEG"
    if suffix == ".png":
        if not header.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"PNG header missing: {path.name}")
        return "PNG"
    if suffix == ".webp":
        if header[:4] != b"RIFF" or header[8:12] != b"WEBP":
            raise ValueError(f"WebP header missing: {path.name}")
        return "WebP"
    if suffix == ".svg":
        if b"<svg" not in header:
            raise ValueError(f"SVG root missing: {path.name}")
        return "SVG"
    return None


class EntryPoints(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        for key, value in attributes:
            if key not in {"src", "href"} or value is None or value.startswith("#") or value == "./":
                continue
            if (tag == "a" and key == "href"
                    and value == "https://docs.nvidia.com/vss/3.2.1/warehouse-docs/Introduction.html"
                    and attrs.get("target") == "_blank"
                    and {"noopener", "noreferrer"} <= set(attrs.get("rel", "").split())):
                continue
            self.paths.add(package_path(value))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ValueError("HTTP verification does not follow redirects")


def local_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("HTTP verification accepts only an HTTP loopback base URL without credentials or query parameters")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid loopback port")
    prefix = parsed.path.strip("/")
    if prefix:
        package_path(prefix)
    return value.rstrip("/") + "/"


def verify_http(base_url: str, files: list[dict], directory: Path | None = None) -> dict:
    base = local_base_url(base_url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    checks = []
    for item in files:
        relative = item["path"]
        size = item["bytes"]
        url = base + urllib.parse.quote(relative, safe="/")
        with opener.open(urllib.request.Request(url, method="HEAD"), timeout=10) as response:
            if response.status != 200 or int(response.headers.get("Content-Length", "-1")) != size:
                raise ValueError(f"HTTP HEAD size/status mismatch: {relative}")
        if size:
            end = min(31, size - 1)
            request = urllib.request.Request(url, headers={"Range": f"bytes=0-{end}"})
            with opener.open(request, timeout=10) as response:
                payload = response.read(33)
                if (response.status != 206 or response.headers.get("Content-Range") != f"bytes 0-{end}/{size}"
                        or int(response.headers.get("Content-Length", "-1")) != end + 1 or len(payload) != end + 1):
                    raise ValueError(f"HTTP byte-range status/header/body mismatch: {relative}")
            if directory is not None:
                with owned_file(directory, relative).open("rb") as local:
                    if payload != local.read(end + 1):
                        raise ValueError(f"HTTP byte-range content differs from verified local file: {relative}")
            checks.append({"path": relative, "head": 200, "range": 206, "rangeBytes": end + 1,
                           "rangeContentMatchesLocal": directory is not None})
        else:
            checks.append({"path": relative, "head": 200, "range": "empty file"})
    return {"baseUrl": base, "headChecks": len(checks), "rangeChecks": sum(item["range"] == 206 for item in checks), "checks": checks}


def verify(directory: Path, base_url: str | None = None) -> dict:
    root = directory.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Walkthrough directory does not exist")
    summary = read_json(owned_file(root, "checksums.json"))
    if (not isinstance(summary, dict) or type(summary.get("format")) is not int or summary.get("format") != 1
            or summary.get("kind") != "captured-warehouse-walkthrough"
            or summary.get("completeAssetContract") is not True
            or not isinstance(summary.get("files"), list) or not summary["files"]):
        raise ValueError("Expected a complete captured-walkthrough checksums manifest")
    expected: dict[str, dict] = {}
    for item in summary["files"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid checksum record")
        relative = package_path(item.get("path"))
        if (relative == "checksums.json" or relative in expected
                or type(item.get("bytes")) is not int or item["bytes"] < 0
                or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise ValueError(f"Invalid or duplicate checksum record: {relative}")
        expected[relative] = item
    actual = set()
    for path in root.rglob("*"):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("Package contains a symlink outside its directory")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(expected) | {"checksums.json"}:
        raise ValueError(f"Package file list differs: missing={sorted(set(expected) - actual)}, extra={sorted(actual - set(expected) - {'checksums.json'})}")
    total = 0
    for relative, item in expected.items():
        source = owned_file(root, relative)
        if source.stat().st_size != item["bytes"]:
            raise ValueError(f"Checksum size mismatch: {relative}")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != item["sha256"]:
            raise ValueError(f"SHA-256 mismatch: {relative}")
        total += item["bytes"]
    manifest = read_json(owned_file(root, "demo.json"))
    assets = load_builder().validate_manifest(manifest)
    if not assets <= expected.keys():
        raise ValueError("Some scenario assets are absent from the checksums manifest")
    media = []
    for relative in sorted(assets):
        source = owned_file(root, relative)
        if source.stat().st_size == 0:
            raise ValueError(f"Empty scenario asset: {relative}")
        header = check_header(source)
        if header:
            media.append({"path": relative, "header": header})
    parser = EntryPoints()
    parser.feed(owned_file(root, "index.html").read_text(encoding="utf-8"))
    if not parser.paths <= expected.keys():
        raise ValueError("HTML references unpackaged entry points")
    historical_links = []
    for scenario in manifest["scenarios"]:
        for report in scenario.get("reports", []):
            if report.get("type") != "markdown":
                continue
            text = owned_file(root, report["src"]).read_text(encoding="utf-8")
            for url in re.findall(r"https?://[^\s)<>\]\"']+", text):
                historical_links.append(urllib.parse.urlsplit(url).hostname)
    result = {"status": "pass", "kind": summary["kind"], "completeAssetContract": True,
              "scenarios": len(manifest["scenarios"]), "filesVerified": len(expected),
              "assetsVerified": len(assets), "bytesVerified": total, "mediaHeaders": media,
              "htmlEntrypoints": "local packaged files",
              "historicalReportLinks": {"count": len(historical_links), "hosts": sorted(set(historical_links)), "followed": False}}
    if base_url is not None:
        result["http"] = verify_http(base_url, list(expected.values()), root)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--base-url", help="Optional existing HTTP loopback server; only HEAD and bounded byte-range GETs are issued")
    args = parser.parse_args()
    try:
        result = verify(args.directory, args.base_url)
    except (OSError, ValueError, TypeError, KeyError, urllib.error.URLError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, indent=2))
        raise SystemExit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
