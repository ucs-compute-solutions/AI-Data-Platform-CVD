#!/usr/bin/env python3
"""Prepare source-locked Warehouse incident-follow-up review artifacts.

This command writes local JSON manifests and reviewed Python modules only.
It does not contact Kubernetes, apply manifests, restart services or run models.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any
from uuid import UUID


DIRECTORY = Path(__file__).resolve().parent
NAMESPACE = "nvidia-vss-321-warehouse"
DEPLOYMENT = "warehouse-demo-agent"
CONTAINER = "agent"
CONFIGMAP = "warehouse-agent-incident-followup-v1"
VOLUME = "agent-incident-followup"
IMAGE = "nvcr.io/nvidia/vss-core/vss-agent:3.2.1@sha256:b7f3246aaf355ebf96e91a40b2f0abc5dea7e330e7bf9a7cf726107b560c3ac1"
SOURCE_PREFIX = "services/agent/src/vss_agents/agents"
RUNTIME_PREFIX = "/vss-agent/.venv/lib/python3.13/site-packages/vss_agents/agents"
BASELINE_SHA256 = {
    "top_agent.py": "a4aa4a051b589858078a7941cf32de8128d7c22f574bdf718974a70cb0c140c4",
    "report_agent.py": "0c8392a5bfa9e12e7a387b0c66091d2f147dd50a53378ef14cfe6163458ea55d",
}
MODULES = ("top_agent.py", "report_agent.py", "incident_followup.py")
PATCH_NAME = "0007-incident-bound-followup.patch"
MAX_CONFIGMAP_BYTES = 900_000


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def runtime_path(name: str) -> str:
    return f"{RUNTIME_PREFIX}/{name}"


def validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate narrow deployment evidence and return the prior report mount."""
    if not isinstance(contract, dict):
        raise ValueError("Deployment contract must be a JSON object")
    if contract.get("namespace") != NAMESPACE or contract.get("deployment") != DEPLOYMENT:
        raise ValueError("Deployment contract identifies a different target")
    try:
        UUID(str(contract["uid"]))
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Deployment contract requires its live UID") from exc
    if not re.fullmatch(r"[0-9]+", str(contract.get("resourceVersion", ""))):
        raise ValueError("Deployment contract requires its live resourceVersion")
    if contract.get("image") != IMAGE:
        raise ValueError("Agent image differs from the source-locked image")
    if not isinstance(contract.get("resources"), dict):
        raise ValueError("Deployment contract requires unchanged resource evidence")
    mounts = contract.get("mount_before")
    volumes = contract.get("volumes_before")
    if not isinstance(mounts, list) or not isinstance(volumes, list):
        raise ValueError("Deployment contract requires mount_before and volumes_before lists")
    if any(not isinstance(item, dict) for item in mounts + volumes):
        raise ValueError("Mount and volume references must be JSON objects")
    mount_paths = [item.get("mountPath") for item in mounts]
    volume_names = [item.get("name") for item in volumes]
    if any(not isinstance(value, str) or not value for value in mount_paths + volume_names):
        raise ValueError("Every recorded mount and volume requires its merge-key identity")
    if len(set(mount_paths)) != len(mount_paths) or len(set(volume_names)) != len(volume_names):
        raise ValueError("Duplicate mount paths or volume names are not accepted")
    if VOLUME in volume_names or any(item.get("name") == VOLUME for item in mounts):
        raise ValueError("Follow-up volume already exists; do not overwrite an existing override")
    for name in ("top_agent.py", "incident_followup.py"):
        if runtime_path(name) in mount_paths:
            raise ValueError(f"Existing {name} override requires a separate review")
    report_mounts = [item for item in mounts if item.get("mountPath") == runtime_path("report_agent.py")]
    if len(report_mounts) != 1:
        raise ValueError("Exactly one existing report_agent.py mount is required for rollback")
    report_mount = report_mounts[0]
    if (report_mount.get("readOnly") is not True
            or report_mount.get("subPath") != "report_agent.py"
            or report_mount.get("name") not in volume_names):
        raise ValueError("Existing report module must be a referenced read-only subPath mount")
    if not any(item.get("mountPath") == "/vss-agent/config" and item.get("readOnly") is True
               for item in mounts):
        raise ValueError("Existing read-only Agent configuration directory is missing")
    # Only a Kubernetes volumeMount object is copied into rollback, never the
    # complete contract or arbitrary volume/source data supplied alongside it.
    mount_keys = {"name", "mountPath", "subPath", "subPathExpr", "readOnly",
                  "mountPropagation", "recursiveReadOnly"}
    if set(report_mount) - mount_keys:
        raise ValueError("Existing report mount contains unsupported fields")
    return copy.deepcopy(report_mount)


