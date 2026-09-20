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

for command_name in awk oc git helm vastde python3 grep sha256sum; do require_command "${command_name}"; done
load_env "${env_file}"
verify_context

[[ -d "${CVD_SOURCE_DIR}/.git" ]] || die "VAST VSS source is not a Git checkout: ${CVD_SOURCE_DIR}"
expected_commit="$(lock_value source.commit)"
actual_commit="$(git -C "${CVD_SOURCE_DIR}" rev-parse HEAD)"
[[ "${actual_commit}" == "${expected_commit}" ]] || \
  die "source commit mismatch: expected ${expected_commit}, found ${actual_commit}"
[[ -z "$(git -C "${CVD_SOURCE_DIR}" status --porcelain --untracked-files=all)" ]] || \
  die "the pinned VAST source checkout must remain clean"
[[ -r "${CHART_DIR}/Chart.yaml" ]] || die "CVD VAST VSS chart is unavailable: ${CHART_DIR}"
expected_chart_version="$(lock_value applicationChart.version)"
actual_chart_version="$(python3 - "${CHART_DIR}/Chart.yaml" <<'PY'
import sys
from pathlib import Path

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.startswith("version:"):
        print(line.split(":", 1)[1].strip().strip("\"'"))
        break
else:
    raise SystemExit("chart version is absent")
PY
)"
[[ "${actual_chart_version}" == "${expected_chart_version}" ]] || \
  die "chart version mismatch: expected ${expected_chart_version}, found ${actual_chart_version}"

patch_paths=(
  "${COMPANION_DIR}/source-assets/patches/0001-openshift-hardening.patch"
  "${COMPANION_DIR}/source-assets/patches/0002-optional-services.patch"
  "${COMPANION_DIR}/source-assets/patches/0003-lightning-thinking-control.patch"
)
patch_locks=(
  reviewedPatches.openshiftHardening.sha256
  reviewedPatches.optionalSourceServices.sha256
  reviewedPatches.lightningThinkingControl.sha256
)
for index in "${!patch_paths[@]}"; do
  [[ -r "${patch_paths[$index]}" ]] || die "reviewed source patch is unavailable: ${patch_paths[$index]}"
  expected_patch_sha="$(lock_value "${patch_locks[$index]}")"
  actual_patch_sha="$(sha256sum "${patch_paths[$index]}" | awk '{print $1}')"
  [[ "${actual_patch_sha}" == "${expected_patch_sha}" ]] || \
    die "reviewed source patch checksum mismatch: ${patch_paths[$index]}"
done

if oc get namespace "${CVD_NAMESPACE}" >/dev/null 2>&1; then
  owner="$(oc get namespace "${CVD_NAMESPACE}" -o jsonpath='{.metadata.labels.cvd\.cisco\.com/workload}')"
  [[ "${owner}" == "vast-native-vss" ]] || \
    die "existing namespace ${CVD_NAMESPACE} is not owned by this CVD workload"
fi

oc get namespace "${CVD_CONTROL_NAMESPACE}" >/dev/null
oc get namespace "${CVD_NIM_NAMESPACE}" >/dev/null
for service in "${CVD_EMBEDDING_NIMSERVICE}" "${CVD_COSMOS_NIMSERVICE}" "${CVD_LLM_NIMSERVICE}"; do
  state="$(oc -n "${CVD_NIM_NAMESPACE}" get nimservice "${service}" -o jsonpath='{.status.state}')"
  [[ "${state}" == "Ready" ]] || die "NIMService ${service} is not Ready: ${state:-unknown}"
done

printf 'PASS: approved OpenShift context and namespaces verified.\n'
printf 'PASS: pinned VAST VSS source commit and CVD chart verified.\n'
printf 'PASS: reviewed source patch checksums verified.\n'
printf 'PASS: embedding, Cosmos Reason2, and Lightning NIM Services are Ready.\n'
printf 'No OpenShift, VAST, DataEngine, or Secret resources were changed.\n'
