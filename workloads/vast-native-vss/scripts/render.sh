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

"${SCRIPT_DIR}/preflight.sh" --env "${env_file}"
load_env "${env_file}"
require_application_digests
for command_name in awk rm sha256sum; do require_command "${command_name}"; done
umask 077
mkdir -p "${CVD_RENDER_DIR}"

chart_version="$(lock_value applicationChart.version)"
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
[[ "${actual_chart_version}" == "${chart_version}" ]] || \
  die "chart version mismatch: expected ${chart_version}, found ${actual_chart_version}"

render_template "${COMPANION_DIR}/values/site-values.example.yaml" "${CVD_RENDER_DIR}/site-values.yaml"
render_template "${COMPANION_DIR}/manifests/namespace.yaml.tpl" "${CVD_RENDER_DIR}/namespace.yaml"
render_template "${COMPANION_DIR}/manifests/storage-resources.yaml.tpl" "${CVD_RENDER_DIR}/storage-resources.yaml"
render_template "${COMPANION_DIR}/manifests/kafka-topic.yaml.tpl" "${CVD_RENDER_DIR}/kafka-topic.yaml"
render_template "${COMPANION_DIR}/manifests/pipeline.yaml.tpl" "${CVD_RENDER_DIR}/pipeline.yaml"

helm lint "${CHART_DIR}" -f "${CVD_RENDER_DIR}/site-values.yaml"
helm template "${CVD_RELEASE}" "${CHART_DIR}" \
  -n "${CVD_NAMESPACE}" -f "${CVD_RENDER_DIR}/site-values.yaml" \
  >"${CVD_RENDER_DIR}/application.yaml"
rm -f "${CVD_RENDER_DIR}/vast-vss-app-"*.tgz
helm package "${CHART_DIR}" --destination "${CVD_RENDER_DIR}" >/dev/null
chart_archive="${CVD_RENDER_DIR}/vast-vss-app-${chart_version}.tgz"
[[ -r "${chart_archive}" ]] || die "rendered chart archive is unavailable: ${chart_archive}"

# Bind the private site-input file to the reviewed artifact set without copying
# its contents into the render directory.
sha256sum "${env_file}" | awk '{print $1}' >"${CVD_RENDER_DIR}/release-inputs.sha256"

rendered_artifacts=(
  "${CVD_RENDER_DIR}/namespace.yaml"
  "${CVD_RENDER_DIR}/site-values.yaml"
  "${CVD_RENDER_DIR}/application.yaml"
  "${CVD_RENDER_DIR}/storage-resources.yaml"
  "${CVD_RENDER_DIR}/kafka-topic.yaml"
  "${CVD_RENDER_DIR}/pipeline.yaml"
)
if grep -En '(^|[/:])latest([[:space:]"@]|$)' "${rendered_artifacts[@]}"; then
  die "rendered artifacts contain a latest reference"
fi
if grep -En '__[A-Z0-9_]+__|<[^>]+>' "${rendered_artifacts[@]}"; then
  die "rendered artifacts contain unresolved placeholders"
fi

oc apply --dry-run=client --validate=true -f "${CVD_RENDER_DIR}/namespace.yaml" >/dev/null
if oc get namespace "${CVD_NAMESPACE}" >/dev/null 2>&1; then
  oc apply --dry-run=server -f "${CVD_RENDER_DIR}/application.yaml" >/dev/null
else
  oc apply --dry-run=client --validate=true -f "${CVD_RENDER_DIR}/application.yaml" >/dev/null
fi
oc apply --dry-run=server -f "${CVD_RENDER_DIR}/storage-resources.yaml" >/dev/null
oc apply --dry-run=server -f "${CVD_RENDER_DIR}/kafka-topic.yaml" >/dev/null

(
  cd "${CVD_RENDER_DIR}"
  sha256sum namespace.yaml site-values.yaml application.yaml \
    storage-resources.yaml kafka-topic.yaml pipeline.yaml \
    release-inputs.sha256 \
    "vast-vss-app-${chart_version}.tgz" >reviewed-artifacts.sha256
)

receipt_digest="$(sha256sum "${CVD_RENDER_DIR}/reviewed-artifacts.sha256" | awk '{print $1}')"

printf 'PASS: Helm lint, render, placeholder audit, and dry-run validation completed.\n'
printf 'Review directory: %s\n' "${CVD_RENDER_DIR}"
printf 'Reviewed receipt SHA-256: %s\n' "${receipt_digest}"
printf 'No OpenShift, VAST, DataEngine, or Secret resources were changed.\n'