def source_bytes(source: Path, name: str) -> bytes:
    """Accept a flat locked snapshot or the exact corresponding source tree."""
    candidates = [path for path in (source / name, source / SOURCE_PREFIX / name) if path.is_file()]
    if len(candidates) != 1:
        raise ValueError(f"Provide exactly one baseline source for {name}")
    raw = candidates[0].read_bytes()
    if sha256(raw) != BASELINE_SHA256[name]:
        raise ValueError(f"Baseline source SHA-256 differs for {name}")
    return raw


def patched_modules(source: Path) -> tuple[dict[str, str], str]:
    originals = {name: source_bytes(source, name) for name in BASELINE_SHA256}
    patch = DIRECTORY / PATCH_NAME
    patch_raw = patch.read_bytes()
    expected_paths = {f"{SOURCE_PREFIX}/{name}" for name in BASELINE_SHA256}
    headers = re.findall(r"^diff --git a/(\S+) b/(\S+)$", patch_raw.decode("utf-8"), re.MULTILINE)
    if (len(headers) != len(expected_paths)
            or {left for left, _right in headers} != expected_paths
            or any(left != right for left, right in headers)):
        raise ValueError("Follow-up patch must touch exactly the two locked Agent modules")
    helper = (DIRECTORY / "incident_followup.py").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="warehouse-incident-followup-") as temporary:
        stage = Path(temporary)
        for name, raw in originals.items():
            target = stage / SOURCE_PREFIX / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        for options in (("--check",), ()):
            result = subprocess.run(["git", "apply", *options, str(patch)], cwd=stage,
                                    capture_output=True, text=True, check=False)
            if result.returncode:
                raise ValueError("Source-locked incident follow-up patch did not apply")
        contents = {name: (stage / SOURCE_PREFIX / name).read_text(encoding="utf-8")
                    for name in BASELINE_SHA256}
    contents["incident_followup.py"] = helper
    for name, content in contents.items():
        ast.parse(content, filename=name)
    if sum(len(value.encode("utf-8")) for value in contents.values()) > MAX_CONFIGMAP_BYTES:
        raise ValueError("Reviewed module data exceeds the bounded ConfigMap size")
    return contents, sha256(patch_raw)


