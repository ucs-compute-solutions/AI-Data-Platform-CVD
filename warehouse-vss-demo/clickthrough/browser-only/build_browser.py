#!/usr/bin/env python3
"""Build a browser-only captured demo; recipients need no server or runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent.parent
VERIFIER_PATH = APP_ROOT / "tools/verify_bundle.py"
RELEASE_NAME = "Cisco-AI-POD-Warehouse-Demo-Browser"
ENTRY_POINTS = ("app.js", "core.js", "style.css", "demo.json")
OFFLINE_CSP = ("default-src 'none'; script-src 'self' file:; style-src 'self' file:; "
               "img-src 'self' file:; media-src 'self' file:; connect-src 'none'; "
               "frame-src 'self' file:; object-src 'none'; base-uri 'none'; form-action 'none'")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def load_verifier():
    spec = importlib.util.spec_from_file_location("browser_demo_verifier", VERIFIER_PATH)
    if spec is None or spec.loader is None:
        raise ValueError("The strict verify_bundle.py companion is required")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def exact_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def offline_script(manifest: dict, reports: dict[str, str]) -> tuple[bytes, str]:
    payload = json.dumps({"format": 1, "manifest": manifest, "reports": reports},
                         ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for value, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                           ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        payload = payload.replace(value, escaped)
    payload_bytes = payload.encode("utf-8")
    return b"window.WarehouseOfflineCapture = " + payload_bytes + b";\n", hashlib.sha256(payload_bytes).hexdigest()


def browser_index(text: str) -> str:
    if "offline-data.js" in text:
        raise ValueError("Source HTML already includes offline data; use the original verified package")
    app_tag = re.compile(r'<script\s+src=["\']app\.js["\']\s+defer\s*></script>', re.IGNORECASE)
    if len(app_tag.findall(text)) != 1:
        raise ValueError("Expected exactly one classic deferred app.js entry point")
    text = app_tag.sub(lambda match: '<script src="offline-data.js" defer></script>\n  ' + match.group(0), text)
    home = 'class="brand" href="./"'
    if text.count(home) != 1:
        raise ValueError("Expected exactly one local brand home link")
    text = text.replace(home, 'class="brand" href="Start%20Demo.html"')
    csp = re.compile(r'(<meta\s+http-equiv="Content-Security-Policy"\s+content=")[^"]*(">)', re.IGNORECASE)
    if len(csp.findall(text)) != 1:
        raise ValueError("Expected exactly one source Content Security Policy")
    return csp.sub(lambda match: match.group(1) + OFFLINE_CSP + match.group(2), text)


def copy_file(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Expected a regular source file: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o644)
    if digest(source) != digest(target):
        raise ValueError(f"Copied file differs: {target.name}")


def records(directory: Path) -> list[dict]:
    result = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Output cannot contain symlinks")
        if path.is_file():
            result.append({"path": path.relative_to(directory).as_posix(),
                           "bytes": path.stat().st_size, "sha256": digest(path)})
    return result


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_checksums(directory: Path) -> None:
    content = "".join(f"{item['sha256']}  {item['path']}\n" for item in records(directory)
                      if item["path"] != "SHA256SUMS.txt")
    (directory / "SHA256SUMS.txt").write_text(content, encoding="utf-8")


def archive(directory: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            name = (Path(directory.name) / path.relative_to(directory)).as_posix()
            info = zipfile.ZipInfo.from_file(path, name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as source, bundle.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    with zipfile.ZipFile(output) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("Browser ZIP integrity check failed")


RECIPIENT_README = """CISCO AI POD FOR AI DATA PLATFORMS
Warehouse Video Intelligence with NVIDIA AI and VAST Data

1. Extract the complete ZIP into a folder on your computer.
2. Double-click Start Demo.html to open it in your browser.
   Or use your browser's Open File command and select Start Demo.html.
3. Choose a scenario and click the highlighted controls inside the screens.
   Play its evidence, read the PDF report and explore the captured questions.
4. Watch the demo opens the presenter movie. See the platform architecture
   offers the overview and Automatic / Back / Next animation modes.
5. Close the browser tab when finished. There is no server to stop.

Keep the complete extracted folder together. Do not open files inside the ZIP.
No installation, Python, executable, app, account, VPN, plugin, local server
or live deployment is required. Use a current desktop browser with JavaScript
and MP4 playback enabled. All demo content works offline. Optional vendor
reference links and historical links in original reports are external.

