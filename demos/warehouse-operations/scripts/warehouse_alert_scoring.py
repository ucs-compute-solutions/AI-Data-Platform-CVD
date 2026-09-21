#!/usr/bin/env python3

"""Prepare and evaluate evidence for the NVIDIA Warehouse alert demo.

The tool is intentionally local-only.  It never connects to OpenShift, copies
video, or stores raw exported records.  Evidence ingestion writes a small,
whitelisted index of identifiers and scoring fields plus the source export's
SHA-256 digest.
"""

from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit


STOCK_RULES = (
    "near_miss",
    "ppe",
    "load_quality",
    "pathway_obstruction",
    "spillover",
)
ANALYTICS_ONLY_RULES = ("roi_tripwire", "restricted_area", "confined_area")
KNOWN_RULES = frozenset((*STOCK_RULES, *ANALYTICS_ONLY_RULES))
EVIDENCE_TYPES = frozenset(
    {
        "perception",
        "behavior",
        "vlm",
        "delivery",
        "alerts_ui",
        "clip",
        "agent_report",
        "health",
    }
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]+|password\s*[:=]|api[_ -]?key\s*[:=]|"
    r"token\s*[:=]|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
SENSITIVE_PATH_PART_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|kubeconfig)"
)
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

HUMAN_LABEL_COLUMNS = (
    "record_type",
    "label_id",
    "reviewer_id",
    "sensor_id",
    "source_file",
    "rule",
    "label",
    "start_seconds",
    "end_seconds",
    "visible_facts",
    "timeline_reviewed",
    "review_completed_utc",
    "notes",
)
ADJUDICATED_COLUMNS = (
    "ground_truth_id",
    "sensor_id",
    "source_file",
    "rule",
    "label",
    "start_seconds",
    "end_seconds",
    "visible_facts",
    "reviewer_label_ids",
    "adjudication_status",
    "adjudicator_ids",
    "adjudicated_utc",
    "notes",
)
EVIDENCE_COLUMNS = (
    "evidence_id",
    "run_id",
    "replay_iteration",
    "evidence_type",
    "sensor_id",
    "rule",
    "source_start_seconds",
    "source_end_seconds",
    "event_utc",
    "object_class",
    "track_id",
    "event_id",
    "incident_id",
    "alert_id",
    "status",
    "verdict",
    "class_id",
    "class_label",
    "clip_ref",
    "clip_playable",
    "topic",
    "partition",
    "offset",
    "index_name",
    "document_id",
    "model_id",
    "config_hash",
    "latency_ms",
    "report_grounded",
    "health_ok",
    "source_export",
    "source_sha256",
    "source_record",
)
SCENARIO_RUN_COLUMNS = (
    "scenario_id",
    "ground_truth_id",
    "run_id",
    "replay_iteration",
    "prediction_evidence_id",
    "perception_evidence_ids",
    "behavior_evidence_ids",
    "vlm_evidence_ids",
    "delivery_evidence_id",
    "alerts_ui_evidence_id",
    "clip_evidence_id",
    "agent_report_evidence_id",
    "health_evidence_id",
    "notes",
)

DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "sensor_id": (
        "sensor_id",
        "sensorId",
        "sensor.id",
        "camera_id",
        "cameraId",
        "source.sensor_id",
        "source.sensorId",
    ),
    "rule": ("rule", "rule_id", "ruleId", "alert_type", "alertType", "category"),
    "source_start_seconds": (
        "source_start_seconds",
        "start_seconds",
        "sourceStartSeconds",
        "clip.start_seconds",
    ),
    "source_end_seconds": (
        "source_end_seconds",
        "end_seconds",
        "sourceEndSeconds",
        "clip.end_seconds",
    ),
    "event_utc": ("event_utc", "@timestamp", "timestamp", "event.timestamp"),
    "object_class": ("object_class", "objectClass", "object.class", "class"),
    "track_id": ("track_id", "trackId", "object.track_id", "object.id"),
    "event_id": ("event_id", "eventId", "event.id", "id"),
    "incident_id": ("incident_id", "incidentId", "incident.id"),
    "alert_id": ("alert_id", "alertId", "alert.id"),
    "status": ("status", "state", "delivery_status"),
    "verdict": ("verdict", "verification_status", "verificationStatus", "answer"),
    "class_id": ("class_id", "classId", "classification.id", "output.class_id"),
    "class_label": (
        "class_label",
        "classLabel",
        "classification.label",
        "output.class_label",
        "label",
    ),
    "clip_ref": ("clip_ref", "clipId", "clip.id", "video_id", "videoId"),
    "clip_playable": ("clip_playable", "clipPlayable", "playable"),
    "topic": ("topic", "_topic", "kafka.topic"),
    "partition": ("partition", "_partition", "kafka.partition"),
    "offset": ("offset", "_offset", "kafka.offset"),
    "index_name": ("index_name", "_index", "index"),
    "document_id": ("document_id", "_id", "document.id"),
    "model_id": ("model_id", "modelId", "model.id", "model"),
    "config_hash": ("config_hash", "configuration_hash", "config.sha256"),
    "latency_ms": ("latency_ms", "latencyMs", "latency.ms"),
    "report_grounded": ("report_grounded", "reportGrounded", "grounded"),
    "health_ok": ("health_ok", "healthOk", "healthy"),
}


