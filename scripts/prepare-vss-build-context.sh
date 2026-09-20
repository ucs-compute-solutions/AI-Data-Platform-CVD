#!/usr/bin/env bash

set -euo pipefail

usage() {
  printf 'Usage: %s --source <clean-vendor-checkout> --destination <new-directory> [--expected-commit <sha>] [--patch-dir <directory>] [--lock-root <directory>]\n' "$0"
}

SOURCE=""
DESTINATION=""
EXPECTED_COMMIT=""
PATCH_DIR=""
LOCK_ROOT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE="${2:-}"
      shift 2
      ;;
    --destination)
      DESTINATION="${2:-}"
      shift 2
      ;;
    --expected-commit)
      EXPECTED_COMMIT="${2:-}"
      shift 2
      ;;
    --patch-dir)
      PATCH_DIR="${2:-}"
      shift 2
      ;;
    --lock-root)
      LOCK_ROOT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$SOURCE" || -z "$DESTINATION" ]]; then
  usage >&2
  exit 2
fi

if [[ ! -d "$SOURCE/.git" ]]; then
  printf 'Source is not a Git checkout: %s\n' "$SOURCE" >&2
  exit 1
fi

if [[ -e "$DESTINATION" ]]; then
  printf 'Destination already exists; refusing to overwrite: %s\n' "$DESTINATION" >&2
  exit 1
fi

for command_name in git grep install sha256sum; do
  command -v "$command_name" >/dev/null || {
    printf 'Required command not found: %s\n' "$command_name" >&2
    exit 1
  }
done

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
EXPECTED_COMMIT="${EXPECTED_COMMIT:-8b34c2c919edcec6b7bd51cf9ff09722d3dda879}"
ASSET_ROOT="$REPO_ROOT/workloads/vast-native-vss/source-assets"
PATCH_DIR="${PATCH_DIR:-$ASSET_ROOT/patches}"
LOCK_ROOT="${LOCK_ROOT:-$ASSET_ROOT/locks}"
PATCH_FILE="${PATCH_DIR}/0001-openshift-hardening.patch"
OPTIONAL_PATCH_FILE="${PATCH_DIR}/0002-optional-services.patch"
LLM_COMPAT_PATCH_FILE="${PATCH_DIR}/0003-lightning-thinking-control.patch"

for required_asset in "${PATCH_FILE}" "${OPTIONAL_PATCH_FILE}" "${LLM_COMPAT_PATCH_FILE}" "${LOCK_ROOT}/SHA256SUMS"; do
  [[ -r "${required_asset}" ]] || {
    printf 'Required reviewed source asset is unavailable: %s\n' "${required_asset}" >&2
    exit 1
  }
done

ACTUAL_COMMIT=$(git -C "$SOURCE" rev-parse HEAD)
if [[ "$ACTUAL_COMMIT" != "$EXPECTED_COMMIT" ]]; then
  printf 'Unexpected source commit: %s\nExpected: %s\n' \
    "$ACTUAL_COMMIT" "$EXPECTED_COMMIT" >&2
  exit 1
fi

if [[ -n "$(git -C "$SOURCE" status --porcelain)" ]]; then
  printf 'Source checkout is not clean; refusing to prepare it.\n' >&2
  exit 1
fi

(
  cd "$LOCK_ROOT"
  sha256sum --check SHA256SUMS
)

git clone --quiet --no-hardlinks "$SOURCE" "$DESTINATION"
git -C "$DESTINATION" apply --check "$PATCH_FILE"
git -C "$DESTINATION" apply "$PATCH_FILE"
git -C "$DESTINATION" apply --check "$OPTIONAL_PATCH_FILE"
git -C "$DESTINATION" apply "$OPTIONAL_PATCH_FILE"
git -C "$DESTINATION" apply --check "$LLM_COMPAT_PATCH_FILE"
git -C "$DESTINATION" apply "$LLM_COMPAT_PATCH_FILE"