This is a captured walkthrough: it replays recorded results, not new inference
or live alerts. Original videos and reports are included unchanged.
SHA256SUMS.txt records every release file except itself; BUILD-REPORT.json
records source verification and asset preservation.
"""


def build(source: Path, output: Path) -> dict:
    source = source.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new parent directory")
    if output.resolve().is_relative_to(source):
        raise ValueError("Output must be outside the unchanged source package")
    verifier = load_verifier()
    verified = verifier.verify(source)
    manifest = verifier.read_json(verifier.owned_file(source, "demo.json"))
    assets = verifier.load_builder().validate_manifest(manifest)
    for name in ("core.js", "style.css"):
        if digest(APP_ROOT / name) != digest(verifier.owned_file(source, name)):
            raise ValueError(f"Shared {name} differs from the verified source; review before packaging")
    if "WarehouseOfflineCapture" not in exact_text(APP_ROOT / "app.js"):
        raise ValueError("Shared app.js must include the reviewed offline data loader")
    reports = {}
    for scenario in manifest["scenarios"]:
        for report in scenario.get("reports", []):
            if report["type"] == "pdf" and not report.get("pages"):
                raise ValueError("Every PDF must include its captured page images for browser-only playback")
            if report["type"] == "markdown":
                reports[report["src"]] = exact_text(verifier.owned_file(source, report["src"]))
    payload, payload_sha = offline_script(manifest, reports)
    html = browser_index(exact_text(verifier.owned_file(source, "index.html")))
    source_facts = {"checksumsSha256": digest(source / "checksums.json"),
                    "manifestSha256": digest(source / "demo.json"),
                    "filesVerified": verified["filesVerified"], "assets": len(assets),
                    "scenarios": verified["scenarios"]}
    movie = manifest.get("overview", {}).get("video")
    if movie:
        path = verifier.owned_file(source, movie)
        source_facts["presentationMovie"] = {"path": movie, "bytes": path.stat().st_size,
                                              "sha256": digest(path)}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        staging = Path(temporary)
        release = staging / RELEASE_NAME
        release.mkdir()
        for relative in sorted(assets):
            copy_file(verifier.owned_file(source, relative), release / relative)
        for name in ENTRY_POINTS:
            path = source / name if name == "demo.json" else APP_ROOT / name
            copy_file(path, release / name)
        for name in ("Start Demo.html", "index.html"):
            (release / name).write_bytes(html.encode("utf-8"))
        (release / "offline-data.js").write_bytes(payload)
        (release / "README.txt").write_text(RECIPIENT_README, encoding="utf-8")
        build_report = {"format": 1, "status": "pass", "kind": "browser-only-captured-demo",
                        "verificationScope": "source checksums, copied assets and offline serialization",
                        "fileBrowserAcceptance": "pending",
                        "source": source_facts, "offlinePayloadSha256": payload_sha,
                        "offlineDataFileSha256": digest(release / "offline-data.js"),
                        "markdownReports": len(reports), "recipientServerRequired": False,
                        "recipientPythonRequired": False, "recipientExecutableRequired": False,
                        "assets": [{"path": name, "sha256": digest(release / name),
                                    "bytes": (release / name).stat().st_size} for name in sorted(assets)],
                        "applicationFiles": [{"path": name, "sha256": digest(release / name)}
                                             for name in (*ENTRY_POINTS, "index.html", "Start Demo.html", "offline-data.js")]}
        write_json(release / "BUILD-REPORT.json", build_report)
        write_checksums(release)
        zip_path = staging / f"{RELEASE_NAME}.zip"
        archive(release, zip_path)
        result = {"format": 1, "status": "pass", "directory": RELEASE_NAME, "archive": zip_path.name,
                  "verificationScope": "source checksums, copied assets, offline serialization and ZIP CRC",
                  "fileBrowserAcceptance": "pending",
                  "archiveSha256": digest(zip_path), "archiveBytes": zip_path.stat().st_size,
                  "source": source_facts, "offlinePayloadSha256": payload_sha,
                  "files": len(records(release)), "recipientServerRequired": False,
                  "recipientPythonRequired": False, "recipientExecutableRequired": False}
        write_json(staging / "build-report.json", result)
        write_checksums(staging)
        output.mkdir(exist_ok=False)
        for path in staging.iterdir():
            path.rename(output / path.name)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Complete, checksum-verified capture package")
    parser.add_argument("--output", type=Path, required=True, help="New parent directory for folder, ZIP and build report")
    args = parser.parse_args()
    try:
        result = build(args.source, args.output)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(1, f"FAIL: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
