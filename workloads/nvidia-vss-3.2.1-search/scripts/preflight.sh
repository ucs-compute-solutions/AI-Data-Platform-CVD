#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

env_file=""
while (( $# )); do
  case "$1" in
    --env) env_file="${2:-}"; shift 2 ;;
    *) die "usage: $0 --env <release-inputs.env>" ;;
  esac
done
[[ -n "${env_file}" ]] || die "usage: $0 --env <release-inputs.env>"

for command_name in oc git helm python3 awk grep tar; do require_command "${command_name}"; done
load_env "${env_file}"
verify_context
verify_kubernetes_version

[[ -d "${CVD_SOURCE_DIR}/.git" ]] || die "pinned NVIDIA source is not a Git checkout: ${CVD_SOURCE_DIR}"
[[ -r "${CVD_OPENSHIFT_PATCH}" ]] || die "OpenShift patch is unavailable: ${CVD_OPENSHIFT_PATCH}"
expected_commit="$(lock_value commit)"
expected_chart="$(lock_value chartVersion)"
expected_ngc_cli="$(lock_value ngcCliVersion)"
expected_ngc_cli_sha="$(lock_value ngcCliSha256)"
actual_commit="$(git -C "${CVD_SOURCE_DIR}" rev-parse HEAD)"
[[ "${actual_commit}" == "${expected_commit}" ]] || \
  die "source commit mismatch: expected ${expected_commit}, found ${actual_commit}"
source_status="$(git -C "${CVD_SOURCE_DIR}" status --porcelain --untracked-files=all)"
[[ -z "${source_status}" ]] || \
  die "pinned NVIDIA source contains tracked or untracked changes; use a clean checkout"
git -C "${CVD_SOURCE_DIR}" apply --check "${CVD_OPENSHIFT_PATCH}" >/dev/null 2>&1 || \
  die "reviewed OpenShift patch does not apply cleanly to the pinned source"
grep -Fq "NGC_CLI_VERSION=\"${expected_ngc_cli}\"" "${CVD_OPENSHIFT_PATCH}" || \
  die "OpenShift patch does not carry the locked NGC CLI version"
grep -Fq "NGC_CLI_SHA256=\"${expected_ngc_cli_sha}\"" "${CVD_OPENSHIFT_PATCH}" || \
  die "OpenShift patch does not carry the locked NGC CLI checksum"

chart="$(chart_dir)"
[[ -r "${chart}/Chart.yaml" ]] || die "Search chart is unavailable: ${chart}"
actual_chart="$(awk -F ': *' '$1 == "version" {gsub(/["[:space:]]/, "", $2); print $2; exit}' "${chart}/Chart.yaml")"
[[ "${actual_chart}" == "${expected_chart}" ]] || \
  die "chart version mismatch: expected ${expected_chart}, found ${actual_chart:-<empty>}"

storage_provisioner="$(oc get storageclass "${CVD_STORAGE_CLASS}" -o jsonpath='{.provisioner}')"
[[ "${storage_provisioner}" == "csi.vastdata.com" ]] || \
  die "StorageClass ${CVD_STORAGE_CLASS} is not backed by csi.vastdata.com"
gpu_nodes="$(oc get nodes -l "${CVD_GPU_NODE_SELECTOR_KEY}=${CVD_GPU_NODE_SELECTOR_VALUE}" -o name)"
cpu_nodes="$(oc get nodes -l "${CVD_CPU_NODE_SELECTOR_KEY}=${CVD_CPU_NODE_SELECTOR_VALUE}" -o name)"
[[ -n "${gpu_nodes}" ]] || die "no node matches the configured GPU node selector"
[[ -n "${cpu_nodes}" ]] || die "no node matches the configured CPU node selector"
verify_node_pool "${CVD_GPU_NODE_SELECTOR_KEY}=${CVD_GPU_NODE_SELECTOR_VALUE}" "GPU" 3
verify_node_pool "${CVD_CPU_NODE_SELECTOR_KEY}=${CVD_CPU_NODE_SELECTOR_VALUE}" "CPU" 0
oc api-resources --api-group=route.openshift.io | grep -q '^routes'

if oc get namespace "${CVD_NAMESPACE}" >/dev/null 2>&1; then
  namespace_owner="$(oc get namespace "${CVD_NAMESPACE}" \
    -o jsonpath='{.metadata.labels.cvd\.cisco\.com/workload}')"
  [[ "${namespace_owner}" == "nvidia-vss-3.2.1-search" ]] || \
    die "existing namespace ${CVD_NAMESPACE} is not owned by this CVD workload"
  installed_releases="$(helm -n "${CVD_NAMESPACE}" list --all -q | sed '/^$/d')"
  unexpected_releases="$(printf '%s\n' "${installed_releases}" | \
    awk -v expected="${CVD_RELEASE}" '$0 != expected {print}')"
  [[ -z "${unexpected_releases}" ]] || \
    die "namespace ${CVD_NAMESPACE} contains another Helm release: ${unexpected_releases}"
  if ! printf '%s\n' "${installed_releases}" | grep -Fxq "${CVD_RELEASE}"; then
    existing_workloads="$(oc -n "${CVD_NAMESPACE}" get \
      deployment,statefulset,daemonset,job,cronjob,pod -o name 2>/dev/null || true)"
    [[ -z "${existing_workloads}" ]] || \
      die "namespace ${CVD_NAMESPACE} is not empty and has no matching Helm release"
  fi
fi

printf 'PASS: approved OpenShift context verified.\n'
printf 'PASS: clean NVIDIA source commit, chart version, and applicable OpenShift patch verified.\n'
printf 'PASS: Kubernetes version, VAST CSI StorageClass, schedulable node pools, namespace ownership, and Route API are verified.\n'
printf 'No OpenShift resources or Secrets were changed.\n'
