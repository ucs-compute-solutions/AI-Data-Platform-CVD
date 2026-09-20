#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

env_file=""
apply=false
while (( $# )); do
  case "$1" in
    --env) env_file="${2:-}"; shift 2 ;;
    --apply) apply=true; shift ;;
    *) die "usage: $0 --env <release-inputs.env> [--apply]" ;;
  esac
done
[[ -n "${env_file}" ]] || die "usage: $0 --env <release-inputs.env> [--apply]"

"${SCRIPT_DIR}/preflight.sh" --env "${env_file}"
load_env "${env_file}"

functions=(video-segmenter video-reasoner video-embedder video-vastdb-writer)
sources=(
  "${CVD_SEGMENTER_ARTIFACT_SOURCE}"
  "${CVD_REASONER_ARTIFACT_SOURCE}"
  "${CVD_EMBEDDER_ARTIFACT_SOURCE}"
  "${CVD_WRITER_ARTIFACT_SOURCE}"
)
triggers=(video-chunk-land-trigger video-segment-land-trigger)
buckets=(video-chunks video-chunks-segments)

for name in "${functions[@]}"; do
  vastde_has_name functions "${name}" && \
    die "function already exists and must be reviewed instead of overwritten: ${name}"
done
for name in "${triggers[@]}"; do
  vastde_has_name triggers "${name}" && \
    die "trigger already exists and must be reviewed instead of overwritten: ${name}"
done

printf 'Planned DataEngine registration:\n'
for index in "${!functions[@]}"; do
  printf '  function %-22s registry=%s source=%s tag=%s\n' \
    "${functions[$index]}" "${CVD_REGISTRY_NAME}" "${sources[$index]}" "${CVD_FUNCTION_IMAGE_TAG}"
done
for index in "${!triggers[@]}"; do
  printf '  trigger  %-22s bucket=%s event=ObjectCreated:* topic=%s/%s\n' \
    "${triggers[$index]}" "${buckets[$index]}" "${CVD_BROKER_NAME}" "${CVD_TOPIC_NAME}"
done

if [[ "${apply}" != "true" ]]; then
  printf 'PREVIEW ONLY: rerun with --apply after image, trigger, and change review.\n'
  exit 0
fi

for index in "${!functions[@]}"; do
  vastde functions create \
    --name "${functions[$index]}" \
    --container-registry "${CVD_REGISTRY_NAME}" \
    --artifact-source "${sources[$index]}" \
    --artifact-type image \
    --image-tag "${CVD_FUNCTION_IMAGE_TAG}"
done

for index in "${!triggers[@]}"; do
  vastde triggers create \
    --name "${triggers[$index]}" \
    --type Element \
    --source-bucket "${buckets[$index]}" \
    --events 'ObjectCreated:*' \
    --broker-name "${CVD_BROKER_NAME}" \
    --broker-type Internal \
    --topic "${CVD_TOPIC_NAME}"
done

for name in "${functions[@]}"; do
  vastde_has_name functions "${name}" || die "function was not registered: ${name}"
done
for name in "${triggers[@]}"; do
  vastde_has_name triggers "${name}" || die "trigger was not registered: ${name}"
done
printf 'PASS: four functions and two S3 triggers are registered.\n'
