#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  build-logstash-derived-image.sh --artifacts PATH \
    --repository-prefix REGISTRY/PATH [--version VERSION] [--execute]

Without --execute, print the locked Logstash build target and make no changes.
With --execute, require the reviewed offline plug-in pack checksum in the lock,
verify the pack and Dockerfile, and build one local linux/amd64 image with the
build network disabled. This script does not authenticate, push, or change
OpenShift.
EOF
}

artifact_path=""
repository_prefix=""
version="3.2.1-aidp.1"
execute=false

validate_version() {
  local candidate=$1
  if [[ -z "$candidate" ||
        "$candidate" =~ ^[Ll][Aa][Tt][Ee][Ss][Tt]$ ||
        "$candidate" == *[[:space:]]* ||
        ! "$candidate" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
    printf 'ERROR: --version must be a fixed Docker tag (1-128 characters using A-Z, a-z, 0-9, _, . or -); latest is prohibited.\n' >&2
    return 2
  fi
}

validate_repository_prefix() {
  local candidate=$1
  local registry host port path_component label
  local -a parts labels
  local longest_suffix="warehouse-logstash-protobuf"

  if [[ -z "$candidate" ||
        "$candidate" == */ ||
        "$candidate" == *[[:space:]]* ||
        "$candidate" == *://* ||
        "$candidate" == *//* ||
        "$candidate" == *@* ||
        "$candidate" != */* ||
        "$candidate" == *[A-Z]* ||
        $(( ${#candidate} + 1 + ${#longest_suffix} )) -gt 255 ]]; then
    printf 'ERROR: --repository-prefix must be a lowercase REGISTRY/PATH without a scheme, tag, digest, whitespace, or trailing slash; resulting repository names must not exceed 255 characters.\n' >&2
    return 2
  fi

  IFS=/ read -r -a parts <<<"$candidate"
  registry=${parts[0]}
  if [[ "$registry" =~ ^(.+):([0-9]+)$ ]]; then
    host=${BASH_REMATCH[1]}
    port=${BASH_REMATCH[2]}
    if [[ ${#port} -gt 5 ]] || (( 10#$port < 1 || 10#$port > 65535 )); then
      printf 'ERROR: --repository-prefix contains an invalid registry port.\n' >&2
      return 2
    fi
  else
    host=$registry
  fi

  IFS=. read -r -a labels <<<"$host"
  for label in "${labels[@]}"; do
    if [[ ! "$label" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
      printf 'ERROR: --repository-prefix contains an invalid registry hostname.\n' >&2
      return 2
    fi
  done

  for path_component in "${parts[@]:1}"; do
    if [[ ! "$path_component" =~ ^[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*$ ]]; then
      printf 'ERROR: --repository-prefix contains an invalid repository path component.\n' >&2
      return 2
    fi
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --artifacts)
      artifact_path=${2:?missing value for --artifacts}
      shift 2
      ;;
    --repository-prefix)
      if [[ $# -lt 2 ]]; then
        printf 'ERROR: --repository-prefix requires a value.\n' >&2
        exit 2
      fi
      repository_prefix=$2
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
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$artifact_path" || -z "$repository_prefix" ]]; then
  usage >&2
  exit 2
fi
validate_version "$version" || exit $?
validate_repository_prefix "$repository_prefix" || exit $?
readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly build_dir="${script_dir}/.."
readonly lock_file="${build_dir}/derived-images.lock.json"

command -v python3 >/dev/null || {
  printf 'ERROR: python3 is required to validate the derived-image lock.\n' >&2
  exit 1
}

read_lock_value() {
  python3 - "$lock_file" "$1" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    lock = json.load(stream)
offline = lock["logstashPlugin"]["offlinePack"]
output = lock["logstashPlugin"]["derivedOutput"]
expected = {
    "key": "logstash-protobuf",
    "repositorySuffix": "warehouse-logstash-protobuf",
    "dockerfile": "Dockerfile.logstash-protobuf",
    "networkDuringBuild": "none",
}
for field, expected_value in expected.items():
    if output.get(field) != expected_value:
        raise SystemExit(
            f"invalid Logstash derived output {field}: {output.get(field)!r}"
        )
digest = output.get("outputDigest")
if digest is not None and not (
    isinstance(digest, str)
    and len(digest) == 71
    and digest.startswith("sha256:")
    and all(character in "0123456789abcdef" for character in digest[7:])
):
    raise SystemExit("invalid Logstash derived output digest")
values = {
    "filename": offline["filename"],
    "sha256": offline["sha256"] or "",
    "dockerfileSha256": lock["buildFiles"]["dockerfiles/Dockerfile.logstash-protobuf"],
    "repositorySuffix": output["repositorySuffix"],
    "dockerfile": output["dockerfile"],
    "networkDuringBuild": output["networkDuringBuild"],
}
print(values[sys.argv[2]])
PY
}

readonly pack_name=$(read_lock_value filename)
readonly pack_sha256=$(read_lock_value sha256)
readonly dockerfile_sha256=$(read_lock_value dockerfileSha256)
readonly repository_suffix=$(read_lock_value repositorySuffix)
readonly dockerfile_name=$(read_lock_value dockerfile)
readonly network_during_build=$(read_lock_value networkDuringBuild)
readonly dockerfile="${build_dir}/dockerfiles/${dockerfile_name}"
readonly image="${repository_prefix}/${repository_suffix}:${version}"

if [[ "$network_during_build" != "none" ]]; then
  printf 'ERROR: Logstash derived output must keep networkDuringBuild=none.\n' >&2
  exit 1
fi

printf 'Offline pack: %s/%s\n' "$artifact_path" "$pack_name"
printf 'Target platform: linux/amd64\n'
printf 'Local image target: %s\n' "$image"

if [[ "$execute" != true ]]; then
  if [[ -z "$pack_sha256" ]]; then
    printf 'BLOCKED: generate, review, and checksum-lock the offline plug-in pack first.\n'
  fi
  printf 'PLAN ONLY: add --execute only after the lock contains the reviewed pack checksum.\n'
  exit 0
fi

command -v docker >/dev/null || {
  printf 'ERROR: docker is required.\n' >&2
  exit 1
}
command -v sha256sum >/dev/null || {
  printf 'ERROR: sha256sum is required.\n' >&2
  exit 1
}

if [[ -z "$pack_sha256" ]]; then
  printf 'ERROR: offline pack SHA-256 is not recorded in %s.\n' "$lock_file" >&2
  exit 1
fi
readonly pack_path="${artifact_path}/${pack_name}"
[[ -f "$pack_path" ]] || {
  printf 'ERROR: offline pack is missing: %s\n' "$pack_path" >&2
  exit 1
}
printf '%s  %s\n' "$pack_sha256" "$pack_path" | sha256sum --check --status || {
  printf 'ERROR: offline pack checksum does not match the reviewed lock.\n' >&2
  exit 1
}
printf '%s  %s\n' "$dockerfile_sha256" "$dockerfile" | sha256sum --check --status || {
  printf 'ERROR: Logstash Dockerfile checksum does not match the reviewed lock.\n' >&2
  exit 1
}

context_dir=$(mktemp -d "${TMPDIR:-/tmp}/warehouse-logstash-context.XXXXXX")
cleanup() {
  case "$context_dir" in
    "${TMPDIR:-/tmp}"/warehouse-logstash-context.*)
      rm -rf -- "$context_dir"
      ;;
    *)
      printf 'WARNING: refusing to remove unexpected temporary path: %s\n' "$context_dir" >&2
      ;;
  esac
}
trap cleanup EXIT HUP INT TERM

mkdir -p -- "$context_dir/third-party"
cp -- "$dockerfile" "$context_dir/Dockerfile"
cp -- "$pack_path" "$context_dir/third-party/$pack_name"

docker build \
  --pull=false \
  --platform linux/amd64 \
  --network "$network_during_build" \
  --file "$context_dir/Dockerfile" \
  --tag "$image" \
  "$context_dir"
docker image inspect --format 'PASS: {{.RepoTags}} local-image-id={{.Id}}' "$image"
printf 'PASS: local Logstash image built. No image was pushed and OpenShift was not changed.\n'