class ScoringError(RuntimeError):
    """Expected, user-correctable validation failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def split_yaml_key_value(text: str, line_number: int) -> tuple[str, str]:
    quote: str | None = None
    for index, char in enumerate(text):
        if char in "\"'":
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == ":" and quote is None:
            key = text[:index].strip()
            if not key:
                raise ScoringError(f"empty YAML key on line {line_number}")
            return key, text[index + 1 :].strip()
    raise ScoringError(f"expected key/value YAML entry on line {line_number}")


def strip_yaml_comment(text: str) -> str:
    quote: str | None = None
    for index, char in enumerate(text):
        if char in "\"'":
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == "#" and quote is None and (index == 0 or text[index - 1].isspace()):
            return text[:index].rstrip()
    return text.rstrip()


def parse_yaml_scalar(text: str) -> Any:
    if text in {"null", "Null", "NULL", "~"}:
        return None
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [parse_yaml_scalar(item.strip()) for item in inner.split(",")]
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        try:
            return ast.literal_eval(text)
        except (SyntaxError, ValueError) as exc:
            raise ScoringError(f"invalid quoted YAML scalar: {text}") from exc
    if re.fullmatch(r"-?[0-9]+", text):
        return int(text)
    if re.fullmatch(r"-?(?:[0-9]+\.[0-9]*|[0-9]*\.[0-9]+)", text):
        return float(text)
    return text


def simple_yaml_load(text: str) -> Any:
    """Load the mapping/list/scalar YAML subset used by the source manifest.

    PyYAML is deliberately not required by this local evidence utility.  The
    fallback rejects advanced YAML features instead of attempting unsafe or
    surprising interpretation.
    """

    tokens: list[tuple[int, str, int]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ScoringError(f"tabs are not allowed for YAML indentation (line {line_number})")
        content = strip_yaml_comment(raw.lstrip(" "))
        if not content:
            continue
        tokens.append((len(raw) - len(raw.lstrip(" ")), content, line_number))
    if not tokens:
        raise ScoringError("manifest is empty")

    def parse_block(position: int, indent: int) -> tuple[Any, int]:
        if position >= len(tokens) or tokens[position][0] != indent:
            raise ScoringError("invalid YAML indentation")
        is_list = tokens[position][1].startswith("- ") or tokens[position][1] == "-"
        container: Any = [] if is_list else {}

        while position < len(tokens):
            current_indent, content, line_number = tokens[position]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ScoringError(f"unexpected YAML indentation on line {line_number}")

            if is_list:
                if not (content.startswith("- ") or content == "-"):
                    break
                item_text = content[1:].strip()
                position += 1
                if not item_text:
                    if position >= len(tokens) or tokens[position][0] <= indent:
                        container.append(None)
                    else:
                        child, position = parse_block(position, tokens[position][0])
                        container.append(child)
                    continue
                if ":" in item_text:
                    key, value_text = split_yaml_key_value(item_text, line_number)
                    item: dict[str, Any] = {}
                    if value_text:
                        item[key] = parse_yaml_scalar(value_text)
                    elif position < len(tokens) and tokens[position][0] > indent:
                        child, position = parse_block(position, tokens[position][0])
                        item[key] = child
                    else:
                        item[key] = None
                    if position < len(tokens) and tokens[position][0] > indent:
                        extra, position = parse_block(position, tokens[position][0])
                        if not isinstance(extra, dict):
                            raise ScoringError(
                                f"list mapping continuation must be a mapping (line {line_number})"
                            )
                        duplicate = set(item) & set(extra)
                        if duplicate:
                            raise ScoringError(f"duplicate YAML key: {sorted(duplicate)[0]}")
                        item.update(extra)
                    container.append(item)
                else:
                    container.append(parse_yaml_scalar(item_text))
                continue

            if content.startswith("- ") or content == "-":
                break
            key, value_text = split_yaml_key_value(content, line_number)
            if key in container:
                raise ScoringError(f"duplicate YAML key {key!r} on line {line_number}")
            position += 1
            if value_text:
                container[key] = parse_yaml_scalar(value_text)
            elif position < len(tokens) and tokens[position][0] > indent:
                child, position = parse_block(position, tokens[position][0])
                container[key] = child
            else:
                container[key] = None
        return container, position

    root, consumed = parse_block(0, tokens[0][0])
    if consumed != len(tokens):
        _, _, line_number = tokens[consumed]
        raise ScoringError(f"could not parse YAML starting on line {line_number}")
    return root


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ScoringError(f"manifest does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        document = simple_yaml_load(text)
    else:
        document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ScoringError("manifest root must be a mapping")
    return document


def require_mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScoringError(f"{location} must be a mapping")
    return value


def require_list(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise ScoringError(f"{location} must be a list")
    return value


def require_text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScoringError(f"{location} must be non-empty text")
    return value.strip()


def require_positive_int(value: Any, location: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScoringError(f"{location} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ScoringError(f"{location} must be at least {minimum}")
    return value


def require_sha256(value: Any, location: str) -> str:
    text = require_text(value, location)
    if not SHA256_RE.fullmatch(text):
        raise ScoringError(f"{location} must be a lowercase SHA-256 digest")
    return text


def safe_relative_path(value: Any, location: str) -> Path:
    text = require_text(value, location)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ScoringError(f"{location} must be a safe relative path")
    return path


def validate_verified_alerts(alerts: Any, location: str, duration: float) -> None:
    for index, item in enumerate(require_list(alerts, location)):
        row = require_mapping(item, f"{location}[{index}]")
        rule = canonical_rule(require_text(row.get("rule"), f"{location}[{index}].rule"))
        if rule not in STOCK_RULES:
            raise ScoringError(f"{location}[{index}].rule is not a stock Alerts UI rule")
        start = number(row.get("start_seconds"), f"{location}[{index}].start_seconds")
        end = number(row.get("end_seconds"), f"{location}[{index}].end_seconds")
        validate_interval(start, end, duration, f"{location}[{index}]")
        if int(row.get("reproducible_runs", 0)) != 3:
            raise ScoringError(f"{location}[{index}] must record reproducible_runs: 3")


def validate_manifest_document(
    manifest: dict[str, Any],
    *,
    media_root: Path | None = None,
    archive_path: Path | None = None,
    profile_root: Path | None = None,
) -> dict[str, Any]:
    if str(manifest.get("manifest_version")) != "1.0":
        raise ScoringError("manifest_version must be 1.0")
    require_text(manifest.get("status"), "status")
    require_text(manifest.get("verified_date"), "verified_date")

    dataset = require_mapping(manifest.get("dataset"), "dataset")
    require_text(dataset.get("name"), "dataset.name")
    require_text(dataset.get("ngc_resource"), "dataset.ngc_resource")
    require_text(dataset.get("version"), "dataset.version")
    require_text(dataset.get("presentation_permission"), "dataset.presentation_permission")
    archive = require_mapping(dataset.get("archive"), "dataset.archive")
    safe_relative_path(archive.get("filename"), "dataset.archive.filename")
    archive_size = require_positive_int(archive.get("byte_size"), "dataset.archive.byte_size")
    archive_sha = require_sha256(archive.get("sha256"), "dataset.archive.sha256")

    profile = require_mapping(manifest.get("nvidia_profile"), "nvidia_profile")
    require_text(profile.get("repository"), "nvidia_profile.repository")
    require_text(profile.get("tag"), "nvidia_profile.tag")
    commit = require_text(profile.get("commit"), "nvidia_profile.commit")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ScoringError("nvidia_profile.commit must be a lowercase 40-character Git commit")
    stream_count = require_positive_int(profile.get("stream_count"), "nvidia_profile.stream_count")
    locks = require_mapping(profile.get("configuration_locks"), "nvidia_profile.configuration_locks")
    expected_locks = {
        "behavior_analytics",
        "alert_verification",
        "realtime_alerts",
        "calibration",
        "warehouse_environment",
    }
    if set(locks) != expected_locks:
        raise ScoringError(
            "configuration_locks must contain exactly: " + ", ".join(sorted(expected_locks))
        )

    lock_checks: list[dict[str, Any]] = []
    for name in sorted(locks):
        lock = require_mapping(locks[name], f"nvidia_profile.configuration_locks.{name}")
        relative = safe_relative_path(
            lock.get("path"), f"nvidia_profile.configuration_locks.{name}.path"
        )
        expected_sha = require_sha256(
            lock.get("sha256"), f"nvidia_profile.configuration_locks.{name}.sha256"
        )
        result: dict[str, Any] = {"name": name, "path": str(relative), "checked": False}
        if profile_root is not None:
            target = profile_root / relative
            verify_file(target, None, expected_sha, f"configuration lock {name}")
            result["checked"] = True
        lock_checks.append(result)

    source_profile = require_mapping(manifest.get("source_video_profile"), "source_video_profile")
    duration = float(
        require_positive_int(source_profile.get("duration_seconds"), "source_video_profile.duration_seconds")
    )
    require_positive_int(source_profile.get("width"), "source_video_profile.width")
    require_positive_int(source_profile.get("height"), "source_video_profile.height")
    require_positive_int(source_profile.get("frame_rate_fps"), "source_video_profile.frame_rate_fps")

    cameras = require_list(manifest.get("cameras"), "cameras")
    if len(cameras) != stream_count:
        raise ScoringError(
            f"camera count ({len(cameras)}) does not match stream_count ({stream_count})"
        )
    ordinals: set[int] = set()
    sensors: set[str] = set()
    sources: set[str] = set()
    vast_ids: set[str] = set()
    camera_results: list[dict[str, Any]] = []
    positive_alert_count = 0
    for index, raw_camera in enumerate(cameras):
        camera = require_mapping(raw_camera, f"cameras[{index}]")
        ordinal = require_positive_int(
            camera.get("ordinal"), f"cameras[{index}].ordinal", allow_zero=True
        )
        source = safe_relative_path(camera.get("source_file"), f"cameras[{index}].source_file")
        size = require_positive_int(camera.get("source_byte_size"), f"cameras[{index}].source_byte_size")
        digest = require_sha256(camera.get("source_sha256"), f"cameras[{index}].source_sha256")
        sensor = require_text(camera.get("nvidia_sensor_id"), f"cameras[{index}].nvidia_sensor_id")
        calibration = require_mapping(camera.get("calibration"), f"cameras[{index}].calibration")
        if not require_list(calibration.get("roi_ids"), f"cameras[{index}].calibration.roi_ids"):
            raise ScoringError(f"cameras[{index}] must contain at least one ROI ID")
        if not require_list(
            calibration.get("tripwire_ids"), f"cameras[{index}].calibration.tripwire_ids"
        ):
            raise ScoringError(f"cameras[{index}] must contain at least one tripwire ID")
        vast = require_mapping(camera.get("vast_native"), f"cameras[{index}].vast_native")
        vast_id = require_text(vast.get("camera_id"), f"cameras[{index}].vast_native.camera_id")
        safe_relative_path(
            vast.get("staging_prefix"), f"cameras[{index}].vast_native.staging_prefix"
        )
        alerts = camera.get("verified_positive_alerts")
        validate_verified_alerts(alerts, f"cameras[{index}].verified_positive_alerts", duration)
        positive_alert_count += len(alerts)

        for value, seen, location in (
            (ordinal, ordinals, "ordinal"),
            (sensor, sensors, "sensor ID"),
            (str(source), sources, "source file"),
            (vast_id, vast_ids, "VAST camera ID"),
        ):
            if value in seen:
                raise ScoringError(f"duplicate camera {location}: {value}")
            seen.add(value)

        checked = False
        if media_root is not None:
            verify_file(media_root / source, size, digest, f"source video {source}")
            checked = True
        camera_results.append(
            {
                "ordinal": ordinal,
                "sensor_id": sensor,
                "source_file": str(source),
                "checked": checked,
            }
        )
    if ordinals != set(range(stream_count)):
        raise ScoringError(f"camera ordinals must be contiguous from 0 through {stream_count - 1}")

    scoring = require_mapping(manifest.get("scenario_scoring"), "scenario_scoring")
    scoring_status = require_text(scoring.get("status"), "scenario_scoring.status")
    timestamps = require_list(
        scoring.get("verified_alert_timestamps"), "scenario_scoring.verified_alert_timestamps"
    )
    selected = scoring.get("selected_demo_scenario")
    if scoring_status == "pending":
        if timestamps or selected is not None or positive_alert_count:
            raise ScoringError(
                "pending scenario_scoring may not contain verified timestamps, a selected scenario, "
                "or verified positive alerts"
            )
    elif selected is not None and canonical_rule(str(selected)) not in STOCK_RULES:
        raise ScoringError("selected_demo_scenario must be a stock Alerts UI rule")

    archive_checked = False
    if archive_path is not None:
        verify_file(archive_path, archive_size, archive_sha, "dataset archive")
        archive_checked = True

    return {
        "valid": True,
        "dataset": dataset["name"],
        "profile_commit": commit,
        "camera_count": len(cameras),
        "duration_seconds": duration,
        "positive_alert_count": positive_alert_count,
        "scenario_status": scoring_status,
        "media_hashes_checked": sum(1 for row in camera_results if row["checked"]),
        "archive_hash_checked": archive_checked,
        "configuration_hashes_checked": sum(1 for row in lock_checks if row["checked"]),
        "cameras": camera_results,
        "configuration_locks": lock_checks,
    }


def verify_file(path: Path, expected_size: int | None, expected_sha: str, description: str) -> None:
    if not path.is_file():
        raise ScoringError(f"{description} is missing: {path}")
    if expected_size is not None and path.stat().st_size != expected_size:
        raise ScoringError(
            f"{description} size mismatch: expected {expected_size}, found {path.stat().st_size}"
        )
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        raise ScoringError(
            f"{description} SHA-256 mismatch: expected {expected_sha}, found {actual_sha}"
        )


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(columns), extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in columns})
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def ensure_targets_absent(paths: Iterable[Path]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise ScoringError(
            "refusing to overwrite existing scoring files: " + ", ".join(sorted(existing))
        )


def scoring_checklist_text(dataset: str, duration: int, sensors: Sequence[str]) -> str:
    sensor_text = ", ".join(f"`{sensor}`" for sensor in sensors)
    return f"""# Warehouse alert scoring checklist

