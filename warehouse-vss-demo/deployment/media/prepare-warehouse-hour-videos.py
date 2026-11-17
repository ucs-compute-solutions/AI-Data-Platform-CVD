#!/usr/bin/env python3
"""Build new one-hour Warehouse MP4s without replacing the ten-minute originals.

Default mode probes the four locked inputs and prints the exact remux commands.
--execute creates a new output directory, retains partial files on failure, and
publishes each MP4 only after validation. No OpenShift commands are run.
"""

from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


SOURCES = {
    "Camera.mp4": "56df2cd9c0e7d54f45145da2d265741ca019666663946d5f3cf09bee96c0218c",
    "Camera_01.mp4": "258a6c4f9780780a69d0b7cad3cafd0c39581ead5f54b7b96c42de0dbeb4065e",
    "Camera_02.mp4": "7d246d79dd1d768921ceee1e1ca1581f9fc0e87eb77d79cf41a225e895dcf7e2",
    "Camera_03.mp4": "3e5d509856702176d1437b8b71e34d0c83d7146717ab26b5cbd7afcf4637be3b",
}
PASSES = 6
SOURCE_SECONDS = 600
TARGET_SECONDS = 3600
FPS = 30


class VideoError(RuntimeError):
    pass


def run(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise VideoError(f"Command failed: {shlex.join(command)}\n{result.stderr[-3000:]}")
    return result.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(path: Path, ffprobe: str) -> dict:
    return json.loads(run([
        ffprobe, "-v", "error", "-count_packets", "-show_streams",
        "-show_format", "-of", "json", str(path),
    ]))


def validate_metadata(data: dict, *, output: bool = False, source_packets: int = 0) -> dict:
    streams = data.get("streams", [])
    if len(streams) != 1 or streams[0].get("codec_type") != "video":
        raise VideoError("Expected exactly one video stream and no audio or data streams")
    video = streams[0]
    if video.get("codec_name") != "h264":
        raise VideoError("Expected H.264 video")
    try:
        rate = Fraction(video["avg_frame_rate"])
        width, height = int(video["width"]), int(video["height"])
        packets = int(video["nb_read_packets"])
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise VideoError("Incomplete video metadata") from error
    expected_seconds = TARGET_SECONDS if output else SOURCE_SECONDS
    if rate != FPS or width <= 0 or height <= 0:
        raise VideoError("Expected a positive picture size and 30 fps")
    if not math.isfinite(duration) or abs(duration - expected_seconds) > 0.1:
        raise VideoError(f"Expected {expected_seconds}s, found {duration}s")
    expected_packets = source_packets * PASSES if output else SOURCE_SECONDS * FPS
    if packets != expected_packets:
        raise VideoError(f"Expected {expected_packets} video packets, found {packets}")
    return {
        "codec": "h264", "width": width, "height": height,
        "fps": FPS, "seconds": duration, "packets": packets,
    }


def validate_timestamps(text: str, expected_packets: int) -> None:
    """DTS, not presentation order, must advance uniformly with H.264 B-frames."""
    previous = None
    count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            current = float(line.split(",", 1)[0])
        except ValueError as error:
            raise VideoError("Missing or invalid decode timestamp") from error
        if not math.isfinite(current):
            raise VideoError("Non-finite decode timestamp")
        if previous is not None and abs(current - previous - 1 / FPS) > 0.00001:
            raise VideoError(f"Non-uniform decode timestamps at packet {count + 1}")
        previous = current
        count += 1
    if count != expected_packets:
        raise VideoError(f"Expected {expected_packets} packet timestamps, found {count}")


def remux_command(ffmpeg: str, source: Path, partial: Path) -> list[str]:
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-v", "warning", "-n",
        "-stream_loop", str(PASSES - 1), "-i", str(source),
        "-map", "0:v:0", "-c:v", "copy", "-an", "-movflags", "+faststart",
        str(partial),
    ]


def validate_media(path: Path, ffmpeg: str, ffprobe: str, source_metadata: dict) -> dict:
    metadata = validate_metadata(probe(path, ffprobe), output=True,
                                 source_packets=source_metadata["packets"])
    if (metadata["width"], metadata["height"]) != (
        source_metadata["width"], source_metadata["height"]
    ):
        raise VideoError("Remuxed picture size differs from the original")
    timestamps = run([
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=dts_time", "-of", "csv=p=0", str(path),
    ])
    validate_timestamps(timestamps, metadata["packets"])
    # Decode the beginning, five repeat joins, and tail; a remux needs no encoder.
    for start in [0, 599.5, 1199.5, 1799.5, 2399.5, 2999.5, 3599]:
        run([
            ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror",
            "-err_detect", "explode", "-ss", str(start), "-i", str(path),
            "-map", "0:v:0", "-t", "1", "-f", "null", "-",
        ])
    return metadata


