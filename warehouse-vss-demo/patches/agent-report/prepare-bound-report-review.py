#!/usr/bin/env python3
"""Build source-locked, local-only Warehouse CPU Agent report review artifacts.

Nothing is deployed. Public source files are checksum-verified before staging;
the existing two report patches are retained. Observation-only prompts and the
vehicle-count template correction are explicit, independent opt-ins.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


SOURCE_COMMIT = "7640d917047cf7b0fd3085eefb8282754b56bc94"
IMAGE = "nvcr.io/nvidia/vss-core/vss-agent:3.2.1@sha256:b7f3246aaf355ebf96e91a40b2f0abc5dea7e330e7bf9a7cf726107b560c3ac1"
SOURCE_FILES = {
    "report_agent.py": ("services/agent/src/vss_agents/agents/report_agent.py", "05fde3ee3b94fcea8625143f11ddfbf744d32a31e9b04e91940b43997620bb0e"),
    "template_report_gen.py": ("services/agent/src/vss_agents/tools/template_report_gen.py", "61f9f88a540e8d725946ae95ded3241569051934ebb103da8a9532b897561ec5"),
    "config.yml": ("deploy/docker/industry-profiles/warehouse-operations/vss-agent/configs/config.yml", "0d922090a1ebcf6f4ab6d1d16378dae9be67afc7a82e63d532d662bdb3ee4bd6"),
    "incident_report_template.md": ("deploy/docker/industry-profiles/warehouse-operations/vss-agent/templates/incident_report_template.md", "826cf947a37b7e151c50422812f373b76f57cd89c93f5dcc1457fa8aa6a7e5e1"),
}
RUNTIME_HASHES = {name: digest for name, (_path, digest) in SOURCE_FILES.items()}
RUNTIME_HASHES["report_agent.py"] = "85774db38c1e93931bc39636d730f9ff8fc27f03a86b5bffb83ceca6aad7136f"
RUNTIME_HASHES["va_mcp_server_config.yml"] = "b59620246f01c3eaf59ea8195ecb1d7bb24eda969a84c10606985453e0b89bf3"
BASE_CONFIG_FILES = ("config.yml", "incident_report_template.md", "va_mcp_server_config.yml")
MOUNT_PATHS = {
    "report_agent.py": "/vss-agent/.venv/lib/python3.13/site-packages/vss_agents/agents/report_agent.py",
    "template_report_gen.py": "/vss-agent/.venv/lib/python3.13/site-packages/vss_agents/tools/template_report_gen.py",
    "config.yml": "/vss-agent/config/config.yml",
    "incident_report_template.md": "/vss-agent/config/incident_report_template.md",
}
DIRECTORY = Path(__file__).resolve().parent


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_contract(contract: dict, namespace: str, deployment: str, selected: list[str]) -> None:
    if contract.get("namespace") != namespace or contract.get("deployment") != deployment:
        raise ValueError("Live contract identifies a different deployment")
    if contract.get("image") != IMAGE:
        raise ValueError("Live Agent image differs from the pinned review image")
    if not contract.get("uid") or not contract.get("resourceVersion") or not isinstance(contract.get("resources"), dict):
        raise ValueError("Live contract is missing deployment identity or resource evidence")
    for name in selected:
        if contract.get("runtime_sha256", {}).get(name) != RUNTIME_HASHES[name]:
            raise ValueError(f"Live contract hash differs for {name}")
    if any(name in selected for name in BASE_CONFIG_FILES):
        for name in BASE_CONFIG_FILES:
            if contract.get("runtime_sha256", {}).get(name) != RUNTIME_HASHES[name]:
                raise ValueError(f"Preserved Agent configuration hash differs for {name}")
    mounts = contract.get("volumeMounts", [])
    expected_mount = {"name": "agent-report-content", "mountPath": MOUNT_PATHS["report_agent.py"], "subPath": "report_agent.py", "readOnly": True}
    if expected_mount not in mounts:
        raise ValueError("Live contract does not contain the existing v2 report mount")
    if {"name": "agent-config", "mountPath": "/vss-agent/config", "readOnly": True} not in mounts:
        raise ValueError("Live contract does not contain the original read-only Agent configuration directory")
    volumes = contract.get("configMapVolumes", [])
    if not any(x.get("name") == "agent-report-content" and x.get("configMap", {}).get("name") == "warehouse-agent-report-content-v2" for x in volumes):
        raise ValueError("Live contract does not contain the existing v2 report ConfigMap")
    if not any(x.get("name") == "agent-config" and x.get("configMap", {}).get("name") == "warehouse-agent" for x in volumes):
        raise ValueError("Live contract does not contain the original Agent configuration ConfigMap")


def prepare(
    *, source: Path, output: Path, live_contract: dict,
    namespace: str = "nvidia-vss-321-warehouse", deployment: str = "warehouse-demo-agent",
    configmap_name: str = "warehouse-agent-bound-report-v1",
    observation_prompts: bool = False, template_count_fix: bool = False,
) -> dict:
    for value in (namespace, deployment, configmap_name):
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", value):
            raise ValueError("Target names must be valid Kubernetes names")
    if output.exists() or output.is_symlink():
        raise ValueError("Output directory must be new; existing files are never overwritten")
    selected = ["report_agent.py", "template_report_gen.py"]
    if observation_prompts:
        selected.append("config.yml")
    if template_count_fix:
        selected.append("incident_report_template.md")
    validate_contract(live_contract, namespace, deployment, selected)
    originals = {}
    for name in selected:
        relative, expected = SOURCE_FILES[name]
        raw = (source / relative).read_bytes()
        if sha256(raw) != expected:
            raise ValueError(f"Public pinned source hash differs for {name}")
        originals[name] = raw
    patches = ["0001-forward-generated-report-content.patch", "0002-append-grounded-report-evidence.patch", "0004-bind-source-alert-metadata.patch"]
    if observation_prompts:
        patches.append("0005-observation-only-report-prompts.patch")
    if template_count_fix:
        patches.append("0006-optional-vehicle-count-template.patch")
    with tempfile.TemporaryDirectory(prefix="warehouse-report-review-") as temp:
        stage = Path(temp)
        for name, raw in originals.items():
            path = stage / SOURCE_FILES[name][0]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        for patch in patches:
            for args in (("--check",), ()):
                result = subprocess.run(["git", "apply", *args, str(DIRECTORY / patch)], cwd=stage, capture_output=True, text=True)
                if result.returncode:
                    raise ValueError(f"Source-locked patch did not apply: {patch}")
        contents = {name: (stage / SOURCE_FILES[name][0]).read_text(encoding="utf-8") for name in selected}
        for name in ("report_agent.py", "template_report_gen.py"):
            ast.parse(contents[name], filename=name)
    configmap = {
        "apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
        "metadata": {"namespace": namespace, "name": configmap_name,
                     "labels": {"app.kubernetes.io/part-of": "warehouse-agent-report-correction"},
                     "annotations": {"warehouse.nvidia.com/source-commit": SOURCE_COMMIT}},
        "data": contents,
    }
    mount_name = "agent-bound-report"
    module_names = [name for name in selected if name.endswith(".py")]
    config_names = [name for name in selected if name in BASE_CONFIG_FILES]
    # Do not bind-mount a file into the read-only ConfigMap-backed directory.
    # Project the complete three-file directory with disjoint source items.
    # The delete entry also removes the failed earlier nested config mount.
    mounts = [{"name": mount_name, "mountPath": MOUNT_PATHS[name], "subPath": name, "readOnly": True} for name in module_names]
    nested_config_names = ["config.yml"] + (["incident_report_template.md"] if "incident_report_template.md" in config_names else [])
    mounts.extend({"mountPath": MOUNT_PATHS[name], "$patch": "delete"} for name in nested_config_names)
    volumes = [{"name": mount_name, "configMap": {"name": configmap_name,
                "items": [{"key": name, "path": name} for name in module_names]}}]
    if config_names:
        retained = [name for name in BASE_CONFIG_FILES if name not in config_names]
        volumes.append({"name": "agent-config", "configMap": None, "projected": {
            "defaultMode": 420,
            "sources": [
                {"configMap": {"name": "warehouse-agent", "items": [{"key": name, "path": name} for name in retained]}},
                {"configMap": {"name": configmap_name, "items": [{"key": name, "path": name} for name in config_names]}},
            ],
        }})
    overlay = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"namespace": namespace, "name": deployment},
        "spec": {"template": {"spec": {
            "containers": [{"name": "agent", "volumeMounts": mounts}],
            "volumes": volumes,
        }}},
    }
    rollback_mounts = [{"name": "agent-report-content", "mountPath": MOUNT_PATHS["report_agent.py"], "subPath": "report_agent.py", "readOnly": True}]
    rollback_mounts.extend({"mountPath": MOUNT_PATHS[name], "$patch": "delete"} for name in ["template_report_gen.py", *nested_config_names])
    rollback_volumes = [{"name": mount_name, "$patch": "delete"}]
    if config_names:
        rollback_volumes.append({"name": "agent-config", "projected": None, "configMap": {"name": "warehouse-agent", "defaultMode": 420}})
    rollback = {"spec": {"template": {"spec": {
        "containers": [{"name": "agent", "volumeMounts": rollback_mounts}],
        "volumes": rollback_volumes,
    }}}}
    review = {
        "status": "LOCAL_PREVIEW_ONLY_REQUIRES_APPROVAL",
        "source_commit": SOURCE_COMMIT,
        "target": {key: live_contract[key] for key in ("namespace", "deployment", "uid", "resourceVersion")},
        "image_before": IMAGE, "image_after": IMAGE,
        "resources_before": live_contract["resources"], "resources_after": live_contract["resources"],
        "nodeName_before": live_contract.get("nodeName"), "nodeName_after": live_contract.get("nodeName"),
        "optional_changes": {"observation_only_report_prompts": observation_prompts, "vehicle_count_template_placeholder": template_count_fix},
        "mount_layout": "Two Python module subPath mounts; Agent configuration uses one complete projected directory, never nested file mounts" if config_names else "Two Python module subPath mounts; existing Agent configuration directory unchanged",
        "preserved_config_sha256": {name: RUNTIME_HASHES[name] for name in BASE_CONFIG_FILES if name not in config_names} if config_names else {},
        "patches": [{"name": name, "sha256": sha256((DIRECTORY / name).read_bytes())} for name in patches],
        "files": [{"name": name, "mountPath": MOUNT_PATHS[name], "before_sha256": RUNTIME_HASHES[name],
                   "after_sha256": sha256(contents[name].encode())} for name in selected],
        "effects": ["One immutable non-secret ConfigMap; CPU Agent-only rollout from read-only module mounts and a complete projected configuration directory",
                    "Chat briefly interrupted; in-memory report artifacts may disappear on rollout",
                    "No model/image, resource/GPU, network, Secret, PVC, camera/rule, VIOS or node scheduling changes",
                    "Prompt correction reduces unsupported inferences but does not guarantee visual accuracy"],
        "acceptance": ["Loaded file hashes equal after_sha256; image/resources unchanged; Agent Ready/health HTTP 200",
                       "Same-incident UI report completes with source-bound identity and interval in Markdown and PDF",
                       "Artifact-only public HTTPS check passes 20 identical reads",
                       "Visually verify narrative against the same clip; do not accept invented vehicle motion"],
    }
    output.mkdir(parents=True)
    files = output / "files"
    files.mkdir()
    for name, content in contents.items():
        (files / name).write_text(content, encoding="utf-8")
    write_json(output / "configmap.json", configmap)
    write_json(output / "agent-overlay.json", overlay)
    write_json(output / "corrective-overlay.json", overlay)
    write_json(output / "agent-rollback.json", rollback)
    write_json(output / "review.json", review)
    return review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Pinned public NVIDIA checkout or exact checksum-matched source snapshot")
    parser.add_argument("--output", type=Path, required=True, help="New local output directory")
    parser.add_argument("--live-contract", type=Path, required=True, help="Narrow read-only deployment/file-hash contract; no credentials or source contents")
    parser.add_argument("--namespace", default="nvidia-vss-321-warehouse")
    parser.add_argument("--deployment", default="warehouse-demo-agent")
    parser.add_argument("--configmap-name", default="warehouse-agent-bound-report-v1")
    parser.add_argument("--observation-prompts", action="store_true")
    parser.add_argument("--template-count-fix", action="store_true")
    args = parser.parse_args()
    try:
        review = prepare(source=args.source, output=args.output, live_contract=json.loads(args.live_contract.read_text()),
                         namespace=args.namespace, deployment=args.deployment, configmap_name=args.configmap_name,
                         observation_prompts=args.observation_prompts, template_count_fix=args.template_count_fix)
    except (ValueError, OSError, json.JSONDecodeError):
        print("FAIL: source, live contract, patch or output validation failed; nothing was deployed", file=sys.stderr)
        return 1
    print(json.dumps({"status": review["status"], "files": review["files"], "image_unchanged": True, "resources_unchanged": True}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