Dataset: `{dataset}`<br>
Scoring interval: source seconds `0-{duration}`<br>
Sensors: {sensor_text}

## Freeze inputs

- [ ] Manifest structure passes validation.
- [ ] Dataset archive, four source files, and five configuration locks pass SHA-256 validation.
- [ ] Run metadata records exact source commit, image digests, model IDs, and configuration hashes.
- [ ] Presentation permission is confirmed separately before any external demonstration.

## Establish ground truth before viewing system output

- [ ] Two reviewers independently complete every camera timeline.
- [ ] Each candidate uses `positive`, `negative`, or `uncertain` and records only visible facts.
- [ ] Reviewers use source-relative seconds; they do not copy system alert timestamps.
- [ ] Disagreements are adjudicated into `adjudicated-labels.csv` before evidence is scored.
- [ ] Each accepted positive has a stable `ground_truth_id`; uncertain labels are not positives.

## Capture three clean runs

- [ ] Each run starts all four sources at source second 0 and scores only the first {duration} seconds.
- [ ] Run IDs are unique and replay iterations are exactly 1, 2, and 3.
- [ ] Perception, behavior, VLM, delivery, Alerts UI, playable clip, Agent report, and health evidence are retained where required by the selected rule.
- [ ] Kafka topic/partition/offset and Elasticsearch index/document IDs are retained when supplied.
- [ ] Raw exports remain outside the repository; normalized evidence contains no credentials or private URLs.
- [ ] Each claim row references only evidence from the same run and replay iteration.

## Reproducibility gate

- [ ] The same adjudicated event matches by sensor, rule, and at least one second of interval overlap.
- [ ] Near miss has a traceable incident and a `confirmed` verdict in 3/3 runs.
- [ ] Always-on rules have class `0` / `Yes` in 3/3 runs.
- [ ] The alert is visible, the correct clip plays, and the Agent report is grounded in 3/3 runs.
- [ ] Health evidence is acceptable in 3/3 runs and failures/timeouts are recorded.
- [ ] The scorer reports `PASS` with `--require-pass`; otherwise the scenario remains unqualified.

