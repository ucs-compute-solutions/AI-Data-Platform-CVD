#!/usr/bin/env python3
"""Build an offline, same-origin captured walkthrough from reviewed assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


APP_ROOT = Path(__file__).resolve().parent.parent
APP_FILES = ("index.html", "style.css", "app.js", "core.js")
STEPS = ("createRule", "activeRule", "alert", "play", "report", "followup")
SCREEN_ONLY_STEPS = STEPS[:4] + ('generateReport',) + STEPS[4:]
PATH_KEYS = {"src", "image", "poster", "architecture", "architectureAnimation", "architecturePoster", "coverImage", "video"}
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".mp4", ".pdf", ".md", ".json", ".vtt"}


def asset_path(value: str) -> str:
    """Return a normalized safe relative URL, never a remote or parent path."""
    if not isinstance(value, str) or not value:
        raise ValueError("Asset paths must be nonempty strings")
    parsed = urlsplit(value)
    decoded = unquote(parsed.path)
    path = PurePosixPath(decoded)
    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            or decoded.startswith(("/", "\\")) or "\\" in decoded
            or any(part in ("..", ".") for part in decoded.split("/"))
            or ":" in decoded or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or path.suffix.lower() not in MEDIA_SUFFIXES
            or not decoded.startswith("assets/")):
        raise ValueError(f"Unsafe or unsupported asset path: {value!r}")
    if path.as_posix() != value:
        raise ValueError(f"Asset paths must be literal normalized URLs: {value!r}")
    return value


def list_assets(value: object) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PATH_KEYS and isinstance(item, str) and item:
                result.add(asset_path(item))
            elif isinstance(item, (dict, list)):
                result.update(list_assets(item))
    elif isinstance(value, list):
        for item in value:
            result.update(list_assets(item))
    return result


def validate_manifest(data: object, *, allow_incomplete: bool = False) -> set[str]:
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data.get("schemaVersion") != 1:
        raise ValueError("Expected schemaVersion: 1")
    if not data.get("title") or not data.get("captureDate"):
        raise ValueError("title and captureDate are required")
    if data.get('interactionMode') not in (None, 'screen-only'):
        raise ValueError('Unsupported interaction mode')
    expected_steps = SCREEN_ONLY_STEPS if data.get('interactionMode') == 'screen-only' else STEPS
    if data.get('questionFlow') not in (None, 'sequential'):
        raise ValueError('Unsupported question flow')
    sequential = data.get('questionFlow') == 'sequential'
    if sequential and data.get('interactionMode') != 'screen-only':
        raise ValueError('Sequential questions require screen-only interaction')

    def bounds(value):
        if not isinstance(value, dict):
            raise ValueError('Image bounds must be an object')
        coords = [value.get(key) for key in ('x', 'y', 'width', 'height')]
        if any(isinstance(n, bool) or not isinstance(n,(int,float)) or not math.isfinite(n) for n in coords):
            raise ValueError('Image bounds must contain finite numbers')
        x,y,w,h = coords
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > 1.00001 or y+h > 1.00001:
            raise ValueError('Image bounds must fit the captured screen')
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 2:
        raise ValueError("This reviewed walkthrough requires exactly two scenarios")
    ids: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ValueError("Each scenario must be an object")
        ident = scenario.get("id")
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError("Each scenario needs a unique ID")
        ids.add(ident)
        if not scenario.get("title") or not scenario.get("camera"):
            raise ValueError(f"Missing scenario title/camera: {ident}")
        if not isinstance(scenario.get("rule"), dict) or not scenario["rule"].get("prompt"):
            raise ValueError(f"Missing captured rule prompt: {ident}")
        steps = scenario.get("steps", [])
        if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
            raise ValueError(f"Steps must be an array of objects: {ident}")
        reviewed = list(expected_steps)
        if sequential:
            count = len(scenario.get('followups', []))
            if not 1 <= count <= 6:
                raise ValueError('Sequential review requires one to six captured questions')
            reviewed = list(SCREEN_ONLY_STEPS[:-1]) + [name for i in range(1, count + 1) for name in (f'question{i}', f'answer{i}')]
        if [step.get("id") for step in steps] != reviewed:
            raise ValueError(f"Expected the reviewed step IDs in order: {ident}")
        for step in steps:
            if not step.get("title") or not step.get("instruction"):
                raise ValueError(f"Step title and instruction required: {ident}/{step.get('id')}")
            if not allow_incomplete and not step.get("image"):
                raise ValueError(f"A real captured image is required: {ident}/{step['id']}")
            if step.get("image"):
                asset_path(step["image"])
                if Path(step["image"]).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".svg"}:
                    raise ValueError("Step images must reference image files")
            spots = step.get("hotspots", [])
            if step.get('videoBounds') is not None:
                bounds(step['videoBounds'])
            if step.get('answerBounds') is not None:
                bounds(step['answerBounds'])
            if sequential and step['id'].startswith(('question', 'answer')):
                index = step.get('questionIndex')
                phase = 'prompt' if step['id'].startswith('question') else 'answer'
                if (type(index) is not int or not 0 <= index < len(scenario.get('followups', []))
                        or step.get('questionPhase') != phase or step.get('kind') != 'followup'):
                    raise ValueError('Sequential screens must identify their captured question and phase')
                if phase == 'answer':
                    bounds(step.get('answerBounds'))
            if not isinstance(spots, list) or not all(isinstance(spot, dict) for spot in spots):
                raise ValueError("Hotspots must be an array of objects")
            for spot in spots:
                coords = [spot.get(key) for key in ("x", "y", "width", "height")]
                if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in coords):
                    raise ValueError("Hotspots require numeric x/y/width/height")
                if (not isinstance(spot.get("label"), str) or not spot["label"].strip()
                        or spot.get("action") not in {"next", "play", "report", "followup", "question"}):
                    raise ValueError("Hotspots require a label and supported action")
                x, y, width, height = coords
                if (x < 0 or y < 0 or width <= 0 or height <= 0
                        or x + width > 1.00001 or y + height > 1.00001):
                    raise ValueError("Hotspots must fit the normalized image bounds")
                if spot.get('reportType') is not None and spot['reportType'] not in ('pdf','markdown'):
                    raise ValueError('Report hotspots must select PDF or Markdown')
                if spot.get('cue') is not None and (not isinstance(spot['cue'],str) or not spot['cue'].strip() or len(spot['cue']) > 40):
                    raise ValueError('Hotspot cues must be concise text')
                if spot.get('action') == 'question':
                    question = spot.get('questionIndex')
                    if type(question) is not int or not 0 <= question < len(scenario.get('followups', [])):
                        raise ValueError('Question hotspots must select a captured answer')
                    bounds(spot.get('answerBounds'))
        if not allow_incomplete:
            video = scenario.get("video")
            if not isinstance(video, dict) or not video.get("src"):
                raise ValueError(f"Missing real evidence video: {ident}")
            asset_path(video["src"])
            if not video["src"].endswith(".mp4"):
                raise ValueError(f"Evidence must reference an MP4: {ident}")
            reports = scenario.get("reports")
            if not isinstance(reports, list) or not reports:
                raise ValueError(f"Missing captured report: {ident}")
            for report in reports:
                if (not isinstance(report, dict) or not isinstance(report.get("label"), str)
                        or not report["label"].strip() or report.get("type") not in {"pdf", "markdown"}
                        or not report.get("src")):
                    raise ValueError(f"Invalid captured report: {ident}")
                asset_path(report["src"])
                expected = ".pdf" if report["type"] == "pdf" else ".md"
                if not report["src"].endswith(expected):
                    raise ValueError("Report type and extension do not match")
                if 'pages' in report:
                    pages=report['pages']
                    if report['type'] != 'pdf' or not isinstance(pages,list) or not pages:
                        raise ValueError('Rendered pages require a nonempty PDF page list')
                    for index,page in enumerate(pages,1):
                        if (not isinstance(page,dict) or type(page.get('page')) is not int or page['page']!=index
                                or any(type(page.get(k)) is not int or page[k]<=0 for k in ('width','height'))):
                            raise ValueError('PDF pages require ordered page numbers and native dimensions')
                        asset_path(page.get('src'))
                        if Path(page['src']).suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):
                            raise ValueError('PDF page images must be supported raster files')
            incident = scenario.get("incident", {})
            if not isinstance(incident, dict) or not all(incident.get(key) for key in ("id", "startUtc", "endUtc")):
                raise ValueError(f"Missing captured incident identity/window: {ident}")
            followups = scenario.get("followups")
            if not isinstance(followups, list) or not followups:
                raise ValueError(f"Missing captured follow-up content: {ident}")
            for question in followups:
                if not isinstance(question, dict):
                    raise ValueError(f"Invalid follow-up content: {ident}")
                source = question.get("source")
                if (not isinstance(question.get("question"), str) or not question["question"].strip()
                        or not isinstance(question.get("answer"), str) or not question["answer"].strip()
                        or not isinstance(source, dict) or not isinstance(source.get("label"), str)
                        or not source["label"].strip() or source.get("kind") not in {"captured-chat", "report"}):
                    raise ValueError(f"Follow-ups require captured answers and provenance: {ident}")
    return list_assets(data)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build(manifest: Path, asset_root: Path, output: Path, *, allow_incomplete: bool = False,
          port: int = 18765) -> dict:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Local preview port must be between 1024 and 65535')
    data = json.loads(manifest.read_text())
    assets = validate_manifest(data, allow_incomplete=allow_incomplete)
    root = asset_root.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = {}
    for relative in assets:
        source = (root / relative).resolve(strict=True)
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError(f"Asset is outside asset root or is not a file: {relative}")
        if source.stat().st_size == 0:
            raise ValueError(f"Empty asset: {relative}")
        sources[relative] = source
    for name in APP_FILES:
        if not (APP_ROOT / name).is_file():
            raise ValueError(f"Application source missing: {name}")
    staging = Path(tempfile.mkdtemp(prefix=".walkthrough-stage-", dir=output.parent))
    try:
        for name in APP_FILES:
            shutil.copy2(APP_ROOT / name, staging / name)
        for relative, source in sources.items():
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        (staging / "demo.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        (staging / "README.txt").write_text(
            "WAREHOUSE CAPTURED WALKTHROUGH\n\n"
            "Mac: double-click Open-Warehouse-Demo.command.\n"
            "Or, from this directory, run:\n"
            f"  python3 serve_demo.py --directory . --port {port}\n\n"
            f"Open http://127.0.0.1:{port}/ in your browser.\n"
            "Keep the local server terminal open while using the demo.\n"
            "Open the server URL, not index.html directly.\n\n"
            "Choose a scenario and use highlighted controls on the real screens.\n"
            "See the platform architecture opens the full platform overview first.\n"
            "Warehouse analytics in motion: Automatic plays the existing animation;\n"
            "Click-through holds each highlighted stage until you choose Back / Next.\n"
            "PDF reports open inside the walkthrough with full page images;\n"
            "use Fit width to read closely, or download the unchanged original PDF.\n\n"
            "This captured walkthrough does not contact a live AI service.\n"
            "All screenshots, evidence videos and reports are included locally.\n"
            "Original generated reports retain their historical source URLs;\n"
            "use the walkthrough's local Play evidence controls for playback.\n"
            "\nVerify the unchanged package with:\n"
            "  python3 tools/verify_bundle.py --directory .\n"
        )
        shutil.copy2(APP_ROOT / "tools" / "serve_demo.py", staging / "serve_demo.py")
        (staging / "tools").mkdir()
        for helper in ("build_demo.py", "verify_bundle.py"):
            source = Path(__file__).with_name(helper)
            if source.is_file():
                shutil.copy2(source, staging / "tools" / helper)
        launcher = staging / "Open-Warehouse-Demo.command"
        launcher.write_text('#!/bin/bash\nset -eu\ncd "$(dirname "$0")"\n'
                            f'exec python3 ./serve_demo.py --directory . --port {port}\n')
        launcher.chmod(0o755)
        files = []
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                files.append({"path": path.relative_to(staging).as_posix(),
                              "bytes": path.stat().st_size, "sha256": digest_file(path)})
        summary = {"format": 1, "kind": "captured-warehouse-walkthrough",
                   "completeAssetContract": not allow_incomplete, "files": files}
        (staging / "checksums.json").write_text(json.dumps(summary, indent=2) + "\n")
        staging.rename(output)
        return {"output": str(output), "scenarios": len(data["scenarios"]),
                "assets": len(assets), "files": len(files),
                "completeAssetContract": not allow_incomplete}
    except BaseException:
        shutil.rmtree(staging)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="Preview only; incomplete content is not final acceptance")
    parser.add_argument('--port', type=int, default=18765, help='Local launcher port for the packaged preview.')
    args = parser.parse_args()
    try:
        result = build(args.manifest, args.asset_root, args.output, allow_incomplete=args.allow_incomplete, port=args.port)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"FAIL: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
