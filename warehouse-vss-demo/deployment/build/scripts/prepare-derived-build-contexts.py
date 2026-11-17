#!/usr/bin/env python3
"""Verify NVIDIA VSS source and stage deterministic Warehouse build contexts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_DIR = (
    SCRIPT_DIR.parent
)
LOCK_FILE = BUILD_DIR / "derived-images.lock.json"
DOCKERFILE_DIR = BUILD_DIR / "dockerfiles"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, expected: str, label: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"missing {label}: {path}")
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(
            f"{label} checksum mismatch: sha256:{actual}; expected sha256:{expected}"
        )


def replace_exact(path: Path, old: str, new: str, expected_count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    actual_count = text.count(old)
    if actual_count != expected_count:
        raise RuntimeError(
            f"adaptation precondition failed for {path}: expected {expected_count} "
            f"occurrence(s), found {actual_count}"
        )
    path.write_text(text.replace(old, new), encoding="utf-8")


def copy_source(source_root: Path, relative: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_root / relative, destination)


def context_manifest(context: Path, source_commit: str, image_key: str) -> None:
    files = {}
    for path in sorted(context.rglob("*")):
        if path.is_file() and path.name != "context-manifest.json":
            files[str(path.relative_to(context))] = sha256_file(path)
    payload = {
        "imageKey": image_key,
        "sourceCommit": source_commit,
        "files": files,
    }
    (context / "context-manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage verified NVIDIA VSS Warehouse derived-image contexts."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.output.exists():
        if not args.output.is_dir():
            print(f"FAIL: output path is not a directory: {args.output}", file=sys.stderr)
            return 1
        if any(args.output.iterdir()):
            print(f"FAIL: output directory is not empty: {args.output}", file=sys.stderr)
            return 1
    args.output.mkdir(parents=True, exist_ok=True)

    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    expected_commit = lock["source"]["commit"]
    try:
        observed_commit = subprocess.run(
            ["git", "-C", str(args.source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        print(f"FAIL: source is not a readable Git checkout: {error}", file=sys.stderr)
        return 1
    if observed_commit != expected_commit:
        print(
            f"FAIL: NVIDIA source is {observed_commit}; expected {expected_commit}",
            file=sys.stderr,
        )
        return 1

    source_root = args.source / lock["source"]["contextRoot"]
    try:
        for relative, expected in lock["sourceFiles"].items():
            verify_file(source_root / relative, expected, f"NVIDIA source {relative}")
        for relative, expected in lock["buildFiles"].items():
            verify_file(BUILD_DIR / relative, expected, f"local build file {relative}")
        for filename, metadata in lock["artifacts"].items():
            verify_file(
                args.artifacts / filename,
                metadata["sha256"],
                f"third-party artifact {filename}",
            )
    except RuntimeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    output_by_key = {item["key"]: item for item in lock["outputs"]}
    for item in lock["outputs"]:
        context = args.output / item["key"]
        context.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DOCKERFILE_DIR / item["dockerfile"], context / "Dockerfile")

    kafka = args.output / "kafka-health-check"
    copy_source(
        source_root,
        "services/infra/broker-health-check/scripts/check-kafka-health.sh",
        kafka / "source" / "check-kafka-health.sh",
    )
    replace_exact(
        kafka / "source" / "check-kafka-health.sh",
        "KAFKA_HOST=${BOOTSTRAP_HOST:-localhost}",
        "KAFKA_HOST=${BOOTSTRAP_HOST:?BOOTSTRAP_HOST is required}",
    )
    (kafka / "third-party").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        args.artifacts / "jq-linux-amd64",
        kafka / "third-party" / "jq-linux-amd64",
    )

    elastic_init = args.output / "elasticsearch-init"
    for filename in (
        "elasticsearch-ilm-policy-creation.sh",
        "elasticsearch-template-creation.sh",
        "elasticsearch-ingest-pipeline-creation.sh",
    ):
        copy_source(
            source_root,
            f"services/infra/elk/elasticsearch/init-scripts/{filename}",
            elastic_init / "source" / filename,
        )
    replace_exact(
        elastic_init / "source" / "elasticsearch-ilm-policy-creation.sh",
        'ELASTICSEARCH_URL="${ELASTICSEARCH_URL:-${ES_URL:-http://localhost:9200}}"',
        'ELASTICSEARCH_URL="${ELASTICSEARCH_URL:?ELASTICSEARCH_URL is required}"',
    )
    for filename in (
        "elasticsearch-template-creation.sh",
        "elasticsearch-ingest-pipeline-creation.sh",
    ):
        replace_exact(
            elastic_init / "source" / filename,
            'ELASTICSEARCH_URL="${ELASTICSEARCH_URL:-http://localhost:9200}"',
            'ELASTICSEARCH_URL="${ELASTICSEARCH_URL:?ELASTICSEARCH_URL is required}"',
        )
    replace_exact(
        elastic_init / "source" / "elasticsearch-ilm-policy-creation.sh",
        "          --insecure 2>&1)",
        "          2>&1)",
    )
    for filename in (
        "elasticsearch-template-creation.sh",
        "elasticsearch-ingest-pipeline-creation.sh",
    ):
        replace_exact(
            elastic_init / "source" / filename,
            "      --insecure)",
            "      )",
        )

    kibana = args.output / "kibana-dashboard-init"
    copy_source(
        source_root,
        "industry-profiles/warehouse-operations/warehouse-2d-app/"
        "kibana-dashboard/init-scripts/kibana-import-dashboard.sh",
        kibana / "source" / "kibana-import-dashboard.sh",
    )
    replace_exact(
        kibana / "source" / "kibana-import-dashboard.sh",
        "    curl -X POST localhost:5601/kibana/api/saved_objects/_import?overwrite=true \\",
        "    curl -fsS -X POST \\\n"
        '    "${KIBANA_URL:?KIBANA_URL is required}/kibana/api/saved_objects/_import?overwrite=true" \\',
    )

    calibration = args.output / "calibration-import"
    copy_source(
        source_root,
        "industry-profiles/warehouse-operations/warehouse-2d-app/"
        "import-calibration/init-scripts/calibration-import.sh",
        calibration / "source" / "calibration-import.sh",
    )
    replace_exact(
        calibration / "source" / "calibration-import.sh",
        'video_analytics_api_url="http://localhost:8081"',
        'video_analytics_api_url="${VIDEO_ANALYTICS_API_URL:?VIDEO_ANALYTICS_API_URL is required}"',
    )
    (calibration / "third-party").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        args.artifacts / "jq-linux-amd64",
        calibration / "third-party" / "jq-linux-amd64",
    )

    for key in output_by_key:
        context_manifest(args.output / key, expected_commit, key)
        print(f"PASS: staged {key}")

    print(
        f"PASS: {len(output_by_key)} verified build contexts staged under {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