Do not enter a verified timestamp in the source manifest until the full gate passes.
"""


def init_workspace(manifest_path: Path, output_dir: Path, reviewers: Sequence[str]) -> list[Path]:
    manifest = load_manifest(manifest_path)
    validation = validate_manifest_document(manifest)
    if len(reviewers) != 2 or len(set(reviewers)) != 2:
        raise ScoringError("exactly two distinct reviewer IDs are required")
    for reviewer in reviewers:
        if not SAFE_ID_RE.fullmatch(reviewer):
            raise ScoringError(f"invalid reviewer ID: {reviewer!r}")

    output_dir.mkdir(parents=True, exist_ok=True)
    targets = [
        output_dir / f"{reviewer}-labels.csv" for reviewer in reviewers
    ] + [
        output_dir / "adjudicated-labels.csv",
        output_dir / "evidence.csv",
        output_dir / "scenario-runs.csv",
        output_dir / "run-metadata.json",
        output_dir / "SCORING-CHECKLIST.md",
    ]
    ensure_targets_absent(targets)

    duration = int(validation["duration_seconds"])
    camera_rows = validation["cameras"]
    for reviewer, target in zip(reviewers, targets[:2]):
        rows: list[dict[str, Any]] = []
        for camera in camera_rows:
            rows.append(
                {
                    "record_type": "timeline_review",
                    "reviewer_id": reviewer,
                    "sensor_id": camera["sensor_id"],
                    "source_file": camera["source_file"],
                    "start_seconds": 0,
                    "end_seconds": duration,
                    "timeline_reviewed": "no",
                }
            )
        write_csv(target, HUMAN_LABEL_COLUMNS, rows)
    write_csv(output_dir / "adjudicated-labels.csv", ADJUDICATED_COLUMNS, [])
    write_csv(output_dir / "evidence.csv", EVIDENCE_COLUMNS, [])
    write_csv(output_dir / "scenario-runs.csv", SCENARIO_RUN_COLUMNS, [])

    metadata = {
        "schema_version": "1.0",
        "status": "not_started",
        "manifest": manifest_path.name,
        "manifest_sha256": sha256_file(manifest_path),
        "dataset": validation["dataset"],
        "dataset_archive": {
            "filename": manifest["dataset"]["archive"]["filename"],
            "byte_size": manifest["dataset"]["archive"]["byte_size"],
            "sha256": manifest["dataset"]["archive"]["sha256"],
        },
        "source_videos": [
            {
                "sensor_id": camera["nvidia_sensor_id"],
                "source_file": camera["source_file"],
                "byte_size": camera["source_byte_size"],
                "sha256": camera["source_sha256"],
            }
            for camera in manifest["cameras"]
        ],
        "profile_commit": validation["profile_commit"],
        "configuration_locks": [
            {
                "name": name,
                "path": lock["path"],
                "sha256": lock["sha256"],
            }
            for name, lock in sorted(
                manifest["nvidia_profile"]["configuration_locks"].items()
            )
        ],
        "source_hashes_verified": False,
        "configuration_hashes_verified": False,
        "archive_hash_verified": False,
        "image_digests": [],
        "model_ids": [],
        "run_ids": [],
        "presentation_permission": manifest["dataset"]["presentation_permission"],
        "notes": "Do not mark complete until all hashes and exact runtime identifiers are recorded.",
    }
    atomic_write_text(
        output_dir / "run-metadata.json", json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )
    atomic_write_text(
        output_dir / "SCORING-CHECKLIST.md",
        scoring_checklist_text(
            validation["dataset"], duration, [row["sensor_id"] for row in camera_rows]
        ),
    )
    return targets


def canonical_rule(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    aliases = {
        "near_miss_violation": "near_miss",
        "proximity_violation": "near_miss",
        "ppe_violation": "ppe",
        "load_quality_violation": "load_quality",
        "pathway_obstruction_violation": "pathway_obstruction",
        "spillover_violation": "spillover",
        "roi": "roi_tripwire",
        "tripwire": "roi_tripwire",
    }
    return aliases.get(normalized, normalized)


def get_path(record: dict[str, Any], path: str) -> Any:
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def parse_mapping(items: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ScoringError(f"mapping must use FIELD=PATH: {item!r}")
        field, path = (piece.strip() for piece in item.split("=", 1))
        if field not in EVIDENCE_COLUMNS or field in {
            "evidence_id",
            "run_id",
            "replay_iteration",
            "evidence_type",
            "source_export",
            "source_sha256",
            "source_record",
        }:
            raise ScoringError(f"field cannot be mapped from an export: {field}")
        if not path or SENSITIVE_PATH_PART_RE.search(path):
            raise ScoringError(f"unsafe or empty export path in mapping for {field}")
        result[field] = path
    return result


def unpack_record(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    source = record.get("_source")
    if isinstance(source, dict):
        result.update(source)
    for envelope_key in ("value", "payload", "message"):
        payload = record.get(envelope_key)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict):
            for key, value in payload.items():
                result.setdefault(key, value)
    return result


def nested_records(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, list):
        if not all(isinstance(item, dict) for item in document):
            raise ScoringError("JSON evidence arrays must contain objects")
        return [unpack_record(item) for item in document]
    if not isinstance(document, dict):
        raise ScoringError("JSON evidence must be an object or array of objects")
    common_paths = ("hits.hits", "records", "items", "events", "alerts")
    for path in common_paths:
        value = get_path(document, path)
        if isinstance(value, list):
            if not all(isinstance(item, dict) for item in value):
                raise ScoringError(f"{path} must contain objects")
            return [unpack_record(item) for item in value]
    return [unpack_record(document)]


def read_export_records(path: Path, input_format: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ScoringError(f"evidence export does not exist: {path}")
    selected = input_format
    if selected == "auto":
        suffix = path.suffix.lower()
        selected = "csv" if suffix == ".csv" else "jsonl" if suffix in {".jsonl", ".ndjson"} else "json"
    if selected == "csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return [dict(row) for row in csv.DictReader(stream)]
    if selected == "jsonl":
        rows: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    document = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ScoringError(f"invalid JSON on line {line_number} of {path.name}") from exc
                rows.extend(nested_records(document))
        return rows
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScoringError(f"invalid JSON export: {path.name}") from exc
    return nested_records(document)


def first_present(record: dict[str, Any], paths: Sequence[str]) -> Any:
    for path in paths:
        value = get_path(record, path)
        if value is not None and value != "":
            return value
    return ""


def scalar_text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        text = str(value).strip()
    else:
        raise ScoringError(f"export field {field} must be scalar")
    if "\n" in text or "\r" in text:
        text = " ".join(text.splitlines())
    if SENSITIVE_TEXT_RE.search(text):
        raise ScoringError(f"export field {field} appears to contain a credential")
    if field == "clip_ref" and text:
        parsed = urlsplit(text)
        if parsed.scheme or parsed.netloc or text.startswith("/"):
            raise ScoringError("clip_ref must be a sanitized identifier, not a URL or absolute path")
    return text


def normalize_boolean_text(value: str, field: str) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "1", "pass", "passed", "ok"}:
        return "true"
    if lowered in {"false", "no", "0", "fail", "failed"}:
        return "false"
    raise ScoringError(f"{field} must be a recognizable true/false value")


def normalize_evidence_record(
    record: dict[str, Any],
    *,
    mappings: dict[str, str],
    run_id: str,
    iteration: int,
    evidence_type: str,
    default_sensor: str,
    default_rule: str,
    source_name: str,
    source_sha: str,
    record_number: int,
) -> dict[str, str]:
    row = {column: "" for column in EVIDENCE_COLUMNS}
    row.update(
        {
            "run_id": run_id,
            "replay_iteration": str(iteration),
            "evidence_type": evidence_type,
            "source_export": source_name,
            "source_sha256": source_sha,
            "source_record": str(record_number),
        }
    )
    for field in EVIDENCE_COLUMNS:
        if field in row and row[field]:
            continue
        if field in mappings:
            value = get_path(record, mappings[field])
        else:
            value = first_present(record, DEFAULT_ALIASES.get(field, ()))
        if value is not None and value != "":
            row[field] = scalar_text(value, field)
    if not row["sensor_id"]:
        row["sensor_id"] = default_sensor
    if not row["rule"]:
        row["rule"] = default_rule
    if row["rule"]:
        row["rule"] = canonical_rule(row["rule"])
        if row["rule"] not in KNOWN_RULES:
            raise ScoringError(f"unknown rule in export record {record_number}: {row['rule']}")
    for field in ("clip_playable", "report_grounded", "health_ok"):
        row[field] = normalize_boolean_text(row[field], field)
    seed = f"{source_sha}:{record_number}:{run_id}:{iteration}:{evidence_type}".encode()
    row["evidence_id"] = "EV-" + hashlib.sha256(seed).hexdigest()[:20]
    return row


def read_csv_rows(path: Path, expected_columns: Sequence[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise ScoringError(f"CSV file does not exist: {path}")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(expected_columns):
            raise ScoringError(
                f"unexpected CSV header in {path.name}; expected: {','.join(expected_columns)}"
            )
        return [dict(row) for row in reader]


def ingest_evidence(
    input_path: Path,
    output_path: Path,
    *,
    input_format: str,
    mappings: dict[str, str],
    run_id: str,
    iteration: int,
    evidence_type: str,
    default_sensor: str,
    default_rule: str,
) -> int:
    if not SAFE_ID_RE.fullmatch(run_id):
        raise ScoringError("run_id must be a short identifier without spaces or path characters")
    if iteration not in {1, 2, 3}:
        raise ScoringError("replay iteration must be 1, 2, or 3")
    if evidence_type not in EVIDENCE_TYPES:
        raise ScoringError(f"unknown evidence type: {evidence_type}")
    if default_rule:
        default_rule = canonical_rule(default_rule)
        if default_rule not in KNOWN_RULES:
            raise ScoringError(f"unknown default rule: {default_rule}")
    records = read_export_records(input_path, input_format)
    if not records:
        raise ScoringError("evidence export contains no records")
    source_sha = sha256_file(input_path)
    new_rows = [
        normalize_evidence_record(
            record,
            mappings=mappings,
            run_id=run_id,
            iteration=iteration,
            evidence_type=evidence_type,
            default_sensor=default_sensor,
            default_rule=default_rule,
            source_name=input_path.name,
            source_sha=source_sha,
            record_number=index,
        )
        for index, record in enumerate(records, start=1)
    ]
    existing = read_csv_rows(output_path, EVIDENCE_COLUMNS) if output_path.exists() else []
    existing_ids = {row["evidence_id"] for row in existing}
    duplicate = next((row["evidence_id"] for row in new_rows if row["evidence_id"] in existing_ids), None)
    if duplicate:
        raise ScoringError(f"evidence import already present: {duplicate}")
    write_csv(output_path, EVIDENCE_COLUMNS, [*existing, *new_rows])
    return len(new_rows)


def number(value: Any, location: str) -> float:
    if isinstance(value, bool):
        raise ScoringError(f"{location} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ScoringError(f"{location} must be numeric") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise ScoringError(f"{location} must be finite")
    return result


def validate_interval(start: float, end: float, duration: float, location: str) -> None:
    if start < 0 or end <= start or end > duration:
        raise ScoringError(
            f"{location} interval must satisfy 0 <= start < end <= {duration:g}"
        )


def bool_field(value: str, location: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "1", "pass", "passed", "ok"}:
        return True
    if lowered in {"false", "no", "0", "fail", "failed"}:
        return False
    raise ScoringError(f"{location} must be true or false")


def parse_utc_timestamp(value: str, location: str) -> dt.datetime:
    text = value.strip()
    if not text:
        raise ScoringError(f"{location} must contain a UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScoringError(f"{location} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ScoringError(f"{location} must include UTC timezone information")
    return parsed


def load_reviewer_labels(
    paths: Sequence[Path], manifest: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    if len(paths) != 2 or len(set(paths)) != 2:
        raise ScoringError("exactly two distinct reviewer label files are required")
    duration = float(manifest["source_video_profile"]["duration_seconds"])
    cameras = {
        camera["nvidia_sensor_id"]: camera["source_file"] for camera in manifest["cameras"]
    }
    candidates: dict[str, dict[str, Any]] = {}
    reviewer_ids: set[str] = set()
    for path in paths:
        rows = read_csv_rows(path, HUMAN_LABEL_COLUMNS)
        file_reviewers = {row["reviewer_id"].strip() for row in rows if row["reviewer_id"].strip()}
        if len(file_reviewers) != 1:
            raise ScoringError(f"{path.name} must contain exactly one reviewer_id")
        reviewer_id = next(iter(file_reviewers))
        if reviewer_id in reviewer_ids:
            raise ScoringError(f"reviewer_id is reused across files: {reviewer_id}")
        reviewer_ids.add(reviewer_id)
        timeline_sensors: set[str] = set()
        for line_number, row in enumerate(rows, start=2):
            if row["reviewer_id"].strip() != reviewer_id:
                raise ScoringError(f"mixed reviewer IDs in {path.name}:{line_number}")
            record_type = row["record_type"].strip()
            sensor = row["sensor_id"].strip()
            if sensor not in cameras:
                raise ScoringError(f"unknown sensor in {path.name}:{line_number}: {sensor}")
            if row["source_file"].strip() != cameras[sensor]:
                raise ScoringError(f"source file does not match sensor in {path.name}:{line_number}")
            start = number(row["start_seconds"], f"{path.name}:{line_number}.start_seconds")
            end = number(row["end_seconds"], f"{path.name}:{line_number}.end_seconds")
            validate_interval(start, end, duration, f"{path.name}:{line_number}")
            if record_type == "timeline_review":
                if sensor in timeline_sensors:
                    raise ScoringError(f"duplicate timeline review for {sensor} in {path.name}")
                timeline_sensors.add(sensor)
                if start != 0 or end != duration:
                    raise ScoringError(f"timeline review for {sensor} must cover 0-{duration:g}")
                if not bool_field(
                    row["timeline_reviewed"], f"{path.name}:{line_number}.timeline_reviewed"
                ):
                    raise ScoringError(f"timeline review is incomplete for {sensor} in {path.name}")
                parse_utc_timestamp(
                    row["review_completed_utc"],
                    f"{path.name}:{line_number}.review_completed_utc",
                )
                continue
            if record_type != "candidate":
                raise ScoringError(f"invalid record_type in {path.name}:{line_number}: {record_type}")
            identifier = row["label_id"].strip()
            if not identifier or identifier in candidates:
                raise ScoringError(f"empty or duplicate label_id in {path.name}:{line_number}")
            rule = canonical_rule(row["rule"])
            if rule not in STOCK_RULES:
                raise ScoringError(f"candidate {identifier} must use a stock alert rule")
            label = row["label"].strip().lower()
            if label not in {"positive", "negative", "uncertain"}:
                raise ScoringError(f"invalid candidate label for {identifier}: {label}")
            if not row["visible_facts"].strip():
                raise ScoringError(f"candidate {identifier} requires concise visible facts")
            candidates[identifier] = {
                **row,
                "label_id": identifier,
                "reviewer_id": reviewer_id,
                "sensor_id": sensor,
                "rule": rule,
                "label": label,
                "start": start,
                "end": end,
            }
        missing = set(cameras) - timeline_sensors
        if missing:
            raise ScoringError(
                f"{path.name} is missing completed timeline reviews for: {', '.join(sorted(missing))}"
            )
    return candidates, reviewer_ids


def load_ground_truth(
    path: Path,
    manifest: dict[str, Any],
    reviewer_candidates: dict[str, dict[str, Any]],
    reviewer_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    duration = float(manifest["source_video_profile"]["duration_seconds"])
    sensors = {camera["nvidia_sensor_id"] for camera in manifest["cameras"]}
    rows = read_csv_rows(path, ADJUDICATED_COLUMNS)
    accepted: dict[str, dict[str, Any]] = {}
    all_rows: list[dict[str, Any]] = []
    for line_number, row in enumerate(rows, start=2):
        identifier = row["ground_truth_id"].strip()
        if not identifier:
            raise ScoringError(f"missing ground_truth_id on {path.name}:{line_number}")
        if identifier in {item["ground_truth_id"] for item in all_rows}:
            raise ScoringError(f"duplicate ground_truth_id: {identifier}")
        sensor = row["sensor_id"].strip()
        if sensor not in sensors:
            raise ScoringError(f"unknown sensor for {identifier}: {sensor}")
        rule = canonical_rule(row["rule"])
        if rule not in STOCK_RULES:
            raise ScoringError(f"ground truth {identifier} must use a stock alert rule")
        label = row["label"].strip().lower()
        if label not in {"positive", "negative", "uncertain"}:
            raise ScoringError(f"invalid label for {identifier}: {label}")
        start = number(row["start_seconds"], f"{identifier}.start_seconds")
        end = number(row["end_seconds"], f"{identifier}.end_seconds")
        validate_interval(start, end, duration, identifier)
        status = row["adjudication_status"].strip().lower()
        if status not in {"accepted", "excluded"}:
            raise ScoringError(f"invalid adjudication_status for {identifier}: {status}")
        if status == "accepted" and label == "positive" and not row["visible_facts"].strip():
            raise ScoringError(f"accepted positive {identifier} requires concise visible facts")
        if status == "accepted":
            source_ids = split_ids(row["reviewer_label_ids"])
            source_labels: list[dict[str, Any]] = []
            for source_id in source_ids:
                source = reviewer_candidates.get(source_id)
                if source is None:
                    raise ScoringError(
                        f"accepted ground truth {identifier} references unknown reviewer label {source_id}"
                    )
                source_labels.append(source)
            contributing_reviewers = {item["reviewer_id"] for item in source_labels}
            if contributing_reviewers != reviewer_ids:
                raise ScoringError(
                    f"accepted ground truth {identifier} must reference a label from both reviewers"
                )
            for source in source_labels:
                if source["sensor_id"] != sensor or source["rule"] != rule:
                    raise ScoringError(
                        f"reviewer label {source['label_id']} does not match {identifier}'s sensor/rule"
                    )
                if max(0.0, min(end, source["end"]) - max(start, source["start"])) < 1.0:
                    raise ScoringError(
                        f"reviewer label {source['label_id']} overlaps {identifier} by less than one second"
                    )
            parse_utc_timestamp(row["adjudicated_utc"], f"{identifier}.adjudicated_utc")
            if not split_ids(row["adjudicator_ids"]):
                raise ScoringError(f"accepted ground truth {identifier} requires adjudicator_ids")
        normalized = dict(row)
        normalized.update(
            {
                "ground_truth_id": identifier,
                "sensor_id": sensor,
                "rule": rule,
                "label": label,
                "start": start,
                "end": end,
                "adjudication_status": status,
            }
        )
        all_rows.append(normalized)
        if status == "accepted":
            accepted[identifier] = normalized
    return accepted, all_rows


def validate_run_metadata(
    path: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    expected_run_ids: set[str],
) -> dict[str, Any]:
    if not path.is_file():
        raise ScoringError(f"run metadata does not exist: {path}")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScoringError(f"invalid JSON run metadata: {path.name}") from exc
    if not isinstance(metadata, dict):
        raise ScoringError("run metadata root must be an object")

    errors: list[str] = []
    if metadata.get("status") != "ready_for_scoring":
        errors.append("run metadata status is not ready_for_scoring")
    if metadata.get("manifest_sha256") != sha256_file(manifest_path):
        errors.append("run metadata manifest SHA-256 does not match the scored manifest")
    if metadata.get("dataset") != manifest["dataset"]["name"]:
        errors.append("run metadata dataset does not match the manifest")
    if metadata.get("profile_commit") != manifest["nvidia_profile"]["commit"]:
        errors.append("run metadata profile commit does not match the manifest")
    for field in (
        "source_hashes_verified",
        "configuration_hashes_verified",
        "archive_hash_verified",
    ):
        if metadata.get(field) is not True:
            errors.append(f"{field} is not true")

    expected_archive = manifest["dataset"]["archive"]
    if metadata.get("dataset_archive") != expected_archive:
        errors.append("dataset archive identity does not match the manifest")
    expected_sources = {
        camera["nvidia_sensor_id"]: {
            "sensor_id": camera["nvidia_sensor_id"],
            "source_file": camera["source_file"],
            "byte_size": camera["source_byte_size"],
            "sha256": camera["source_sha256"],
        }
        for camera in manifest["cameras"]
    }
    recorded_sources = metadata.get("source_videos")
    if not isinstance(recorded_sources, list):
        errors.append("source_videos is not a list")
    else:
        keyed_sources = {
            item.get("sensor_id"): item for item in recorded_sources if isinstance(item, dict)
        }
        if keyed_sources != expected_sources:
            errors.append("source video identities do not match the manifest")

    expected_locks = {
        name: {"name": name, "path": lock["path"], "sha256": lock["sha256"]}
        for name, lock in manifest["nvidia_profile"]["configuration_locks"].items()
    }
    recorded_locks = metadata.get("configuration_locks")
    if not isinstance(recorded_locks, list):
        errors.append("configuration_locks is not a list")
    else:
        keyed_locks = {
            item.get("name"): item for item in recorded_locks if isinstance(item, dict)
        }
        if keyed_locks != expected_locks:
            errors.append("configuration lock identities do not match the manifest")

    images = metadata.get("image_digests")
    if not isinstance(images, list) or not images:
        errors.append("image_digests is empty")
    else:
        components: set[str] = set()
        for index, item in enumerate(images):
            if not isinstance(item, dict):
                errors.append(f"image_digests[{index}] is not an object")
                continue
            component = str(item.get("component", "")).strip()
            image = str(item.get("image", "")).strip()
            if not component or component in components:
                errors.append(f"image_digests[{index}] has an empty or duplicate component")
            components.add(component)
            if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
                errors.append(f"image_digests[{index}] is not an immutable image digest")

    models = metadata.get("model_ids")
    if not isinstance(models, list) or len(models) < 3:
        errors.append("model_ids must record at least perception, VLM, and Agent LLM models")
    else:
        roles: set[str] = set()
        for index, item in enumerate(models):
            if not isinstance(item, dict):
                errors.append(f"model_ids[{index}] is not an object")
                continue
            role = str(item.get("role", "")).strip()
            model_id = str(item.get("model_id", "")).strip()
            if not role or role in roles or not model_id:
                errors.append(f"model_ids[{index}] has an empty/duplicate role or empty model_id")
            roles.add(role)

    recorded_run_ids = metadata.get("run_ids")
    if not isinstance(recorded_run_ids, list) or any(
        not isinstance(item, str) or not SAFE_ID_RE.fullmatch(item) for item in recorded_run_ids
    ):
        errors.append("run_ids must be a list of safe run identifiers")
        normalized_run_ids: set[str] = set()
    else:
        normalized_run_ids = set(recorded_run_ids)
        if len(normalized_run_ids) != len(recorded_run_ids):
            errors.append("run_ids contains duplicates")
    if normalized_run_ids != expected_run_ids:
        errors.append("run_ids does not exactly match the scenario evidence runs")

    return {"passed": not errors, "errors": errors}


def load_evidence(
    path: Path, manifest: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    duration = float(manifest["source_video_profile"]["duration_seconds"])
    sensors = {camera["nvidia_sensor_id"] for camera in manifest["cameras"]}
    rows = read_csv_rows(path, EVIDENCE_COLUMNS)
    by_id: dict[str, dict[str, Any]] = {}
    normalized_rows: list[dict[str, Any]] = []
    for line_number, row in enumerate(rows, start=2):
        identifier = row["evidence_id"].strip()
        if not identifier:
            raise ScoringError(f"missing evidence_id on {path.name}:{line_number}")
        if identifier in by_id:
            raise ScoringError(f"duplicate evidence_id: {identifier}")
        run_id = row["run_id"].strip()
        if not run_id:
            raise ScoringError(f"evidence {identifier} has no run_id")
        try:
            iteration = int(row["replay_iteration"])
        except ValueError as exc:
            raise ScoringError(f"evidence {identifier} has invalid replay_iteration") from exc
        if iteration not in {1, 2, 3}:
            raise ScoringError(f"evidence {identifier} replay_iteration must be 1, 2, or 3")
        evidence_type = row["evidence_type"].strip()
        if evidence_type not in EVIDENCE_TYPES:
            raise ScoringError(f"evidence {identifier} has invalid evidence_type")
        sensor = row["sensor_id"].strip()
        if sensor and sensor not in sensors:
            raise ScoringError(f"evidence {identifier} has unknown sensor: {sensor}")
        rule = canonical_rule(row["rule"]) if row["rule"].strip() else ""
        if rule and rule not in KNOWN_RULES:
            raise ScoringError(f"evidence {identifier} has unknown rule: {rule}")
        start: float | None = None
        end: float | None = None
        if row["source_start_seconds"].strip() or row["source_end_seconds"].strip():
            if not (row["source_start_seconds"].strip() and row["source_end_seconds"].strip()):
                raise ScoringError(f"evidence {identifier} must contain both interval endpoints")
            start = number(row["source_start_seconds"], f"{identifier}.source_start_seconds")
            end = number(row["source_end_seconds"], f"{identifier}.source_end_seconds")
            validate_interval(start, end, duration, identifier)
        normalized = dict(row)
        normalized.update(
            {
                "evidence_id": identifier,
                "run_id": run_id,
                "iteration": iteration,
                "evidence_type": evidence_type,
                "sensor_id": sensor,
                "rule": rule,
                "start": start,
                "end": end,
            }
        )
        by_id[identifier] = normalized
        normalized_rows.append(normalized)
    return by_id, normalized_rows


def overlap_seconds(left: dict[str, Any], right: dict[str, Any]) -> float:
    if left.get("start") is None or right.get("start") is None:
        return 0.0
    return max(0.0, min(left["end"], right["end"]) - max(left["start"], right["start"]))


def is_positive_prediction(row: dict[str, Any]) -> bool:
    rule = row.get("rule", "")
    verdict = row.get("verdict", "").strip().lower()
    class_id = row.get("class_id", "").strip().lower()
    class_label = row.get("class_label", "").strip().lower()
    if rule == "near_miss":
        return verdict == "confirmed"
    if rule in STOCK_RULES:
        return class_id == "0" and class_label == "yes"
    return False


def classify_nonpositive(row: dict[str, Any]) -> str:
    combined = " ".join(
        (row.get("status", ""), row.get("verdict", ""), row.get("class_label", ""))
    ).lower()
    if "reject" in combined or " no" in f" {combined}":
        return "rejected"
    return "unverified_or_failed"


def compute_metrics(
    ground_truth: dict[str, dict[str, Any]], evidence_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    positives = [row for row in ground_truth.values() if row["label"] == "positive"]
    final_rows = [row for row in evidence_rows if row["evidence_type"] == "alerts_ui"]
    details: list[dict[str, Any]] = []
    aggregate = {"tp": 0, "fp": 0, "fn": 0, "duplicates": 0, "rejected": 0, "unverified_or_failed": 0}
    for iteration in (1, 2, 3):
        groups = {
            (label["rule"], label["sensor_id"]) for label in positives
        } | {
            (row["rule"], row["sensor_id"])
            for row in final_rows
            if row["iteration"] == iteration and row["rule"] and row["sensor_id"]
        }
        for rule, sensor in sorted(groups):
            labels = [
                label for label in positives if label["rule"] == rule and label["sensor_id"] == sensor
            ]
            rows = [
                row
                for row in final_rows
                if row["iteration"] == iteration
                and row["rule"] == rule
                and row["sensor_id"] == sensor
            ]
            predictions = [row for row in rows if is_positive_prediction(row)]
            candidates: list[tuple[float, str, str]] = []
            label_by_id = {row["ground_truth_id"]: row for row in labels}
            for label in labels:
                for prediction in predictions:
                    overlap = overlap_seconds(label, prediction)
                    if overlap >= 1.0:
                        candidates.append(
                            (overlap, label["ground_truth_id"], prediction["evidence_id"])
                        )
            matched_labels: set[str] = set()
            matched_predictions: set[str] = set()
            for _, label_id, prediction_id in sorted(candidates, reverse=True):
                if label_id not in matched_labels and prediction_id not in matched_predictions:
                    matched_labels.add(label_id)
                    matched_predictions.add(prediction_id)
            unmatched_predictions = [
                row for row in predictions if row["evidence_id"] not in matched_predictions
            ]
            duplicates = sum(
                1
                for prediction in unmatched_predictions
                if any(overlap_seconds(label, prediction) >= 1.0 for label in label_by_id.values())
            )
            rejected = sum(1 for row in rows if classify_nonpositive(row) == "rejected")
            unverified = len(rows) - len(predictions) - rejected
            tp = len(matched_labels)
            fp = len(unmatched_predictions)
            fn = len(labels) - tp
            precision = tp / (tp + fp) if tp + fp else None
            recall = tp / (tp + fn) if tp + fn else None
            latencies = [
                float(row["latency_ms"])
                for row in rows
                if row["latency_ms"].strip() and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", row["latency_ms"])
            ]
            detail = {
                "iteration": iteration,
                "rule": rule,
                "sensor_id": sensor,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "precision": precision,
                "recall": recall,
                "duplicates": duplicates,
                "rejected": rejected,
                "unverified_or_failed": unverified,
                "latency_ms_mean": sum(latencies) / len(latencies) if latencies else None,
            }
            details.append(detail)
            for key in aggregate:
                aggregate[key] += detail[key]
    aggregate["precision"] = (
        aggregate["tp"] / (aggregate["tp"] + aggregate["fp"])
        if aggregate["tp"] + aggregate["fp"]
        else None
    )
    aggregate["recall"] = (
        aggregate["tp"] / (aggregate["tp"] + aggregate["fn"])
        if aggregate["tp"] + aggregate["fn"]
        else None
    )
    return {"aggregate": aggregate, "by_iteration_rule_camera": details}


def split_ids(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;|]", value) if item.strip()]


def evidence_ref(
    row: dict[str, str],
    column: str,
    by_id: dict[str, dict[str, Any]],
    errors: list[str],
    *,
    expected_type: str,
    many: bool = False,
    required: bool = True,
) -> list[dict[str, Any]]:
    identifiers = split_ids(row[column]) if many else ([row[column].strip()] if row[column].strip() else [])
    if required and not identifiers:
        errors.append(f"missing {column}")
        return []
    result: list[dict[str, Any]] = []
    for identifier in identifiers:
        item = by_id.get(identifier)
        if item is None:
            errors.append(f"unknown evidence ID {identifier} in {column}")
            continue
        if item["evidence_type"] != expected_type:
            errors.append(
                f"{identifier} is {item['evidence_type']}, expected {expected_type} for {column}"
            )
            continue
        result.append(item)
    return result


def evaluate_scenarios(
    scenario_path: Path,
    ground_truth: dict[str, dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows = read_csv_rows(scenario_path, SCENARIO_RUN_COLUMNS)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    observed_run_ids: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        scenario_id = row["scenario_id"].strip()
        if not scenario_id:
            raise ScoringError(f"missing scenario_id on {scenario_path.name}:{line_number}")
        grouped[scenario_id].append(row)
        if row["run_id"].strip():
            observed_run_ids.add(row["run_id"].strip())

    results: list[dict[str, Any]] = []
    for scenario_id, scenario_rows in sorted(grouped.items()):
        errors: list[str] = []
        ground_truth_ids = {row["ground_truth_id"].strip() for row in scenario_rows}
        if len(ground_truth_ids) != 1 or "" in ground_truth_ids:
            errors.append("all three rows must reference one non-empty ground_truth_id")
            ground_truth_row = None
        else:
            ground_truth_id = next(iter(ground_truth_ids))
            ground_truth_row = ground_truth.get(ground_truth_id)
            if ground_truth_row is None:
                errors.append(f"unknown or non-accepted ground_truth_id: {ground_truth_id}")
            elif ground_truth_row["label"] != "positive":
                errors.append("3/3 gate requires an accepted positive ground-truth event")

        seen_iterations: list[int] = []
        seen_run_ids: set[str] = set()
        verdict_keys: list[str] = []
        for row_index, row in enumerate(scenario_rows, start=1):
            prefix = f"row {row_index}"
            try:
                iteration = int(row["replay_iteration"])
            except ValueError:
                errors.append(f"{prefix}: invalid replay_iteration")
                continue
            seen_iterations.append(iteration)
            run_id = row["run_id"].strip()
            if not run_id:
                errors.append(f"{prefix}: missing run_id")
            elif run_id in seen_run_ids:
                errors.append(f"{prefix}: run_id is reused")
            seen_run_ids.add(run_id)

            rule = ground_truth_row["rule"] if ground_truth_row else ""
            required_behavior = rule == "near_miss"
            references: list[dict[str, Any]] = []
            explicit_prediction = evidence_ref(
                row,
                "prediction_evidence_id",
                evidence_by_id,
                errors,
                expected_type="alerts_ui",
            )
            references += explicit_prediction
            references += evidence_ref(
                row,
                "perception_evidence_ids",
                evidence_by_id,
                errors,
                expected_type="perception",
                many=True,
                required=required_behavior,
            )
            behavior = evidence_ref(
                row,
                "behavior_evidence_ids",
                evidence_by_id,
                errors,
                expected_type="behavior",
                many=True,
                required=required_behavior,
            )
            references += behavior
            vlm = evidence_ref(
                row,
                "vlm_evidence_ids",
                evidence_by_id,
                errors,
                expected_type="vlm",
                many=True,
            )
            references += vlm
            delivery = evidence_ref(
                row,
                "delivery_evidence_id",
                evidence_by_id,
                errors,
                expected_type="delivery",
            )
            references += delivery
            alerts_ui = evidence_ref(
                row,
                "alerts_ui_evidence_id",
                evidence_by_id,
                errors,
                expected_type="alerts_ui",
            )
            references += alerts_ui
            clip = evidence_ref(
                row,
                "clip_evidence_id",
                evidence_by_id,
                errors,
                expected_type="clip",
            )
            references += clip
            report = evidence_ref(
                row,
                "agent_report_evidence_id",
                evidence_by_id,
                errors,
                expected_type="agent_report",
            )
            references += report
            health = evidence_ref(
                row,
                "health_evidence_id",
                evidence_by_id,
                errors,
                expected_type="health",
            )
            references += health

            for item in references:
                if item["run_id"] != run_id or item["iteration"] != iteration:
                    errors.append(
                        f"{prefix}: {item['evidence_id']} belongs to a different run or iteration"
                    )
                if ground_truth_row:
                    if item["sensor_id"] and item["sensor_id"] != ground_truth_row["sensor_id"]:
                        errors.append(f"{prefix}: {item['evidence_id']} has the wrong sensor")
                    if item["rule"] and item["rule"] != ground_truth_row["rule"]:
                        errors.append(f"{prefix}: {item['evidence_id']} has the wrong rule")

            prediction = alerts_ui[0] if alerts_ui else None
            if prediction and explicit_prediction and prediction["evidence_id"] != explicit_prediction[0]["evidence_id"]:
                errors.append(f"{prefix}: prediction and Alerts UI evidence IDs differ")
            if prediction:
                if not is_positive_prediction(prediction):
                    errors.append(f"{prefix}: Alerts UI evidence is not a positive stock-rule result")
                if ground_truth_row and overlap_seconds(ground_truth_row, prediction) < 1.0:
                    errors.append(f"{prefix}: Alerts UI interval overlaps ground truth by less than one second")
                verdict_keys.append(
                    f"{prediction.get('verdict', '').strip().lower()}|"
                    f"{prediction.get('class_id', '').strip().lower()}|"
                    f"{prediction.get('class_label', '').strip().lower()}"
                )
            if clip and not bool_field(clip[0]["clip_playable"], f"{prefix}.clip_playable"):
                errors.append(f"{prefix}: evidence clip is not marked playable")
            if report and not bool_field(report[0]["report_grounded"], f"{prefix}.report_grounded"):
                errors.append(f"{prefix}: Agent report is not marked grounded")
            if health and not bool_field(health[0]["health_ok"], f"{prefix}.health_ok"):
                errors.append(f"{prefix}: health evidence is not acceptable")
            if delivery:
                delivery_status = delivery[0]["status"].strip().lower()
                if delivery_status not in {"delivered", "indexed", "success", "visible", "confirmed"}:
                    errors.append(f"{prefix}: delivery status is not successful")

            alert_ids = {
                item["alert_id"].strip()
                for item in [*delivery, *alerts_ui, *clip, *report]
                if item["alert_id"].strip()
            }
            if len(alert_ids) != 1:
                errors.append(f"{prefix}: delivery, UI, clip, and report need one shared alert_id")
            if required_behavior:
                incident_ids = {
                    item["incident_id"].strip()
                    for item in [*behavior, *vlm, *delivery]
                    if item["incident_id"].strip()
                }
                if len(incident_ids) != 1:
                    errors.append(f"{prefix}: near-miss behavior, VLM, and delivery need one incident_id")
                if vlm and vlm[0]["verdict"].strip().lower() != "confirmed":
                    errors.append(f"{prefix}: near-miss VLM verdict is not confirmed")
            elif ground_truth_row and ground_truth_row["rule"] in STOCK_RULES:
                if not vlm or not is_positive_prediction(vlm[0]):
                    errors.append(f"{prefix}: always-on VLM result is not class 0 / Yes")

        if sorted(seen_iterations) != [1, 2, 3] or len(seen_iterations) != 3:
            errors.append("scenario must contain exactly one row for each replay iteration 1, 2, and 3")
        if len(set(verdict_keys)) > 1:
            errors.append("verdict/class is not stable across all three runs")
        results.append(
            {
                "scenario_id": scenario_id,
                "ground_truth_id": next(iter(ground_truth_ids)) if len(ground_truth_ids) == 1 else None,
                "passed": not errors,
                "errors": errors,
            }
        )
    passed = [row["scenario_id"] for row in results if row["passed"]]
    return {
        "passed": bool(passed),
        "qualified_scenarios": passed,
        "scenario_results": results,
        "observed_run_ids": sorted(observed_run_ids),
        "message": (
            "At least one supported alert scenario passed the 3/3 evidence gate."
            if passed
            else "No alert scenario has passed the 3/3 evidence gate; no timestamp is qualified."
        ),
    }


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def markdown_report(result: dict[str, Any]) -> str:
    gate = result["reproducibility_gate"]
    metrics = result["metrics"]
    status = "PASS" if gate["passed"] else "NOT QUALIFIED"
    lines = [
        "# Warehouse alert evidence score",
        "",
        f"Result: **{status}**",
        "",
        gate["message"],
        "",
        "## Aggregate metrics",
        "",
        "| TP | FP | FN | Precision | Recall | Duplicates | Rejected | Unverified/failed |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    aggregate = metrics["aggregate"]
    lines.append(
        f"| {aggregate['tp']} | {aggregate['fp']} | {aggregate['fn']} | "
        f"{percent(aggregate['precision'])} | {percent(aggregate['recall'])} | "
        f"{aggregate['duplicates']} | {aggregate['rejected']} | "
        f"{aggregate['unverified_or_failed']} |"
    )
    lines.extend(["", "## Reproducibility scenarios", ""])
    if not gate["scenario_results"]:
        lines.append("No scenario-run claims were supplied.")
    else:
        lines.extend(
            [
                "| Scenario | Ground truth | Result | Reason |",
                "|---|---|---|---|",
            ]
        )
        for row in gate["scenario_results"]:
            reason = "Complete 3/3 chain" if row["passed"] else "; ".join(row["errors"])
            lines.append(
                f"| {row['scenario_id']} | {row['ground_truth_id'] or ''} | "
                f"{'PASS' if row['passed'] else 'FAIL'} | {reason.replace('|', '/')} |"
            )
    lines.extend(["", "## Input-lock gate", ""])
    if gate["input_lock"]["passed"]:
        lines.append("Manifest, dataset, configuration, image, model, and run identities are complete.")
    else:
        lines.extend(f"- {error}" for error in gate["input_lock"]["errors"])
    lines.extend(
        [
            "",
            "A failed or absent gate result must not be promoted to a verified manifest timestamp.",
            "",
        ]
    )
    return "\n".join(lines)


def score(
    manifest_path: Path,
    run_metadata_path: Path,
    reviewer_label_paths: Sequence[Path],
    ground_truth_path: Path,
    evidence_path: Path,
    scenario_path: Path,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    manifest_validation = validate_manifest_document(manifest)
    reviewer_candidates, reviewer_ids = load_reviewer_labels(reviewer_label_paths, manifest)
    ground_truth, all_ground_truth = load_ground_truth(
        ground_truth_path, manifest, reviewer_candidates, reviewer_ids
    )
    evidence_by_id, evidence_rows = load_evidence(evidence_path, manifest)
    metrics = compute_metrics(ground_truth, evidence_rows)
    gate = evaluate_scenarios(scenario_path, ground_truth, evidence_by_id)
    chain_qualified = list(gate["qualified_scenarios"])
    input_lock = validate_run_metadata(
        run_metadata_path, manifest_path, manifest, set(gate["observed_run_ids"])
    )
    gate["input_lock"] = input_lock
    gate["chain_qualified_scenarios"] = chain_qualified
    gate["passed"] = gate["passed"] and input_lock["passed"]
    gate["qualified_scenarios"] = chain_qualified if input_lock["passed"] else []
    gate["message"] = (
        "At least one supported alert scenario passed the locked-input 3/3 evidence gate."
        if gate["passed"]
        else "No alert scenario has passed the locked-input 3/3 evidence gate; no timestamp is qualified."
    )
    return {
        "schema_version": "1.0",
        "generated_utc": utc_now(),
        "dataset": manifest_validation["dataset"],
        "reviewer_count": len(reviewer_ids),
        "reviewer_candidate_records": len(reviewer_candidates),
        "ground_truth_records": len(all_ground_truth),
        "accepted_positive_ground_truth": sum(
            1 for row in ground_truth.values() if row["label"] == "positive"
        ),
        "evidence_records": len(evidence_rows),
        "metrics": metrics,
        "reproducibility_gate": gate,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and score local NVIDIA Warehouse alert evidence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-manifest", help="validate schema and optional files")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--media-root", type=Path)
    validate.add_argument("--archive", type=Path)
    validate.add_argument("--profile-root", type=Path)
    validate.add_argument("--json", action="store_true")

    initialize = subparsers.add_parser("init-workspace", help="create blank scoring templates")
    initialize.add_argument("--manifest", type=Path, required=True)
    initialize.add_argument("--output-dir", type=Path, required=True)
    initialize.add_argument(
        "--reviewer", action="append", dest="reviewers", metavar="ID", required=True
    )

    ingest = subparsers.add_parser(
        "ingest-evidence", help="normalize a CSV, JSON, or JSONL evidence export"
    )
    ingest.add_argument("--input", type=Path, required=True)
    ingest.add_argument("--output", type=Path, required=True)
    ingest.add_argument("--format", choices=("auto", "csv", "json", "jsonl"), default="auto")
    ingest.add_argument("--run-id", required=True)
    ingest.add_argument("--iteration", type=int, required=True)
    ingest.add_argument("--type", dest="evidence_type", choices=sorted(EVIDENCE_TYPES), required=True)
    ingest.add_argument("--sensor", default="")
    ingest.add_argument("--rule", default="")
    ingest.add_argument("--map", action="append", default=[], metavar="FIELD=PATH")

    scoring = subparsers.add_parser("score", help="score evidence and apply the 3/3 gate")
    scoring.add_argument("--manifest", type=Path, required=True)
    scoring.add_argument("--run-metadata", type=Path, required=True)
    scoring.add_argument(
        "--reviewer-labels",
        type=Path,
        action="append",
        required=True,
        metavar="CSV",
        help="provide exactly twice, once for each independent reviewer",
    )
    scoring.add_argument("--ground-truth", type=Path, required=True)
    scoring.add_argument("--evidence", type=Path, required=True)
    scoring.add_argument("--scenario-runs", type=Path, required=True)
    scoring.add_argument("--output-json", type=Path, required=True)
    scoring.add_argument("--output-markdown", type=Path, required=True)
    scoring.add_argument("--require-pass", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-manifest":
            manifest = load_manifest(args.manifest)
            result = validate_manifest_document(
                manifest,
                media_root=args.media_root,
                archive_path=args.archive,
                profile_root=args.profile_root,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    "PASS: manifest schema is valid; "
                    f"media hashes {result['media_hashes_checked']}/{result['camera_count']}, "
                    f"configuration hashes {result['configuration_hashes_checked']}/5, "
                    f"archive hash {'1/1' if result['archive_hash_checked'] else '0/1'} checked"
                )
            return 0
        if args.command == "init-workspace":
            paths = init_workspace(args.manifest, args.output_dir, args.reviewers)
            print(f"Created {len(paths)} scoring files in {args.output_dir}")
            return 0
        if args.command == "ingest-evidence":
            count = ingest_evidence(
                args.input,
                args.output,
                input_format=args.format,
                mappings=parse_mapping(args.map),
                run_id=args.run_id,
                iteration=args.iteration,
                evidence_type=args.evidence_type,
                default_sensor=args.sensor,
                default_rule=args.rule,
            )
            print(f"Imported {count} normalized evidence record(s) into {args.output}")
            return 0
        if args.command == "score":
            result = score(
                args.manifest,
                args.run_metadata,
                args.reviewer_labels,
                args.ground_truth,
                args.evidence,
                args.scenario_runs,
            )
            atomic_write_text(args.output_json, json.dumps(result, indent=2, sort_keys=True) + "\n")
            atomic_write_text(args.output_markdown, markdown_report(result))
            gate_passed = result["reproducibility_gate"]["passed"]
            print(
                ("PASS" if gate_passed else "NOT QUALIFIED")
                + f": {result['reproducibility_gate']['message']}"
            )
            return 0 if gate_passed or not args.require_pass else 2
    except (OSError, ScoringError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