def prepare(input_dir: Path, output_dir: Path, ffmpeg: str, ffprobe: str) -> list[dict]:
    input_dir = input_dir.resolve(strict=True)
    if not input_dir.is_dir():
        raise VideoError("Input must be a directory")
    if output_dir.is_symlink() or output_dir.exists():
        raise VideoError("Output directory already exists; choose a new directory")
    parent = output_dir.parent.resolve(strict=True)
    if not parent.is_dir():
        raise VideoError("Output parent must already exist")
    if parent == input_dir or input_dir in parent.parents:
        raise VideoError("Place derived videos outside the original source directory")
    result = []
    for filename, expected_hash in SOURCES.items():
        source = input_dir / filename
        if source.is_symlink() or not source.is_file():
            raise VideoError(f"Required original is absent or a symlink: {filename}")
        if sha256(source) != expected_hash:
            raise VideoError(f"Original checksum mismatch: {filename}")
        metadata = validate_metadata(probe(source, ffprobe))
        result.append({
            "source": source, "partial": output_dir / f".{source.stem}.partial.mp4",
            "output": output_dir / filename, "sourceMetadata": metadata,
            "sourceBytes": source.stat().st_size,
            "command": remux_command(ffmpeg, source, output_dir / f".{source.stem}.partial.mp4"),
        })
    required = sum(item["sourceBytes"] for item in result) * PASSES
    # faststart rewrites one file; allow one extra largest output plus 10% margin.
    reserve = math.ceil((required + max(item["sourceBytes"] for item in result) * PASSES) * 1.1)
    if shutil.disk_usage(parent).free < reserve:
        raise VideoError(f"Need at least {reserve} free bytes for outputs and faststart workspace")
    return result


def execute(plan: list[dict], output_dir: Path, ffmpeg: str, ffprobe: str) -> dict:
    output_dir.mkdir(mode=0o755, exist_ok=False)
    manifest = {
        "kind": "WarehouseDerivedHourVideos", "secondsPerCamera": TARGET_SECONDS,
        "repeatedOriginalPasses": PASSES, "audio": False,
        "ffmpegVersion": run([ffmpeg, "-version"]).splitlines()[0], "videos": [],
    }
    for item in plan:
        source, partial, output = item["source"], item["partial"], item["output"]
        print(f"Building {output.name}: six original passes, video stream-copy", flush=True)
        run(item["command"])
        metadata = validate_media(partial, ffmpeg, ffprobe, item["sourceMetadata"])
        if sha256(source) != SOURCES[source.name]:
            raise VideoError(f"Original changed during preparation: {source.name}")
        # Output directory was created exclusively; no preexisting output is replaced.
        if output.exists() or output.is_symlink():
            raise VideoError(f"Output appeared unexpectedly: {output.name}")
        partial.rename(output)
        manifest["videos"].append({
            "filename": output.name, "sourceSha256": SOURCES[source.name],
            "sha256": sha256(output), "bytes": output.stat().st_size, **metadata,
        })
        print(f"PASS: {output.name}: {metadata['seconds']}s; packet continuity and joins verified", flush=True)
    with (output_dir / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        ffmpeg, ffprobe = shutil.which(args.ffmpeg), shutil.which(args.ffprobe)
        if not ffmpeg or not ffprobe:
            raise VideoError("ffmpeg and ffprobe must already be installed; nothing was changed")
        output_dir = args.output_dir.absolute()
        plan = prepare(args.input_dir, output_dir, ffmpeg, ffprobe)
        print("Plan: four new 3600-second MP4s; six repeats of each locked 600-second original")
        print("Originals are retained; no deployment, camera, rule, or recorder changes are included")
        for item in plan:
            print(shlex.join(item["command"]))
        if not args.execute:
            print("PLAN ONLY: no output directory or video created; add --execute to build")
            return 0
        execute(plan, output_dir, ffmpeg, ffprobe)
        print(f"PASS: four derived videos and manifest.json created in {output_dir}")
        print("Playback, synchronized cameras, alerts, recording, and hour-long service health remain to test")
        return 0
    except (VideoError, OSError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}\nExisting sources retained; any new partial output directory is retained for inspection", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
