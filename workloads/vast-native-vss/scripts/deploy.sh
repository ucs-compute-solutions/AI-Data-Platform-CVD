#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

env_file=""
pipeline_secret_file=""
receipt_sha256=""
apply=false
while (( $# )); do
  case "$1" in
    --env) env_file="${2:-}"; shift 2 ;;
    --pipeline-secret-file) pipeline_secret_file="${2:-}"; shift 2 ;;
    --receipt-sha256) receipt_sha256="${2:-}"; shift 2 ;;
    --apply) apply=true; shift ;;
    *) die "usage: $0 --env <release-inputs.env> --pipeline-secret-file <private-file> [--receipt-sha256 <sha256>] [--apply]" ;;
  esac
done
[[ -n "${env_file}" && -n "${pipeline_secret_file}" ]] || \
  die "usage: $0 --env <release-inputs.env> --pipeline-secret-file <private-file> [--receipt-sha256 <sha256>] [--apply]"
for command_name in awk helm oc python3 sha256sum vastde; do require_command "${command_name}"; done
[[ -f "${pipeline_secret_file}" && ! -L "${pipeline_secret_file}" && -r "${pipeline_secret_file}" ]] || \
  die "pipeline Secret file must be a readable regular file, not a symbolic link: ${pipeline_secret_file}"
python3 - "${pipeline_secret_file}" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
metadata = os.stat(path, follow_symlinks=False)
mode = stat.S_IMODE(metadata.st_mode)
if not stat.S_ISREG(metadata.st_mode):
    raise SystemExit(f"pipeline Secret input is not a regular file: {path}")
if metadata.st_uid != os.geteuid():
    raise SystemExit(f"pipeline Secret input is not owned by the current user: {path}")
if mode & 0o077:
    raise SystemExit(
        f"pipeline Secret input grants group or other permissions ({mode:03o}): {path}"
    )
PY

load_env "${env_file}"
require_application_digests
verify_context

receipt_file="${CVD_RENDER_DIR}/reviewed-artifacts.sha256"
[[ -r "${receipt_file}" ]] || \
  die "reviewed render receipt is unavailable; run render.sh and review its output first"
(
  cd "${CVD_RENDER_DIR}"
  sha256sum --check reviewed-artifacts.sha256 >/dev/null
) || die "a rendered artifact changed after review; rerender and review again"
actual_receipt_sha256="$(sha256sum "${receipt_file}" | awk '{print $1}')"
input_receipt_file="${CVD_RENDER_DIR}/release-inputs.sha256"
[[ -r "${input_receipt_file}" ]] || \
  die "reviewed release-input digest is unavailable; rerun render.sh"
expected_input_sha256="$(tr -d '[:space:]' <"${input_receipt_file}")"
[[ "${expected_input_sha256}" =~ ^[0-9a-f]{64}$ ]] || \
  die "reviewed release-input digest is invalid; rerun render.sh"
actual_input_sha256="$(sha256sum "${env_file}" | awk '{print $1}')"
[[ "${actual_input_sha256}" == "${expected_input_sha256}" ]] || \
  die "release-inputs.env changed after review; rerender and review again"
chart_version="$(lock_value applicationChart.version)"
chart_archive="${CVD_RENDER_DIR}/vast-vss-app-${chart_version}.tgz"
[[ -r "${chart_archive}" ]] || die "reviewed chart archive is unavailable: ${chart_archive}"

for name in video-segmenter video-reasoner video-embedder video-vastdb-writer; do
  vastde_has_name functions "${name}" || die "required function is absent: ${name}"
done
for name in video-chunk-land-trigger video-segment-land-trigger; do
  vastde_has_name triggers "${name}" || die "required trigger is absent: ${name}"
done
vastde_has_name pipelines "${CVD_PIPELINE_NAME}" && \
  die "pipeline already exists and must be upgraded through a separately reviewed procedure: ${CVD_PIPELINE_NAME}"

printf 'Planned apply order:\n'
printf '  1. VAST S3/VASTDB views and Event Broker topic in %s\n' "${CVD_CONTROL_NAMESPACE}"
printf '  2. Helm release %s in %s\n' "${CVD_RELEASE}" "${CVD_NAMESPACE}"
printf '  3. DataEngine pipeline %s using the private Secret file\n' "${CVD_PIPELINE_NAME}"

if [[ "${apply}" != "true" ]]; then
  printf 'Reviewed receipt SHA-256: %s\n' "${actual_receipt_sha256}"
  printf 'PREVIEW ONLY: no resources were changed. Rerun with --apply after review and approval.\n'
  exit 0
fi
[[ "${receipt_sha256}" =~ ^[0-9a-f]{64}$ ]] || \
  die "--apply requires --receipt-sha256 with the digest printed by render.sh"
[[ "${receipt_sha256}" == "${actual_receipt_sha256}" ]] || \
  die "receipt digest mismatch; the approved render is not the render being applied"

oc get namespace "${CVD_NAMESPACE}" >/dev/null || \
  die "apply the reviewed namespace manifest and complete the VAST namespace/registry assignments first"
oc -n "${CVD_NAMESPACE}" get secret "${CVD_IMAGE_PULL_SECRET}" >/dev/null
oc -n "${CVD_NAMESPACE}" get secret "${CVD_RUNTIME_SECRET}" >/dev/null

oc apply -f "${CVD_RENDER_DIR}/storage-resources.yaml"
oc apply -f "${CVD_RENDER_DIR}/kafka-topic.yaml"
helm upgrade --install "${CVD_RELEASE}" "${chart_archive}" \
  -n "${CVD_NAMESPACE}" -f "${CVD_RENDER_DIR}/site-values.yaml" \
  --wait --timeout "${CVD_HELM_TIMEOUT}"

vastde pipelines create \
  --name "${CVD_PIPELINE_NAME}" \
  --config "@${CVD_RENDER_DIR}/pipeline.yaml" \
  --secret-file "${pipeline_secret_file}" \
  --deploy

printf 'PASS: application and DataEngine pipeline create requests completed.\n'
printf 'Run verify.sh and fresh MP4 functional acceptance before declaring the workload ready.\n'