def prepare(*, source: Path, contract: dict[str, Any], output: Path) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise ValueError("Output directory must be new; existing files are never overwritten")
    prior_report_mount = validate_contract(contract)
    contents, patch_digest = patched_modules(source)
    mounts = [{"name": VOLUME, "mountPath": runtime_path(name), "subPath": name,
               "readOnly": True} for name in MODULES]
    configmap = {
        "apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
        "metadata": {"name": CONFIGMAP, "namespace": NAMESPACE,
                     "labels": {"app.kubernetes.io/part-of": "warehouse-agent-incident-followup"}},
        "data": contents,
    }
    target = {key: contract[key] for key in ("namespace", "deployment", "uid", "resourceVersion")}
    overlay = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": DEPLOYMENT, "namespace": NAMESPACE,
                     "uid": contract["uid"], "resourceVersion": str(contract["resourceVersion"])},
        "spec": {"template": {"spec": {
            "containers": [{"name": CONTAINER, "volumeMounts": mounts}],
            "volumes": [{"name": VOLUME, "configMap": {"name": CONFIGMAP,
                         "items": [{"key": name, "path": name} for name in MODULES]}}],
        }}},
    }
    rollback = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": DEPLOYMENT, "namespace": NAMESPACE, "uid": contract["uid"]},
        "spec": {"template": {"spec": {
            "containers": [{"name": CONTAINER, "volumeMounts": [prior_report_mount,
                {"mountPath": runtime_path("top_agent.py"), "$patch": "delete"},
                {"mountPath": runtime_path("incident_followup.py"), "$patch": "delete"}]}],
            "volumes": [{"name": VOLUME, "$patch": "delete"}],
        }}},
    }
    review = {
        "status": "LOCAL_PREVIEW_ONLY_NOT_DEPLOYED",
        "target": target,
        "image_before": IMAGE, "image_after": IMAGE,
        "resources_before": copy.deepcopy(contract["resources"]),
        "resources_after": copy.deepcopy(contract["resources"]),
        "patch": {"name": PATCH_NAME, "sha256": patch_digest},
        "builder_sha256": sha256(Path(__file__).read_bytes()),
        "configmap": {"name": CONFIGMAP, "immutable": True},
        "files": [{"name": name, "mountPath": runtime_path(name),
                   "before_sha256": BASELINE_SHA256.get(name),
                   "after_sha256": sha256(contents[name].encode("utf-8"))} for name in MODULES],
        "prior_report_mount": prior_report_mount,
        "preserved_volume_names": sorted(item["name"] for item in contract["volumes_before"]),
        "effects": [
            "Create one immutable non-secret ConfigMap containing three reviewed Python modules",
            "Patch only three read-only module mounts and one new volume on the CPU Agent Deployment",
            "Retain all pre-existing volumes and unrelated mounts, including the template-report module and configuration directory",
            "Agent-only rollout briefly interrupts chat and clears its in-memory conversation state",
            "No image, CPU/memory or GPU allocation, network, Secret, PVC, VIOS, camera, rule or node scheduling change",
            "Follow-up visual analysis reuses the existing configured GPU-backed video-understanding service",
        ],
        "acceptance": [
            "Verify fresh identity, Deployment UID/resourceVersion and existing image/resources/mounts before applying",
            "Server-side dry-run both generated manifests before the reviewed apply",
            "Verify Agent Ready, health HTTP 200 and all three loaded module hashes",
            "Generate a fresh report and ask its clothing-color follow-up in the same conversation",
            "Verify absent visual details use only the bound exact clip and another conversation never inherits that binding",
            "Confirm four ongoing recordings and unchanged camera/rule/PVC/GPU/scheduling state",
        ],
        "rollback": "Restore only the prior report module mount; remove the new top/helper mounts and new volume; retain both ConfigMaps",
        "deployed": False,
    }
    output.mkdir(parents=True, exist_ok=False)
    files = output / "files"
    files.mkdir()
    for name, content in contents.items():
        (files / name).write_text(content, encoding="utf-8")
    write_json(output / "configmap.json", configmap)
    write_json(output / "agent-overlay.json", overlay)
    write_json(output / "agent-rollback.json", rollback)
    write_json(output / "review.json", review)
    review["artifact_sha256"] = {
        name: sha256((output / name).read_bytes())
        for name in ("configmap.json", "agent-overlay.json", "agent-rollback.json")
    }
    write_json(output / "review.json", review)
    return review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path,
                        help="Checksum-matched baseline snapshot or corresponding source tree")
    parser.add_argument("--contract", required=True, type=Path,
                        help="Narrow deployment identity, image/resources and mount/volume references JSON")
    parser.add_argument("--output-dir", required=True, type=Path, help="New local review directory")
    args = parser.parse_args()
    try:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        review = prepare(source=args.source_dir, contract=contract, output=args.output_dir)
    except (OSError, ValueError, SyntaxError, KeyError, TypeError) as exc:
        print(f"FAIL: {exc}; nothing was deployed", file=sys.stderr)
        return 1
    print(json.dumps({"status": review["status"], "target": review["target"],
                      "files": review["files"], "artifact_sha256": review["artifact_sha256"],
                      "deployed": False}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
