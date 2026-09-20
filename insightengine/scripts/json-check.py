#!/usr/bin/env python3
"""Small, fixed-purpose JSON assertions for the InsightEngine CVD companion.

The helper reads one Kubernetes or Helm JSON document from standard input and
returns a non-zero status when the requested contract is not satisfied.  It
never prints Secret values.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any, Iterable


FAILED_WAIT_REASONS = {
    "CrashLoopBackOff",
    "ErrImagePull",
    "ImagePullBackOff",
    "CreateContainerConfigError",
    "CreateContainerError",
}


def read_json() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SystemExit(f"invalid JSON input: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("expected a JSON object")
    return value


def items(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw = document.get("items")
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, dict)]
    return [document]


def condition_true(value: dict[str, Any], condition_type: str) -> bool:
    conditions = value.get("status", {}).get("conditions", [])
    return any(
        isinstance(condition, dict)
        and condition.get("type") == condition_type
        and condition.get("status") == "True"
        for condition in conditions
    )


def status_ready(value: dict[str, Any]) -> bool:
    status = value.get("status", {})
    state = str(status.get("phase", status.get("status", ""))).lower()
    return state == "ready"


def pod_spec(value: dict[str, Any]) -> dict[str, Any]:
    spec = value.get("spec", {})
    template = spec.get("template")
    if isinstance(template, dict):
        spec = template.get("spec", {})
    return spec if isinstance(spec, dict) else {}


def pod_images(value: dict[str, Any]) -> Iterable[str]:
    spec = pod_spec(value)
    for key in ("initContainers", "containers"):
        for container in spec.get(key, []) or []:
            if isinstance(container, dict) and isinstance(container.get("image"), str):
                yield container["image"]


def pod_pull_secret_names(value: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for entry in pod_spec(value).get("imagePullSecrets", []) or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            names.add(entry["name"])
    return names


def require_nonempty(values: list[dict[str, Any]]) -> None:
    if not values:
        raise SystemExit(1)


def command_pods_ready(document: dict[str, Any], _args: argparse.Namespace) -> None:
    values = items(document)
    require_nonempty(values)
    for pod in values:
        if pod.get("metadata", {}).get("deletionTimestamp") is not None:
            raise SystemExit(1)
        status = pod.get("status", {})
        phase = status.get("phase")
        if phase != "Succeeded" and not (phase == "Running" and condition_true(pod, "Ready")):
            raise SystemExit(1)
        statuses = (status.get("initContainerStatuses") or []) + (
            status.get("containerStatuses") or []
        )
        for container_status in statuses:
            waiting = container_status.get("state", {}).get("waiting", {})
            if waiting.get("reason") in FAILED_WAIT_REASONS:
                raise SystemExit(1)


def command_nodes_ready(document: dict[str, Any], _args: argparse.Namespace) -> None:
    values = items(document)
    require_nonempty(values)
    if not all(condition_true(value, "Ready") for value in values):
        raise SystemExit(1)


def command_clusteroperators_ready(
    document: dict[str, Any], _args: argparse.Namespace
) -> None:
    values = items(document)
    require_nonempty(values)
    for value in values:
        if not condition_true(value, "Available"):
            raise SystemExit(1)
        conditions = value.get("status", {}).get("conditions", [])
        degraded_false = any(
            condition.get("type") == "Degraded" and condition.get("status") == "False"
            for condition in conditions
            if isinstance(condition, dict)
        )
        if not degraded_false:
            raise SystemExit(1)


def command_controllers_ready(
    document: dict[str, Any], args: argparse.Namespace
) -> None:
    values = items(document)
    require_nonempty(values)
    for value in values:
        spec_replicas = value.get("spec", {}).get("replicas", 0) or 0
        status = value.get("status", {})
        if spec_replicas <= 0 or status.get("updatedReplicas", 0) != spec_replicas:
            raise SystemExit(1)
        ready_field = "availableReplicas" if args.kind == "deployment" else "readyReplicas"
        if status.get(ready_field, 0) != spec_replicas:
            raise SystemExit(1)


def command_pvcs_ready(document: dict[str, Any], args: argparse.Namespace) -> None:
    values = items(document)
    require_nonempty(values)
    if not all(
        value.get("status", {}).get("phase") == "Bound"
        and value.get("spec", {}).get("storageClassName") == args.storage_class
        for value in values
    ):
        raise SystemExit(1)


def command_helm_status(document: dict[str, Any], args: argparse.Namespace) -> None:
    if document.get("info", {}).get("status") != args.expected:
        raise SystemExit(1)


def command_helm_chart_version(
    document: dict[str, Any], _args: argparse.Namespace
) -> None:
    version = document.get("version")
    if not isinstance(version, str) or not version:
        version = document.get("chart", {}).get("metadata", {}).get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(1)
    print(version)


def command_pull_secret_valid(
    document: dict[str, Any], _args: argparse.Namespace
) -> None:
    encoded = document.get("data", {}).get(".dockerconfigjson")
    if document.get("type") != "kubernetes.io/dockerconfigjson" or not encoded:
        raise SystemExit(1)


def command_secret_has_keys(
    document: dict[str, Any], args: argparse.Namespace
) -> None:
    data = document.get("data", {})
    for key in args.keys.split(","):
        if not key or not isinstance(data.get(key), str) or not data[key]:
            raise SystemExit(1)


def command_secret_value_equals(
    document: dict[str, Any], args: argparse.Namespace
) -> None:
    encoded = document.get("data", {}).get(args.key)
    if not isinstance(encoded, str) or not encoded:
        raise SystemExit(1)
    try:
        actual = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise SystemExit(1) from exc
    if actual != args.expected:
        raise SystemExit(1)


def command_resources_ready(document: dict[str, Any], _args: argparse.Namespace) -> None:
    values = items(document)
    require_nonempty(values)
    if not all(status_ready(value) for value in values):
        raise SystemExit(1)


def command_resource_ready(document: dict[str, Any], _args: argparse.Namespace) -> None:
    if not status_ready(document):
        raise SystemExit(1)


def command_policy_name(document: dict[str, Any], args: argparse.Namespace) -> None:
    spec = document.get("spec", {})
    if spec.get("policyName", spec.get("name", "")) != args.expected:
        raise SystemExit(1)


def pipeline_matches(value: dict[str, Any], args: argparse.Namespace) -> bool:
    spec = value.get("spec", {})
    topic = spec.get("topic", {})
    image = spec.get("image", {})
    expected = {
        "topic": topic.get("name"),
        "broker_name": topic.get("broker"),
        "registry": spec.get("containerRegistryName"),
        "cluster": spec.get("kubernetesClusterName"),
        "image_repository": image.get("repository"),
        "image_tag": image.get("tag"),
    }
    requested = {
        "topic": args.topic,
        "broker_name": args.broker_name,
        "registry": args.registry,
        "cluster": args.cluster,
        "image_repository": args.image_repository,
        "image_tag": args.image_tag,
    }
    return expected == requested


def command_pipeline(document: dict[str, Any], args: argparse.Namespace) -> None:
    if not status_ready(document) or not pipeline_matches(document, args):
        raise SystemExit(1)


def command_single_item_name(
    document: dict[str, Any], _args: argparse.Namespace
) -> None:
    values = items(document)
    if len(values) != 1:
        raise SystemExit(1)
    name = values[0].get("metadata", {}).get("name")
    if not isinstance(name, str) or not name:
        raise SystemExit(1)
    print(name)


def command_latest_ready_revision(
    document: dict[str, Any], _args: argparse.Namespace
) -> None:
    name = document.get("status", {}).get("latestReadyRevisionName")
    if not isinstance(name, str) or not name:
        raise SystemExit(1)
    print(name)


def command_trigger_contract(
    document: dict[str, Any], args: argparse.Namespace
) -> None:
    values = items(document)
    if len(values) != 1 or not condition_true(values[0], "Ready"):
        raise SystemExit(1)
    spec = values[0].get("spec", {})
    subscriber = spec.get("subscriber", {}).get("ref", {})
    broker = spec.get("broker")
    if (
        not isinstance(broker, str)
        or not broker
        or subscriber.get("kind") != "Service"
        or subscriber.get("name") != args.service
    ):
        raise SystemExit(1)
    print(broker)


def command_broker_contract(
    document: dict[str, Any], args: argparse.Namespace
) -> None:
    if not condition_true(document, "Ready"):
        raise SystemExit(1)
    annotation = document.get("metadata", {}).get("annotations", {}).get(
        "kafka.eventing.knative.dev/external.topic"
    )
    if annotation != args.topic:
        raise SystemExit(1)


def command_image_present(
    document: dict[str, Any], args: argparse.Namespace
) -> None:
    actual = {image for value in items(document) for image in pod_images(value)}
    if args.expected not in actual:
        raise SystemExit(1)


def command_image_repository_present(
    document: dict[str, Any], args: argparse.Namespace
) -> None:
    actual = {image for value in items(document) for image in pod_images(value)}
    if not any(
        image == args.repository
        or image.startswith(f"{args.repository}:")
        or image.startswith(f"{args.repository}@sha256:")
        for image in actual
    ):
        raise SystemExit(1)


def command_broker(document: dict[str, Any], args: argparse.Namespace) -> None:
    if not status_ready(document):
        raise SystemExit(1)
    if document.get("spec", {}).get("brokerName") != args.expected:
        raise SystemExit(1)


def command_topic(document: dict[str, Any], args: argparse.Namespace) -> None:
    values = items(document)
    if not any(
        value.get("spec", {}).get("topicName") == args.expected and status_ready(value)
        for value in values
    ):
        raise SystemExit(1)


def command_knative_all_ready(
    document: dict[str, Any], _args: argparse.Namespace
) -> None:
    values = items(document)
    require_nonempty(values)
    if not all(condition_true(value, "Ready") for value in values):
        raise SystemExit(1)


def command_revision_ready(document: dict[str, Any], _args: argparse.Namespace) -> None:
    values = items(document)
    require_nonempty(values)
    if not any(condition_true(value, "Ready") for value in values):
        raise SystemExit(1)


def command_workloads_reference_secret(
    document: dict[str, Any], args: argparse.Namespace
) -> None:
    values = items(document)
    require_nonempty(values)
    if not all(args.secret in pod_pull_secret_names(value) for value in values):
        raise SystemExit(1)


def command_images_allowed(document: dict[str, Any], args: argparse.Namespace) -> None:
    allowed = {
        line.strip()
        for line in Path(args.allowed_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    if not allowed:
        raise SystemExit(1)
    values = items(document)
    require_nonempty(values)
    actual = {image for value in values for image in pod_images(value)}
    if not actual or not actual.issubset(allowed):
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    simple = {
        "pods-ready": command_pods_ready,
        "nodes-ready": command_nodes_ready,
        "clusteroperators-ready": command_clusteroperators_ready,
        "helm-chart-version": command_helm_chart_version,
        "pull-secret-valid": command_pull_secret_valid,
        "resources-ready": command_resources_ready,
        "resource-ready": command_resource_ready,
        "knative-all-ready": command_knative_all_ready,
        "revision-ready": command_revision_ready,
        "single-item-name": command_single_item_name,
        "latest-ready-revision": command_latest_ready_revision,
    }
    for name, function in simple.items():
        subparser = subparsers.add_parser(name)
        subparser.set_defaults(function=function)

    subparser = subparsers.add_parser("controllers-ready")
    subparser.add_argument("--kind", choices=("deployment", "statefulset"), required=True)
    subparser.set_defaults(function=command_controllers_ready)

    subparser = subparsers.add_parser("pvcs-ready")
    subparser.add_argument("--storage-class", required=True)
    subparser.set_defaults(function=command_pvcs_ready)

    subparser = subparsers.add_parser("helm-status")
    subparser.add_argument("--expected", required=True)
    subparser.set_defaults(function=command_helm_status)

    subparser = subparsers.add_parser("secret-has-keys")
    subparser.add_argument("--keys", required=True)
    subparser.set_defaults(function=command_secret_has_keys)

    subparser = subparsers.add_parser("secret-value-equals")
    subparser.add_argument("--key", required=True)
    subparser.add_argument("--expected", required=True)
    subparser.set_defaults(function=command_secret_value_equals)

    subparser = subparsers.add_parser("policy-name")
    subparser.add_argument("--expected", required=True)
    subparser.set_defaults(function=command_policy_name)

    subparser = subparsers.add_parser("pipeline")
    subparser.add_argument("--topic", required=True)
    subparser.add_argument("--broker-name", required=True)
    subparser.add_argument("--registry", required=True)
    subparser.add_argument("--cluster", required=True)
    subparser.add_argument("--image-repository", required=True)
    subparser.add_argument("--image-tag", required=True)
    subparser.set_defaults(function=command_pipeline)

    subparser = subparsers.add_parser("trigger-contract")
    subparser.add_argument("--service", required=True)
    subparser.set_defaults(function=command_trigger_contract)

    subparser = subparsers.add_parser("broker-contract")
    subparser.add_argument("--topic", required=True)
    subparser.set_defaults(function=command_broker_contract)

    subparser = subparsers.add_parser("image-present")
    subparser.add_argument("--expected", required=True)
    subparser.set_defaults(function=command_image_present)

    subparser = subparsers.add_parser("image-repository-present")
    subparser.add_argument("--repository", required=True)
    subparser.set_defaults(function=command_image_repository_present)

    subparser = subparsers.add_parser("broker")
    subparser.add_argument("--expected", required=True)
    subparser.set_defaults(function=command_broker)

    subparser = subparsers.add_parser("topic")
    subparser.add_argument("--expected", required=True)
    subparser.set_defaults(function=command_topic)

    subparser = subparsers.add_parser("workloads-reference-secret")
    subparser.add_argument("--secret", required=True)
    subparser.set_defaults(function=command_workloads_reference_secret)

    subparser = subparsers.add_parser("images-allowed")
    subparser.add_argument("--allowed-file", required=True)
    subparser.set_defaults(function=command_images_allowed)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    document = read_json()
    args.function(document, args)


if __name__ == "__main__":
    main()
