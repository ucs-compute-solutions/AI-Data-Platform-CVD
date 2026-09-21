#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_FILE="${WAREHOUSE_PROFILE_FILE:-${SCRIPT_DIR}/../profiles.yaml}"

if [[ ! -r "${PROFILE_FILE}" ]]; then
  printf 'ERROR: profile file not found: %s\n' "${PROFILE_FILE}" >&2
  printf 'Copy profiles.example.yaml to profiles.yaml and complete the site values.\n' >&2
  exit 2
fi

exec python3 - "${PROFILE_FILE}" "$@" <<'PY'
import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from shlex import join as shell_join


class SwitchError(RuntimeError):
    pass


def command(args, *, check=True):
    result = subprocess.run(args, text=True, capture_output=True)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise SwitchError(f"{shell_join(args)}: {detail}")
    return result


def output(args):
    return command(args).stdout.strip()


def oc_json(args, *, required=True):
    result = command(["oc", *args, "-o", "json"], check=False)
    if result.returncode:
        if required:
            detail = result.stderr.strip() or result.stdout.strip()
            raise SwitchError(f"oc {' '.join(args)}: {detail}")
        return None
    return json.loads(result.stdout)


def resource_args(item, *, runtime=False):
    kind = item["runtime_kind"] if runtime else item["kind"]
    name = item["runtime_name"] if runtime else item["name"]
    return ["-n", item["namespace"], "get", f"{kind.lower()}/{name}"]


def desired_replicas(item):
    data = oc_json(resource_args(item), required=False)
    if data is None:
        return None
    return int(data.get("spec", {}).get("replicas", 0) or 0)


def runtime_state(item):
    data = oc_json(resource_args(item, runtime=True), required=False)
    if data is None:
        return None
    spec = int(data.get("spec", {}).get("replicas", 0) or 0)
    status = data.get("status", {})
    ready = int(status.get("readyReplicas", 0) or 0)
    available = int(status.get("availableReplicas", ready) or 0)
    containers = data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    gpu = 0
    for container in containers:
        resources = container.get("resources", {})
        value = resources.get("requests", {}).get(
            "nvidia.com/gpu",
            resources.get("limits", {}).get("nvidia.com/gpu", 0),
        )
        gpu += int(value or 0)
    return {"spec": spec, "ready": ready, "available": available, "gpu": gpu, "raw": data}


def mutation(item, replicas):
    ref = f'{item["kind"].lower()}/{item["name"]}'
    if item["kind"] == "NIMService":
        patch = json.dumps({"spec": {"replicas": replicas}}, separators=(",", ":"))
        return ["oc", "-n", item["namespace"], "patch", ref, "--type=merge", "-p", patch]
    return ["oc", "-n", item["namespace"], "scale", ref, f"--replicas={replicas}"]


def wait_for(item, replicas, timeout):
    deadline = time.monotonic() + timeout
    ref = f'{item["runtime_kind"].lower()}/{item["runtime_name"]}'
    while time.monotonic() < deadline:
        state = runtime_state(item)
        if replicas == 0 and (state is None or (state["spec"] == 0 and state["ready"] == 0)):
            return
        if state is not None and state["spec"] == replicas and state["ready"] == replicas and state["available"] == replicas:
            return
        time.sleep(5)
    raise SwitchError(
        f"timed out after {timeout}s waiting for {item['namespace']} {ref} to reach {replicas} ready replica(s)"
    )


