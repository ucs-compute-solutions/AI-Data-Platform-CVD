#!/usr/bin/env bash

set -euo pipefail

readonly CVD_VSS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CVD_REPO_ROOT="$(cd -- "${CVD_VSS_DIR}/../.." && pwd)"
readonly CVD_SOURCE_LOCK="${CVD_VSS_DIR}/source-lock.yaml"
readonly CVD_SITE_VALUES_EXAMPLE="${CVD_VSS_DIR}/values/site-values.example.yaml"
readonly CVD_BOOTSTRAP_VALUES="${CVD_VSS_DIR}/values/bootstrap.yaml"
readonly CVD_STEADY_VALUES="${CVD_VSS_DIR}/values/steady-state.yaml"
readonly CVD_CRITIC_VALUES_EXAMPLE="${CVD_VSS_DIR}/values/critic.yaml"
readonly CVD_NAMESPACE_TEMPLATE="${CVD_VSS_DIR}/manifests/namespace-rbac.yaml.tpl"
readonly CVD_ROUTES_TEMPLATE="${CVD_VSS_DIR}/manifests/routes.yaml.tpl"
readonly CVD_OPENSHIFT_PATCH="${CVD_REPO_ROOT}/patches/nvidia-vss-3.2.1-search/openshift-v321.patch"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

