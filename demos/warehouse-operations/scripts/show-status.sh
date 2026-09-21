#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_FILE="${WAREHOUSE_PROFILE_FILE:-${SCRIPT_DIR}/../profiles.yaml}"

if [[ ! -r "${PROFILE_FILE}" ]]; then
  printf 'ERROR: profile file not found: %s\n' "${PROFILE_FILE}" >&2
  printf 'Copy profiles.example.yaml to profiles.yaml and complete the site values.\n' >&2
  exit 2
fi

exec python3 - "${PROFILE_FILE}" <<'PY'
import json
import subprocess
import sys
from pathlib import Path
from shlex import join as shell_join


def command(args, *, check=True):
    result = subprocess.run(args, text=True, capture_output=True)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(f"{shell_join(args)}: {detail}")
    return result


def output(args):
    return command(args).stdout.strip()


def oc_json(args, *, required=True):
    result = command(["oc", *args, "-o", "json"], check=False)
    if result.returncode:
        if required:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"oc {' '.join(args)}: {detail}")
        return None
    return json.loads(result.stdout)


def get_controller(item):
    control = oc_json(
        ["-n", item["namespace"], "get", f'{item["kind"].lower()}/{item["name"]}'],
        required=False,
    )
    runtime = oc_json(
        ["-n", item["namespace"], "get", f'{item["runtime_kind"].lower()}/{item["runtime_name"]}'],
        required=False,
    )
    desired = None if control is None else int(control.get("spec", {}).get("replicas", 0) or 0)
    runtime_desired = None if runtime is None else int(runtime.get("spec", {}).get("replicas", 0) or 0)
    ready = None if runtime is None else int(runtime.get("status", {}).get("readyReplicas", 0) or 0)
    gpu = None
    if runtime is not None:
        gpu = 0
        containers = runtime.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            resources = container.get("resources", {})
            value = resources.get("requests", {}).get(
                "nvidia.com/gpu",
                resources.get("limits", {}).get("nvidia.com/gpu", 0),
            )
            gpu += int(value or 0)
    return desired, runtime_desired, ready, gpu


