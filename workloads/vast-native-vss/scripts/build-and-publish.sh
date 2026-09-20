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

for command_name in docker grep mktemp python3 rm sed tr vastde; do require_command "${command_name}"; done
load_env "${env_file}"
context="${CVD_BUILD_DIR}/prepared-vast-vss-source"
[[ -d "${context}/source-code/ingest" ]] || \
  die "prepared source is absent; run prepare-source.sh first"

function_names=(video-segmenter video-reasoner video-embedder vastdb-writer)
artifact_sources=(
  "${CVD_SEGMENTER_ARTIFACT_SOURCE}"
  "${CVD_REASONER_ARTIFACT_SOURCE}"
  "${CVD_EMBEDDER_ARTIFACT_SOURCE}"
  "${CVD_WRITER_ARTIFACT_SOURCE}"
)
image_refs=(
  "${CVD_BACKEND_REPOSITORY}:${CVD_FUNCTION_IMAGE_TAG}"
  "${CVD_FRONTEND_REPOSITORY}:${CVD_FUNCTION_IMAGE_TAG}"
)
for source in "${artifact_sources[@]}"; do
  image_refs+=("${CVD_REGISTRY_PUSH_HOST}/${source}:${CVD_FUNCTION_IMAGE_TAG}")
done

printf 'Planned source commit: %s\n' "$(lock_value source.commit)"
printf 'Planned immutable-tag images:\n'
printf '  %s\n' "${image_refs[@]}"
if [[ "${apply}" != "true" ]]; then
  printf 'PREVIEW ONLY: rerun with --apply after source, builder, image, and registry review.\n'
  exit 0
fi

for image in "${image_refs[@]}"; do
  docker image inspect "${image}" >/dev/null 2>&1 && \
    die "local target image already exists; use a new explicit tag: ${image}"
  manifest_error="$(mktemp)"
  if docker manifest inspect "${image}" >/dev/null 2>"${manifest_error}"; then
    rm -f "${manifest_error}"
    die "remote target image already exists; refusing overwrite: ${image}"
  fi
  if ! grep -Eqi 'manifest unknown|MANIFEST_UNKNOWN|no such manifest|manifest[^[:alnum:]].*not found' "${manifest_error}"; then
    manifest_message="$(tr '\n' ' ' <"${manifest_error}" | sed -E 's/[[:space:]]+/ /g')"
    rm -f "${manifest_error}"
    die "unable to confirm that the remote tag is unused for ${image}: ${manifest_message}"
  fi
  rm -f "${manifest_error}"
done

docker build \
  -t "${image_refs[0]}" \
  -f "${context}/source-code/retrieval/video-backend/Dockerfile" \
  "${context}/source-code/retrieval/video-backend"
docker build \
  -t "${image_refs[1]}" \
  -f "${context}/source-code/retrieval/video-frontend/Dockerfile" \
  "${context}/source-code/retrieval/video-frontend"

for index in "${!function_names[@]}"; do
  function_name="${function_names[$index]}"
  image="${image_refs[$((index + 2))]}"
  vastde functions build "${function_name}" \
    --target "${context}/source-code/ingest/${function_name}" \
    --image-tag "${image}" \
    --pull-policy never
done

for image in "${image_refs[@]}"; do
  docker image inspect "${image}" >/dev/null
  docker push "${image}"
  digest="$(docker manifest inspect --verbose "${image}" | \
    python3 -c 'import json,sys; print(json.load(sys.stdin)["Descriptor"]["digest"])')"
  [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid digest returned for ${image}"
  printf 'REGISTRY_DIGEST %s %s\n' "${image}" "${digest}"
done

printf 'PASS: two application and four DataEngine function images were built, pushed, and digest-verified.\n'
printf 'Update the backend and frontend digest inputs before rendering the deployment.\n'