def wait_all_scheduled(items, node, timeout):
    """Wait for target pods to bind to the GPU node without waiting for model readiness."""
    deadline = time.monotonic() + timeout
    last_pending = []
    while time.monotonic() < deadline:
        pending = []
        for item in items:
            runtime = oc_json(resource_args(item, runtime=True), required=False)
            if runtime is None:
                pending.append(
                    f"{item['namespace']} {item['runtime_kind']}/{item['runtime_name']} runtime absent"
                )
                continue
            labels = runtime.get("spec", {}).get("selector", {}).get("matchLabels", {})
            if not labels:
                raise SwitchError(
                    f"{item['namespace']} {item['runtime_kind']}/{item['runtime_name']} has no matchLabels selector"
                )
            selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
            pods = oc_json(
                ["-n", item["namespace"], "get", "pods", "-l", selector],
                required=False,
            )
            scheduled = 0
            if pods is not None:
                for pod in pods.get("items", []):
                    if pod.get("metadata", {}).get("deletionTimestamp"):
                        continue
                    condition = next(
                        (
                            row.get("status")
                            for row in pod.get("status", {}).get("conditions", [])
                            if row.get("type") == "PodScheduled"
                        ),
                        None,
                    )
                    if condition == "True" and pod.get("spec", {}).get("nodeName") == node:
                        scheduled += 1
            required = int(item["replicas"])
            if scheduled < required:
                pending.append(
                    f"{item['namespace']} {item['runtime_kind']}/{item['runtime_name']} "
                    f"scheduled={scheduled}/{required}"
                )
        if not pending:
            return
        last_pending = pending
        time.sleep(2)
    detail = "; ".join(last_pending) or "target pods were not scheduled"
    raise SwitchError(f"timed out after {timeout}s waiting for GPU pod scheduling: {detail}")


def capture_pod_selectors(items):
    selectors = []
    for item in items:
        runtime = oc_json(resource_args(item, runtime=True), required=False)
        if runtime is None:
            continue
        labels = runtime.get("spec", {}).get("selector", {}).get("matchLabels", {})
        if not labels:
            raise SwitchError(
                f"{item['namespace']} {item['runtime_kind']}/{item['runtime_name']} has no matchLabels selector"
            )
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        selectors.append((item["namespace"], item["runtime_kind"], item["runtime_name"], selector))
    return selectors


def wait_all_terminated(selectors, timeout):
    deadline = time.monotonic() + timeout
    last_active = []
    while time.monotonic() < deadline:
        active = []
        for namespace, kind, name, selector in selectors:
            pods = oc_json(["-n", namespace, "get", "pods", "-l", selector], required=False)
            count = 0 if pods is None else len(pods.get("items", []))
            if count:
                active.append(f"{namespace} {kind}/{name} pods={count}")
        if not active:
            return
        last_active = active
        time.sleep(2)
    detail = "; ".join(last_active) or "GPU pods remain"
    raise SwitchError(f"timed out after {timeout}s waiting for GPU pods to terminate: {detail}")


def groups(items, order_key):
    values = sorted({int(item[order_key]) for item in items})
    return [[item for item in items if int(item[order_key]) == value] for value in values]


def profile_gpu_request(profile):
    return sum(int(item["replicas"]) * int(item["gpu_per_replica"]) for item in profile["controllers"])


def validate_profile_shape(name, profile, capacity):
    controllers = profile.get("controllers", [])
    if not controllers:
        raise SwitchError(f"profile {name} has no controller inventory")
    request = profile_gpu_request(profile)
    if name == "warehouse" and request < 1:
        raise SwitchError("profile warehouse must request at least one GPU")
    if request > int(profile["allowed_gpu_request_max"]) or request > capacity:
        raise SwitchError(f"profile {name} requests {request} GPUs; the site permits at most {capacity}")
    locked = profile.get("locked_gpu_request")
    if profile.get("resolved", False) and locked is None:
        raise SwitchError(f"profile {name} is resolved but locked_gpu_request is null")
    if locked is not None and int(locked) != request:
        raise SwitchError(f"profile {name} controller sum {request} does not match locked_gpu_request {locked}")