load_env() {
  local env_file="$1"
  [[ -r "${env_file}" ]] || die "environment file is not readable: ${env_file}"
  # shellcheck disable=SC1090
  source "${env_file}"
  local required=(
    CVD_EXPECTED_API CVD_EXPECTED_CONTEXT CVD_NAMESPACE CVD_RELEASE
    CVD_SOURCE_DIR CVD_STORAGE_CLASS CVD_ROUTE_HOST CVD_STREAMER_ROUTE_HOST
    CVD_NGC_PULL_SECRET CVD_NGC_API_SECRET CVD_GPU_NODE_SELECTOR_KEY
    CVD_GPU_NODE_SELECTOR_VALUE CVD_CPU_NODE_SELECTOR_KEY
    CVD_CPU_NODE_SELECTOR_VALUE CVD_LLM_MODEL CVD_LLM_BASE_URL
    CVD_KUBE_VERSION CVD_HELM_TIMEOUT CVD_RENDER_DIR
  )
  local name value
  for name in "${required[@]}"; do
    value="${!name:-}"
    [[ -n "${value}" ]] || die "required variable is empty: ${name}"
    [[ "${value}" != *'<'* && "${value}" != *'>'* ]] || \
      die "replace the placeholder for ${name} in ${env_file}"
  done
  [[ "${CVD_SOURCE_DIR}" == /* ]] || die "CVD_SOURCE_DIR must be an absolute path"
  [[ "${CVD_RENDER_DIR}" == /* ]] || die "CVD_RENDER_DIR must be an absolute path"
  [[ "${CVD_ROUTE_HOST}" != "${CVD_STREAMER_ROUTE_HOST}" ]] || \
    die "CVD_ROUTE_HOST and CVD_STREAMER_ROUTE_HOST must be different DNS names"
  [[ "${CVD_LLM_BASE_URL}" =~ ^https?://[A-Za-z0-9._-]+:[0-9]+$ ]] || \
    die "CVD_LLM_BASE_URL must be an http(s) service origin with explicit port and no /v1, path, trailing slash, query, or fragment"
  export "${required[@]}"
}

verify_context() {
  local identity server context
  identity="$(oc whoami)"
  server="$(oc whoami --show-server)"
  context="$(oc config current-context)"
  printf 'OpenShift identity: %s\n' "${identity}"
  printf 'OpenShift API: %s\n' "${server}"
  printf 'Current context: %s\n' "${context}"
  [[ "${server}" == "${CVD_EXPECTED_API}" ]] || die "OpenShift API does not match CVD_EXPECTED_API"
  [[ "${context}" == "${CVD_EXPECTED_CONTEXT}" ]] || die "OpenShift context does not match CVD_EXPECTED_CONTEXT"
}

verify_kubernetes_version() {
  local actual
  actual="$(oc version -o json | python3 -c '
import json, re, sys
data = json.load(sys.stdin)
version = data.get("serverVersion", {}).get("gitVersion", "")
match = re.search(r"v?(\d+)\.(\d+)", version)
if not match:
    raise SystemExit("unable to parse the Kubernetes server version")
print(f"{match.group(1)}.{match.group(2)}")
')"
  python3 - "${CVD_KUBE_VERSION}" "${actual}" <<'PY'
import re
import sys

expected, actual = sys.argv[1:]
match = re.search(r"v?(\d+)\.(\d+)", expected)
if not match:
    raise SystemExit(f"ERROR: unable to parse CVD_KUBE_VERSION: {expected}")
normalized = f"{match.group(1)}.{match.group(2)}"
if normalized != actual:
    raise SystemExit(
        f"ERROR: Kubernetes server version {actual} does not match "
        f"CVD_KUBE_VERSION {normalized}"
    )
print(f"Kubernetes server version: {actual}")
PY
}

verify_nim_operator() {
  local expected work_dir deployments_file csv_file
  expected="$(lock_value nimOperatorVersion)"
  [[ -n "${expected}" ]] || die "nimOperatorVersion is absent from source-lock.yaml"
  for crd in nimservices.apps.nvidia.com nimcaches.apps.nvidia.com; do
    served="$(oc get crd "${crd}" \
      -o jsonpath='{.spec.versions[?(@.name=="v1alpha1")].served}' 2>/dev/null || true)"
    [[ "${served}" == "true" ]] || die "${crd} does not serve apps.nvidia.com/v1alpha1"
  done
  work_dir="$(mktemp -d)"
  deployments_file="${work_dir}/deployments.json"
  csv_file="${work_dir}/csv.json"
  oc get deployment -A -o json >"${deployments_file}"
  if oc api-resources --api-group=operators.coreos.com | grep -q '^clusterserviceversions'; then
    oc get csv -A -o json >"${csv_file}"
  else
    printf '{"items":[]}\n' >"${csv_file}"
  fi
  python3 - "${deployments_file}" "${csv_file}" "${expected}" <<'PY'
import json
import sys

deployments_path, csv_path, expected = sys.argv[1:]
deployments = json.load(open(deployments_path, encoding="utf-8")).get("items", [])
csvs = json.load(open(csv_path, encoding="utf-8")).get("items", [])
image_suffix = f"k8s-nim-operator:v{expected}"
deployment_match = any(
    any(container.get("image", "").split("@", 1)[0].endswith(image_suffix)
        for container in item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []))
    and int(item.get("spec", {}).get("replicas", 1) or 0) > 0
    and int(item.get("status", {}).get("readyReplicas", 0) or 0)
        == int(item.get("spec", {}).get("replicas", 1) or 0)
    and int(item.get("status", {}).get("availableReplicas", 0) or 0)
        == int(item.get("spec", {}).get("replicas", 1) or 0)
    for item in deployments
)
csv_match = any(
    str(item.get("spec", {}).get("version", "")) == expected
    and item.get("status", {}).get("phase") == "Succeeded"
    and "nim" in (
        item.get("metadata", {}).get("name", "")
        + " "
        + item.get("spec", {}).get("display", "")
    ).lower()
    for item in csvs
)
if not (deployment_match or csv_match):
    raise SystemExit(
        f"ERROR: validated NVIDIA NIM Operator {expected} was not found as a Ready OLM CSV or pinned deployment image"
    )
print(f"NVIDIA NIM Operator version: {expected}")
PY
  rm -rf -- "${work_dir}"
}

verify_node_pool() {
  local selector="$1"
  local role="$2"
  local minimum_gpus="${3:-0}"
  local work_dir nodes_file
  work_dir="$(mktemp -d)"
  nodes_file="${work_dir}/nodes.json"
  oc get nodes -l "${selector}" -o json >"${nodes_file}"
  python3 - "${nodes_file}" "${role}" "${minimum_gpus}" <<'PY'
import json
import sys

path, role, minimum_gpus = sys.argv[1], sys.argv[2], int(sys.argv[3])
data = json.load(open(path, encoding="utf-8"))
eligible = []
gpu_total = 0
for node in data.get("items", []):
    spec = node.get("spec", {})
    conditions = {
        item.get("type"): item.get("status")
        for item in node.get("status", {}).get("conditions", [])
    }
    blocking_taints = [
        taint for taint in spec.get("taints", [])
        if taint.get("effect") in {"NoSchedule", "NoExecute"}
        and not (role == "GPU" and taint.get("key") == "nvidia.com/gpu")
    ]
    if conditions.get("Ready") != "True" or spec.get("unschedulable") or blocking_taints:
        continue
    eligible.append(node["metadata"]["name"])
    gpu_total += int(node.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu", "0"))
if not eligible:
    raise SystemExit(f"ERROR: no Ready, schedulable, untainted {role} node matches the configured selector")
if gpu_total < minimum_gpus:
    raise SystemExit(
        f"ERROR: {role} pool exposes {gpu_total} allocatable GPU(s); "
        f"the profile requires at least {minimum_gpus}"
    )
print(f"{role} nodes: {len(eligible)} eligible; allocatable GPUs: {gpu_total}")
PY
  rm -rf -- "${work_dir}"
}

require_available_gpus() {
  local selector="$1"
  local required="$2"
  local work_dir nodes_file pods_file
  [[ "${required}" =~ ^[0-9]+$ ]] || die "required GPU count must be an integer"
  (( required > 0 )) || return 0
  work_dir="$(mktemp -d)"
  nodes_file="${work_dir}/nodes.json"
  pods_file="${work_dir}/pods.json"
  oc get nodes -l "${selector}" -o json >"${nodes_file}"
  oc get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed -o json >"${pods_file}"
  python3 - "${nodes_file}" "${pods_file}" "${required}" <<'PY'
import json
import sys

nodes_path, pods_path, required = sys.argv[1], sys.argv[2], int(sys.argv[3])
nodes = json.load(open(nodes_path, encoding="utf-8")).get("items", [])
pods = json.load(open(pods_path, encoding="utf-8")).get("items", [])
eligible = {}
for node in nodes:
    spec = node.get("spec", {})
    conditions = {
        item.get("type"): item.get("status")
        for item in node.get("status", {}).get("conditions", [])
    }
    blocking = any(
        taint.get("effect") in {"NoSchedule", "NoExecute"}
        and taint.get("key") != "nvidia.com/gpu"
        for taint in spec.get("taints", [])
    )
    if conditions.get("Ready") == "True" and not spec.get("unschedulable") and not blocking:
        eligible[node["metadata"]["name"]] = int(
            node.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu", "0")
        )

def requested(container):
    resources = container.get("resources", {})
    requests = resources.get("requests", {})
    limits = resources.get("limits", {})
    return int(requests.get("nvidia.com/gpu", limits.get("nvidia.com/gpu", "0")))

used = {name: 0 for name in eligible}
for pod in pods:
    node_name = pod.get("spec", {}).get("nodeName")
    if node_name not in eligible:
        continue
    containers = pod.get("spec", {}).get("containers", [])
    init_containers = pod.get("spec", {}).get("initContainers", [])
    regular = sum(requested(container) for container in containers)
    init_peak = max((requested(container) for container in init_containers), default=0)
    used[node_name] += max(regular, init_peak)

allocatable = sum(eligible.values())
requested_total = sum(used.values())
available = allocatable - requested_total
print(
    f"GPU capacity: {allocatable} allocatable, {requested_total} requested, "
    f"{available} available"
)
if available < required:
    raise SystemExit(
        f"ERROR: {required} additional GPU(s) are required, but only {available} are available"
    )
PY
  rm -rf -- "${work_dir}"
}

compute_review_digest() {
  local env_file="$1"
  local mode="$2"
  local chart="$3"
  python3 - "${env_file}" "${mode}" "${chart}" "${CVD_RENDER_DIR}" \
    "${CVD_SOURCE_LOCK}" "${CVD_OPENSHIFT_PATCH}" \
    "${CVD_SITE_VALUES_EXAMPLE}" "${CVD_BOOTSTRAP_VALUES}" \
    "${CVD_STEADY_VALUES}" "${CVD_CRITIC_VALUES_EXAMPLE}" <<'PY'
import hashlib
import pathlib
import sys

(
    env_file,
    mode,
    chart,
    render_dir,
    source_lock,
    openshift_patch,
    site_example,
    bootstrap_values,
    steady_values,
    critic_example,
) = sys.argv[1:]

digest = hashlib.sha256()

def add_bytes(label, value):
    digest.update(label.encode("utf-8") + b"\0")
    digest.update(value)
    digest.update(b"\0")

add_bytes("mode", mode.encode("utf-8"))
for label, filename in (
    ("release-inputs", env_file),
    ("source-lock", source_lock),
    ("openshift-patch", openshift_patch),
    ("site-values-example", site_example),
    ("bootstrap-values", bootstrap_values),
    ("steady-values", steady_values),
    ("critic-values-example", critic_example),
    ("site-values", str(pathlib.Path(render_dir) / "site-values.yaml")),
    ("critic-values", str(pathlib.Path(render_dir) / "critic.yaml")),
    ("namespace-rbac", str(pathlib.Path(render_dir) / "namespace-rbac.yaml")),
    ("routes", str(pathlib.Path(render_dir) / "routes.yaml")),
    ("rendered-manifest", str(pathlib.Path(render_dir) / f"nvidia-vss-search-{mode}.yaml")),
):
    path = pathlib.Path(filename)
    if not path.is_file():
        raise SystemExit(f"ERROR: reviewed input is unavailable: {path}")
    add_bytes(label, path.read_bytes())

chart_root = pathlib.Path(chart)
if not chart_root.is_dir():
    raise SystemExit(f"ERROR: reviewed chart snapshot is unavailable: {chart_root}")
for path in sorted(item for item in chart_root.rglob("*") if item.is_file()):
    add_bytes(f"chart/{path.relative_to(chart_root).as_posix()}", path.read_bytes())

print(digest.hexdigest())
PY
}

lock_value() {
  local key="$1"
  awk -F ': ' -v key="${key}" '$1 == key {print substr($0, length(key) + 3); exit}' "${CVD_SOURCE_LOCK}"
}

render_template() {
  local source="$1"
  local destination="$2"
  python3 - "${source}" "${destination}" <<'PY'
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
tokens = {
    "__NAMESPACE__": os.environ["CVD_NAMESPACE"],
    "__STORAGE_CLASS__": os.environ["CVD_STORAGE_CLASS"],
    "__ROUTE_HOST__": os.environ["CVD_ROUTE_HOST"],
    "__STREAMER_ROUTE_HOST__": os.environ["CVD_STREAMER_ROUTE_HOST"],
    "__NGC_PULL_SECRET__": os.environ["CVD_NGC_PULL_SECRET"],
    "__NGC_API_SECRET__": os.environ["CVD_NGC_API_SECRET"],
    "__GPU_NODE_SELECTOR_KEY__": os.environ["CVD_GPU_NODE_SELECTOR_KEY"],
    "__GPU_NODE_SELECTOR_VALUE__": os.environ["CVD_GPU_NODE_SELECTOR_VALUE"],
    "__CPU_NODE_SELECTOR_KEY__": os.environ["CVD_CPU_NODE_SELECTOR_KEY"],
    "__CPU_NODE_SELECTOR_VALUE__": os.environ["CVD_CPU_NODE_SELECTOR_VALUE"],
    "__LLM_MODEL__": os.environ["CVD_LLM_MODEL"],
    "__LLM_BASE_URL__": os.environ["CVD_LLM_BASE_URL"],
}
text = source.read_text(encoding="utf-8")
for token, value in tokens.items():
    text = text.replace(token, value)
if "__" in text:
    unresolved = sorted({part for part in text.split() if part.startswith("__") or part.endswith("__")})
    raise SystemExit(f"unresolved template token(s): {unresolved}")
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(text, encoding="utf-8")
PY
}

chart_dir() {
  chart_dir_for "${CVD_SOURCE_DIR}"
}

chart_dir_for() {
  local source_root="$1"
  printf '%s/deploy/helm/developer-profiles/dev-profile-search\n' "${source_root}"
}