def state_text(value):
    return "-" if value is None else str(value)


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    cluster = config["cluster"]
    identity = output(["oc", "whoami"])
    server = output(["oc", "whoami", "--show-server"])
    context = output(["oc", "config", "current-context"])
    cv = oc_json(["get", "clusterversion", "version"])
    ocp = cv.get("status", {}).get("desired", {}).get("version", "unknown")
    version = json.loads(output(["oc", "version", "-o", "json"]))
    kube = version.get("serverVersion", {}).get("gitVersion", "unknown").lstrip("v").split("+")[0]

    print(f"{cluster['name']} demo profile status (read-only)")
    print(f"Identity: {identity}")
    print(f"API server: {server}")
    print(f"Context: {context}")
    print(f"OpenShift/Kubernetes: {ocp}/{kube}")
    expected = (
        cluster["expected_identity"],
        cluster["expected_api_server"],
        cluster["expected_context"],
        cluster["expected_openshift_version"],
        cluster["expected_kubernetes_version"],
    )
    observed = (identity, server, context, ocp, kube)
    if observed != expected:
        raise RuntimeError(f"cluster identity/version differs from profiles.yaml: observed={observed!r}")

    node = oc_json(["get", "node", cluster["gpu_node"]])
    ready = next(
        (condition.get("status") for condition in node.get("status", {}).get("conditions", []) if condition.get("type") == "Ready"),
        "unknown",
    )
    cordoned = bool(node.get("spec", {}).get("unschedulable", False))
    allocatable = int(node.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu", 0) or 0)
    print(f"GPU node: {cluster['gpu_node']} ready={ready} cordoned={cordoned} allocatable_gpus={allocatable}")

    print("\nRetained Helm releases")
    print(f"{'NAMESPACE':30} {'RELEASE':28} {'REV':>4} {'STATUS':10} {'LOCK':>5}")
    for release in config["retained_data_planes"]:
        result = command(
            ["helm", "-n", release["namespace"], "list", "--all", "--filter", f'^{release["helm_release"]}$', "-o", "json"],
            check=False,
        )
        rows = json.loads(result.stdout or "[]") if result.returncode == 0 else []
        if len(rows) == 1:
            row = rows[0]
            revision = int(row.get("revision", 0))
            status = row.get("status", "unknown")
            lock = "PASS" if revision == int(release["verified_revision"]) and status == "deployed" else "DRIFT"
        else:
            revision, status, lock = 0, "absent", "DRIFT"
        print(f"{release['namespace']:30} {release['helm_release']:28} {revision:>4} {status:10} {lock:>5}")

    profile_states = {}
    print("\nGPU controller lock")
    print(f"{'PROFILE':10} {'NAMESPACE':30} {'CONTROLLER':42} {'CFG':>4} {'DES':>4} {'RUN':>4} {'RDY':>4} {'GPU':>3}")
    for profile_name, profile in config["profiles"].items():
        rows = []
        for item in profile["controllers"]:
            controller = f'{item["kind"]}/{item["name"]}'
            if not item.get("resolved", False):
                print(
                    f"{profile_name:10} {item['namespace']:30} {controller:42} "
                    f"{item['replicas']:>4} {'-':>4} {'-':>4} {'-':>4} {item['gpu_per_replica']:>3}"
                )
                rows.append((False, False))
                continue
            desired, runtime_desired, ready_count, gpu = get_controller(item)
            target = int(item["replicas"])
            active = desired == target and runtime_desired == target and ready_count == target and gpu == int(item["gpu_per_replica"])
            stopped = desired == 0 and (runtime_desired in (None, 0)) and (ready_count in (None, 0))
            rows.append((active, stopped))
            print(
                f"{profile_name:10} {item['namespace']:30} {controller:42} "
                f"{target:>4} {state_text(desired):>4} {state_text(runtime_desired):>4} "
                f"{state_text(ready_count):>4} {state_text(gpu):>3}"
            )
        profile_states[profile_name] = {
            "active": bool(rows) and all(row[0] for row in rows),
            "stopped": bool(rows) and all(row[1] for row in rows),
            "resolved": bool(profile.get("resolved", False)),
        }

    pods = oc_json(["get", "pods", "-A", "--field-selector=status.phase!=Succeeded,status.phase!=Failed"])
    gpu_pods = []
    requested = 0
    for pod in pods.get("items", []):
        gpu = 0
        for container in pod.get("spec", {}).get("containers", []):
            resources = container.get("resources", {})
            value = resources.get("requests", {}).get(
                "nvidia.com/gpu",
                resources.get("limits", {}).get("nvidia.com/gpu", 0),
            )
            gpu += int(value or 0)
        if gpu:
            requested += gpu
            gpu_pods.append(
                (
                    pod.get("metadata", {}).get("namespace", ""),
                    pod.get("metadata", {}).get("name", ""),
                    pod.get("spec", {}).get("nodeName", "Pending"),
                    gpu,
                    pod.get("status", {}).get("phase", ""),
                )
            )

    print("\nActive GPU-requesting pods")
    print(f"{'NAMESPACE':30} {'POD':58} {'NODE':12} {'GPU':>3} {'PHASE':10}")
    for namespace, pod_name, node_name, gpu, phase in sorted(gpu_pods):
        print(f"{namespace:30} {pod_name:58} {node_name:12} {gpu:>3} {phase:10}")
    print(f"GPU requests: {requested}/{allocatable}")

    base = profile_states["base-cvd"]
    warehouse = profile_states["warehouse"]
    if base["active"] and (not warehouse["resolved"] or warehouse["stopped"]):
        mode = "base-cvd"
    elif warehouse["resolved"] and warehouse["active"] and base["stopped"]:
        mode = "warehouse"
    else:
        mode = "mixed-or-transitioning"
    print(f"\nInferred mode: {mode}")
    if not warehouse["resolved"]:
        print("Warehouse execution gate: BLOCKED until the final Helm-rendered GPU controller inventory replaces every placeholder.")
    print(f"{cluster['gpu_node']} end-state gate: {'PASS' if cordoned else 'FAIL'}")
    print(f"GPU capacity gate: {'PASS' if requested <= int(cluster['gpu_capacity']) else 'FAIL'}")
    if not cordoned or requested > int(cluster["gpu_capacity"]) or mode == "mixed-or-transitioning":
        raise RuntimeError(
            f"unsafe or incomplete profile state: mode={mode}, cordoned={cordoned}, "
            f"gpu_requests={requested}/{cluster['gpu_capacity']}"
        )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
PY