def verify_context(config):
    cluster = config["cluster"]
    identity = output(["oc", "whoami"])
    server = output(["oc", "whoami", "--show-server"])
    context = output(["oc", "config", "current-context"])
    cv = oc_json(["get", "clusterversion", "version"])
    ocp = cv.get("status", {}).get("desired", {}).get("version", "")
    version = json.loads(output(["oc", "version", "-o", "json"]))
    kube = version.get("serverVersion", {}).get("gitVersion", "").lstrip("v").split("+")[0]
    observed = (identity, server, context, ocp, kube)
    expected = (
        cluster["expected_identity"],
        cluster["expected_api_server"],
        cluster["expected_context"],
        cluster["expected_openshift_version"],
        cluster["expected_kubernetes_version"],
    )
    print(f"Identity: {identity}")
    print(f"API server: {server}")
    print(f"Context: {context}")
    print(f"OpenShift/Kubernetes: {ocp}/{kube}")
    if observed != expected:
        raise SwitchError(f"cluster identity/version differs from profiles.yaml: observed={observed!r}")
    node = oc_json(["get", "node", cluster["gpu_node"]])
    ready = next(
        (condition.get("status") for condition in node.get("status", {}).get("conditions", []) if condition.get("type") == "Ready"),
        None,
    )
    cordoned = bool(node.get("spec", {}).get("unschedulable", False))
    capacity = int(node.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu", 0) or 0)
    print(f"GPU node: {cluster['gpu_node']} ready={ready} cordoned={cordoned} allocatable_gpus={capacity}")
    if ready != "True" or capacity != int(cluster["gpu_capacity"]):
        raise SwitchError("GPU node readiness or capacity differs from the source lock")
    if cluster["gpu_node_must_begin_and_end_cordoned"] and not cordoned:
        raise SwitchError(f"{cluster['gpu_node']} must be cordoned before a mode-switch command starts")


def verify_releases(config):
    for release in config["retained_data_planes"]:
        result = command(
            ["helm", "-n", release["namespace"], "list", "--all", "--filter", f'^{release["helm_release"]}$', "-o", "json"]
        )
        rows = json.loads(result.stdout or "[]")
        if len(rows) != 1:
            raise SwitchError(f"expected Helm release {release['namespace']}/{release['helm_release']} exactly once")
        row = rows[0]
        revision = int(row.get("revision", 0))
        if row.get("status") != "deployed" or revision != int(release["verified_revision"]):
            raise SwitchError(
                f"Helm release {release['namespace']}/{release['helm_release']} is "
                f"status={row.get('status')} revision={revision}; expected deployed revision={release['verified_revision']}"
            )


def verify_controller_lock(name, profile):
    for item in profile["controllers"]:
        current = desired_replicas(item)
        state = runtime_state(item)
        if current is None:
            raise SwitchError(f"locked controller is absent: {item['namespace']} {item['kind']}/{item['name']}")
        if current not in (0, int(item["replicas"])):
            raise SwitchError(
                f"{item['namespace']} {item['kind']}/{item['name']} has unexpected replicas={current}; "
                f"allowed switch states are 0 or {item['replicas']}"
            )
        if state is not None and state["gpu"] != int(item["gpu_per_replica"]):
            raise SwitchError(
                f"{item['namespace']} {item['runtime_kind']}/{item['runtime_name']} requests "
                f"{state['gpu']} GPU(s) per replica; profiles.yaml locks {item['gpu_per_replica']}"
            )
        expected_retention = item.get("claim_retention_verified")
        if expected_retention and state is not None:
            retention = state["raw"].get("spec", {}).get("persistentVolumeClaimRetentionPolicy", {})
            observed = f"{retention.get('whenDeleted')}/{retention.get('whenScaled')}"
            if observed != expected_retention:
                raise SwitchError(
                    f"{item['namespace']} {item['runtime_kind']}/{item['runtime_name']} claim retention "
                    f"is {observed}; expected {expected_retention}"
                )
    print(f"Controller lock: {name} resources, replica bounds, GPU requests, and claim retention match")


def print_plan(config, target_name, target, source_name, source):
    print("\nPLAN ONLY — no cluster objects were changed")
    print(f"Target profile: {target_name}; locked target GPU request: {profile_gpu_request(target)} of {config['cluster']['gpu_capacity']}")
    print("1. Keep every Helm release, namespace, PVC, cache, object, row, index, topic, and video.")
    print(f"2. Set only the {source_name} GPU controllers to zero:")
    for item in sorted(source["controllers"], key=lambda row: (row["stop_order"], row["namespace"], row["name"])):
        print(f"   $ {shell_join(mutation(item, 0))}")
    print(f"3. Wait until the {source_name} GPU runtime workloads report zero replicas.")
    print(f"4. $ oc adm uncordon {config['cluster']['gpu_node']}")
    print(f"5. Restore the source-locked {target_name} replicas:")
    for item in sorted(target["controllers"], key=lambda row: (row["start_order"], row["namespace"], row["name"])):
        print(f"   $ {shell_join(mutation(item, int(item['replicas'])))}")
    print(
        f"6. Wait for every target GPU pod to bind to {config['cluster']['gpu_node']} "
        "(120 seconds by default)."
    )
    print(f"7. $ oc adm cordon {config['cluster']['gpu_node']}")
    print(
        f"8. Wait for target readiness while {config['cluster']['gpu_node']} remains cordoned."
    )
    print("9. Run scripts/show-status.sh and the target functional tests.")
    print(f"10. On a switching failure, stop the partial {target_name} target and restore the recorded {source_name} replicas.")
    unresolved = [item for item in source["controllers"] + target["controllers"] if not item.get("resolved", False)]
    if not source.get("resolved", False) or not target.get("resolved", False) or unresolved:
        print("\nEXECUTION BLOCKED: Warehouse controller identities are placeholders until the final Helm render is audited.")
        for blocker in config["profiles"]["warehouse"].get("execution_blockers", []):
            print(f"- {blocker}")
    else:
        token = config["safety"]["execution_acknowledgement"]
        print(
            "\nAfter explicit change approval: "
            f"demos/warehouse-operations/scripts/set-demo-mode.sh {target_name} "
            f"--execute --ack {token}"
        )


def total_active_gpu_requests():
    pods = oc_json(["get", "pods", "-A", "--field-selector=status.phase!=Succeeded,status.phase!=Failed"])
    total = 0
    for pod in pods.get("items", []):
        containers = pod.get("spec", {}).get("containers", [])
        for container in containers:
            resources = container.get("resources", {})
            value = resources.get("requests", {}).get(
                "nvidia.com/gpu",
                resources.get("limits", {}).get("nvidia.com/gpu", 0),
            )
            total += int(value or 0)
    return total


def current_profile_runtime_request(profile):
    total = 0
    for item in profile["controllers"]:
        state = runtime_state(item)
        if state is not None:
            total += state["spec"] * state["gpu"]
    return total


def profile_state(profile):
    active = True
    stopped = True
    for item in profile["controllers"]:
        desired = desired_replicas(item)
        state = runtime_state(item)
        target = int(item["replicas"])
        item_active = (
            desired == target
            and state is not None
            and state["spec"] == target
            and state["ready"] == target
            and state["available"] == target
        )
        item_stopped = desired == 0 and (
            state is None or (state["spec"] == 0 and state["ready"] == 0)
        )
        active = active and item_active
        stopped = stopped and item_stopped
    return {"active": active, "stopped": stopped}


def verify_exact_source_mode(source_name, source, target_name, target):
    source_state = profile_state(source)
    target_state = profile_state(target)
    if not source_state["active"] or not target_state["stopped"]:
        raise SwitchError(
            f"execution requires exact {source_name} active and {target_name} stopped; "
            f"observed {source_name}={source_state}, {target_name}={target_state}"
        )
    print(f"Active-mode signature: {source_name} active; {target_name} stopped")


def verify_gpu_operator_ready():
    policy = oc_json(
        ["-n", "nvidia-gpu-operator", "get", "clusterpolicy", "gpu-cluster-policy"],
        required=False,
    )
    if policy is None:
        raise SwitchError("GPU Operator ClusterPolicy gpu-cluster-policy is absent")
    status = policy.get("status", {})
    state = str(status.get("state", status.get("status", ""))).lower()
    if state != "ready":
        raise SwitchError(f"GPU Operator ClusterPolicy is not ready: state={state or 'unknown'}")
    print("GPU Operator ClusterPolicy: ready")


def verify_bound_pvcs(config):
    failed = []
    for namespace in sorted({row["namespace"] for row in config["retained_data_planes"]}):
        pvcs = oc_json(["-n", namespace, "get", "pvc"], required=False)
        if pvcs is None:
            raise SwitchError(f"unable to inspect retained PVCs in namespace {namespace}")
        for pvc in pvcs.get("items", []):
            phase = pvc.get("status", {}).get("phase", "Unknown")
            if phase != "Bound":
                failed.append(f"{namespace}/{pvc.get('metadata', {}).get('name', '?')}={phase}")
    if failed:
        raise SwitchError(f"retained PVC gate failed: {', '.join(failed)}")
    print("Retained PVC gate: all discovered claims are Bound")


def verify_capacity_before_mutation(source, target, capacity):
    active = total_active_gpu_requests()
    source_active = current_profile_runtime_request(source)
    target_request = profile_gpu_request(target)
    unrelated = active - source_active
    if unrelated < 0:
        raise SwitchError("active GPU accounting is inconsistent with the source profile")
    if unrelated:
        raise SwitchError(
            f"unexpected non-source GPU requests={unrelated}; no source controllers were changed"
        )
    if unrelated + target_request > capacity:
        raise SwitchError(
            f"pre-switch GPU request would exceed capacity: unrelated={unrelated}, "
            f"target={target_request}, capacity={capacity}"
        )
    print(
        f"Pre-switch GPU gate: active={active}, source={source_active}, "
        f"unrelated={unrelated}, target={target_request}, capacity={capacity}"
    )


def mutate_group(group, replicas):
    for item in group:
        args = mutation(item, replicas if replicas == 0 else int(item["replicas"]))
        print(f"$ {shell_join(args)}")
        command(args)


def wait_group(group, replicas, timeout):
    for item in group:
        wait_for(item, replicas if replicas == 0 else int(item["replicas"]), timeout)


def apply_group(group, replicas, timeout):
    selectors = capture_pod_selectors(group) if replicas == 0 else []
    mutate_group(group, replicas)
    wait_group(group, replicas, timeout)
    if replicas == 0:
        wait_all_terminated(selectors, timeout)


def restore_source_profile(source_name, source, target_name, target, node, timeout, schedule_timeout):
    print(f"\nRECOVERY — stop partial {target_name} and restore recorded {source_name}", file=sys.stderr)
    for group in groups(target["controllers"], "stop_order"):
        apply_group(group, 0, timeout)

    uncordoned = False
    try:
        args_uncordon = ["oc", "adm", "uncordon", node]
        print(f"$ {shell_join(args_uncordon)}", file=sys.stderr)
        uncordoned = True
        command(args_uncordon)
        for group in groups(source["controllers"], "start_order"):
            mutate_group(group, -1)
        wait_all_scheduled(source["controllers"], node, schedule_timeout)
    finally:
        if uncordoned:
            args_cordon = ["oc", "adm", "cordon", node]
            print(f"$ {shell_join(args_cordon)}", file=sys.stderr)
            command(args_cordon)

    for group in groups(source["controllers"], "start_order"):
        wait_group(group, -1, timeout)
    print(
        f"RECOVERY READY: {source_name} controllers restored and {node} re-cordoned; "
        "functional tests are still required.",
        file=sys.stderr,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Plan or execute a data-preserving GPU demo profile switch."
    )
    parser.add_argument("target", choices=("base-cvd", "warehouse"))
    parser.add_argument("--execute", action="store_true", help="perform the reviewed replica and bounded node scheduling changes")
    parser.add_argument("--ack", default="", help="exact acknowledgement token from profiles.yaml")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds allowed for each controller readiness gate")
    parser.add_argument(
        "--schedule-timeout",
        type=int,
        default=120,
        help="seconds allowed for all target GPU pods to bind before the configured GPU node is re-cordoned",
    )
    args = parser.parse_args(sys.argv[2:])
    if args.timeout < 60 or args.timeout > 3600:
        raise SwitchError("--timeout must be between 60 and 3600 seconds")
    if args.schedule_timeout < 30 or args.schedule_timeout > 300:
        raise SwitchError("--schedule-timeout must be between 30 and 300 seconds")

    profile_path = Path(sys.argv[1])
    config = json.loads(profile_path.read_text(encoding="utf-8"))
    if int(config.get("schema_version", 0)) != 1:
        raise SwitchError("unsupported profiles.yaml schema")
    capacity = int(config["cluster"]["gpu_capacity"])
    for name, profile in config["profiles"].items():
        validate_profile_shape(name, profile, capacity)

    target_name = args.target
    source_name = "warehouse" if target_name == "base-cvd" else "base-cvd"
    target = config["profiles"][target_name]
    source = config["profiles"][source_name]

    verify_context(config)
    verify_releases(config)
    verify_controller_lock("base-cvd", config["profiles"]["base-cvd"])
    print_plan(config, target_name, target, source_name, source)
    if not args.execute:
        return

    token = config["safety"]["execution_acknowledgement"]
    if args.ack != token:
        raise SwitchError(f"--execute requires --ack {token}")
    if not source.get("resolved", False) or not target.get("resolved", False):
        raise SwitchError("both profile controller inventories must be resolved before execution")
    if not config["profiles"]["warehouse"].get("controller_inventory_complete", False):
        raise SwitchError("Warehouse controller_inventory_complete is false")
    if not config["profiles"]["warehouse"].get("retained_data_plane_registered", False):
        raise SwitchError("Warehouse release and PVC inventory are not registered in retained_data_planes")
    warehouse_namespaces = {
        item["namespace"] for item in config["profiles"]["warehouse"]["controllers"]
    }
    retained_namespaces = {item["namespace"] for item in config["retained_data_planes"]}
    if not warehouse_namespaces.issubset(retained_namespaces):
        missing = sorted(warehouse_namespaces - retained_namespaces)
        raise SwitchError(
            f"Warehouse controller namespaces are missing from retained_data_planes: {missing}"
        )
    for item in source["controllers"] + target["controllers"]:
        if not item.get("resolved", False):
            raise SwitchError(f"unresolved controller blocks execution: {item['namespace']} {item['kind']}/{item['name']}")
    verify_controller_lock("warehouse", config["profiles"]["warehouse"])
    verify_exact_source_mode(source_name, source, target_name, target)
    verify_gpu_operator_ready()
    verify_bound_pvcs(config)
    verify_capacity_before_mutation(source, target, capacity)

    print("\nEXECUTE — approved bounded mode switch")
    node = config["cluster"]["gpu_node"]
    transaction_started = False
    try:
        transaction_started = True
        for group in groups(source["controllers"], "stop_order"):
            apply_group(group, 0, args.timeout)

        # Recheck after shutdown in case an unrelated GPU pod appeared between
        # the preflight and the first mutation.
        used = total_active_gpu_requests()
        if used + profile_gpu_request(target) > capacity:
            raise SwitchError(
                f"post-shutdown GPU requests={used} plus target={profile_gpu_request(target)} "
                f"exceed capacity={capacity}"
            )

        uncordoned = False
        try:
            args_uncordon = ["oc", "adm", "uncordon", node]
            print(f"$ {shell_join(args_uncordon)}")
            # Mark the scheduling window before invoking oc. If the client is
            # interrupted after the API accepts the uncordon, the finally path
            # still issues the idempotent re-cordon.
            uncordoned = True
            command(args_uncordon)
            for group in groups(target["controllers"], "start_order"):
                mutate_group(group, -1)
            wait_all_scheduled(target["controllers"], node, args.schedule_timeout)
        finally:
            if uncordoned:
                args_cordon = ["oc", "adm", "cordon", node]
                print(f"$ {shell_join(args_cordon)}")
                command(args_cordon)

        for group in groups(target["controllers"], "start_order"):
            wait_group(group, -1, args.timeout)
    except Exception as primary:
        if transaction_started:
            try:
                restore_source_profile(
                    source_name,
                    source,
                    target_name,
                    target,
                    node,
                    args.timeout,
                    args.schedule_timeout,
                )
            except Exception as recovery:
                raise SwitchError(
                    f"mode switch failed ({primary}); automatic recovery also failed ({recovery}). "
                    f"Confirm {node} is cordoned and follow the bounded recovery runbook."
                ) from recovery
        raise SwitchError(f"mode switch failed; recorded {source_name} was restored: {primary}") from primary

    verify_context(config)
    for item in source["controllers"]:
        wait_for(item, 0, 60)
    for item in target["controllers"]:
        wait_for(item, int(item["replicas"]), 60)
    print(
        f"CONTROLLERS READY: {target_name} is Ready and {node} is re-cordoned. "
        "No persistent data object was deleted. Run the target functional tests before declaring the demo Ready."
    )


def interrupted(signum, _frame):
    raise SwitchError(f"interrupted by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        main()
    except (SwitchError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
PY