install -D -m 0644 \
  "$LOCK_ROOT/retrieval/video-frontend/package-lock.json" \
  "$DESTINATION/source-code/retrieval/video-frontend/package-lock.json"
install -D -m 0644 \
  "$LOCK_ROOT/retrieval/video-backend/requirements.lock" \
  "$DESTINATION/source-code/retrieval/video-backend/requirements.lock"
install -D -m 0644 \
  "$LOCK_ROOT/video-streaming/requirements.lock" \
  "$DESTINATION/source-code/video-streaming/requirements.lock"
install -D -m 0644 \
  "$LOCK_ROOT/video-batch-sync/requirements.lock" \
  "$DESTINATION/source-code/video-batch-sync/requirements.lock"

for component in video-segmenter video-reasoner video-embedder vastdb-writer; do
  install -D -m 0644 \
    "$LOCK_ROOT/ingest/$component/requirements.txt" \
    "$DESTINATION/source-code/ingest/$component/requirements.txt"
done

APPROVED_PATHS=(
  "$DESTINATION/source-code/retrieval/video-backend"
  "$DESTINATION/source-code/retrieval/video-frontend"
  "$DESTINATION/source-code/ingest/video-segmenter"
  "$DESTINATION/source-code/ingest/video-reasoner"
  "$DESTINATION/source-code/ingest/video-embedder"
  "$DESTINATION/source-code/ingest/vastdb-writer"
  "$DESTINATION/source-code/video-streaming"
  "$DESTINATION/source-code/video-batch-sync"
)

if grep -RIn --exclude-dir=__pycache__ -E \
  'verify[[:space:]]*=[[:space:]]*False|ssl_verify[[:space:]]*=[[:space:]]*False' \
  "${APPROVED_PATHS[@]}"; then
  printf 'TLS verification bypass found in prepared context.\n' >&2
  exit 1
fi

if grep -RIn -E ':latest([[:space:]]|$)' \
  "$DESTINATION/source-code/retrieval/video-backend/Dockerfile" \
  "$DESTINATION/source-code/retrieval/video-frontend/Dockerfile" \
  "$DESTINATION/source-code/video-streaming/Dockerfile" \
  "$DESTINATION/source-code/video-batch-sync/Dockerfile"; then
  printf 'Latest tag found in application Dockerfiles.\n' >&2
  exit 1
fi

grep -Eq '^ARG PYTHON_IMAGE=.*@sha256:[0-9a-f]{64}$' \
  "$DESTINATION/source-code/retrieval/video-backend/Dockerfile" || {
  printf 'Backend base image is not pinned by digest.\n' >&2
  exit 1
}

grep -Eq '^ARG NODE_IMAGE=.*@sha256:[0-9a-f]{64}$' \
  "$DESTINATION/source-code/retrieval/video-frontend/Dockerfile" || {
  printf 'Frontend Node image is not pinned by digest.\n' >&2
  exit 1
}

grep -Eq '^ARG NGINX_IMAGE=.*@sha256:[0-9a-f]{64}$' \
  "$DESTINATION/source-code/retrieval/video-frontend/Dockerfile" || {
  printf 'Frontend nginx image is not pinned by digest.\n' >&2
  exit 1
}

for dockerfile in \
  "$DESTINATION/source-code/video-streaming/Dockerfile" \
  "$DESTINATION/source-code/video-batch-sync/Dockerfile"; do
  grep -Eq '^ARG PYTHON_IMAGE=.*@sha256:[0-9a-f]{64}$' "$dockerfile" || {
    printf 'Optional-service base image is not pinned by digest: %s\n' "$dockerfile" >&2
    exit 1
  }
done

printf 'Prepared VSS build context: %s\n' "$DESTINATION"
printf 'Source commit: %s\n' "$ACTUAL_COMMIT"
printf 'Static gates: PASS\n'
printf 'No image was built or pushed by this script.\n'
