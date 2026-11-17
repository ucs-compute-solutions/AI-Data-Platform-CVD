#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  build-derived-images.sh --source PATH --artifacts PATH \
    --repository-prefix REGISTRY/PATH [--version VERSION] [--execute]

Without --execute, print the five source-defined image targets and make no changes. With
--execute, verify the pinned NVIDIA checkout and artifacts, stage temporary
contexts, and build local linux/amd64 images. This script does not log in to a
registry, push images, update Helm values, or change OpenShift.
EOF
}

source_path=""
artifact_path=""
repository_prefix=""
version="3.2.1-aidp.1"
execute=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      source_path=${2:?missing value for --source}
      shift 2
      ;;
    --artifacts)
      artifact_path=${2:?missing value for --artifacts}
      shift 2
      ;;
    --repository-prefix)
      repository_prefix=${2:?missing value for --repository-prefix}
      shift 2
      ;;
    --version)
      if [[ $# -lt 2 ]]; then
        printf 'ERROR: --version requires a value.\n' >&2
        exit 2
      fi
      version=$2
      shift 2
      ;;
    --execute)
      execute=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$source_path" || -z "$artifact_path" || -z "$repository_prefix" ]]; then
  usage >&2
  exit 2
fi

# Docker tags are limited to 128 characters and must begin with an
# alphanumeric character or underscore.  Validate before plan output as well
# as execution so an unsafe target can never look approved by a dry run.
if [[ -z "$version" ||
      "$version" =~ ^[Ll][Aa][Tt][Ee][Ss][Tt]$ ||
      "$version" == *[[:space:]]* ||
      ! "$version" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  printf 'ERROR: --version must be a fixed Docker tag (1-128 characters using A-Z, a-z, 0-9, _, . or -); latest is prohibited.\n' >&2
  exit 2
fi

repository_prefix=${repository_prefix%/}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

image_keys=(
  elasticsearch
  kafka-health-check
  elasticsearch-init
  kibana-dashboard-init
  calibration-import
)

echo "NVIDIA source: $source_path"
echo "Locked artifacts: $artifact_path"
echo "Target platform: linux/amd64"
echo "Local image targets:"
for key in "${image_keys[@]}"; do
  printf '  %s/%s:%s\n' "$repository_prefix" "warehouse-$key" "$version"
done

if [[ "$execute" != true ]]; then
  echo "PLAN ONLY: add --execute to verify inputs and build the local images."
  exit 0
fi

command -v docker >/dev/null || {
  echo "docker is required" >&2
  exit 1
}
command -v python3 >/dev/null || {
  echo "python3 is required" >&2
  exit 1
}

context_root=$(mktemp -d "${TMPDIR:-/tmp}/warehouse-derived-contexts.XXXXXX")
cleanup() {
  rm -rf -- "$context_root"
}
trap cleanup EXIT

python3 "$script_dir/prepare-derived-build-contexts.py" \
  --source "$source_path" \
  --artifacts "$artifact_path" \
  --output "$context_root"

for key in "${image_keys[@]}"; do
  network=default
  case "$key" in
    elasticsearch|kafka-health-check)
      network=none
      ;;
  esac

  image="$repository_prefix/warehouse-$key:$version"
  echo "BUILD: $image (network=$network)"
  docker build \
    --pull=false \
    --platform linux/amd64 \
    --network "$network" \
    --file "$context_root/$key/Dockerfile" \
    --tag "$image" \
    "$context_root/$key"
  docker image inspect --format 'PASS: {{.RepoTags}} local-image-id={{.Id}}' "$image"
done

echo "PASS: five local images built. No image was pushed and OpenShift was not changed."
echo "NEXT: push each image to the reviewed registry and record its registry digest before Helm use."
