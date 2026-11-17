#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  publish-derived-images.sh --repository-prefix REGISTRY/PATH \
    --evidence FILE [--version VERSION] [--execute]

Without --execute, print the exact six-image publication set and make no
changes. With --execute, require every local image and preflight every remote
tag. A tag whose immutable digest is already locked is verified and reused;
the script refuses to overwrite, recreate, or drift it. An output without a
locked digest may be published only when the tag is not already visible.
The resulting credential-free JSON evidence file records the verified remote
digests. This script never logs in, creates a Secret, updates Helm values, or
changes OpenShift.
EOF
}

repository_prefix=""
evidence_file=""
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
  local longest_suffix="warehouse-kibana-dashboard-init"

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
    --repository-prefix)
      if [[ $# -lt 2 ]]; then
        printf 'ERROR: --repository-prefix requires a value.\n' >&2
        exit 2
      fi
      repository_prefix=$2
      shift 2
      ;;
    --evidence)
      if [[ $# -lt 2 ]]; then
        printf 'ERROR: --evidence requires a value.\n' >&2
        exit 2
      fi
      evidence_file=$2
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

if [[ -z "$repository_prefix" || -z "$evidence_file" ]]; then
  usage >&2
  exit 2
fi
validate_version "$version" || exit $?
validate_repository_prefix "$repository_prefix" || exit $?
readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly lock_file="${script_dir}/../derived-images.lock.json"

command -v python3 >/dev/null || {
  printf 'ERROR: python3 is required to validate the derived-image lock.\n' >&2
  exit 1
}

locked_suffixes=$(python3 - "$lock_file" <<'PY'
import json
import sys


with open(sys.argv[1], encoding="utf-8") as stream:
    lock = json.load(stream)

expected = (
    ("elasticsearch", "warehouse-elasticsearch", "Dockerfile.elasticsearch"),
    (
        "kafka-health-check",
        "warehouse-kafka-health-check",
        "Dockerfile.kafka-health-check",
    ),
    (
        "elasticsearch-init",
        "warehouse-elasticsearch-init",
        "Dockerfile.elasticsearch-init",
    ),
    (
        "kibana-dashboard-init",
        "warehouse-kibana-dashboard-init",
        "Dockerfile.kibana-dashboard-init",
    ),
    (
        "calibration-import",
        "warehouse-calibration-import",
        "Dockerfile.calibration-import",
    ),
    (
        "logstash-protobuf",
        "warehouse-logstash-protobuf",
        "Dockerfile.logstash-protobuf",
    ),
)
source_outputs = lock.get("outputs")
if not isinstance(source_outputs, list) or len(source_outputs) != 5:
    raise SystemExit("derived-image lock must contain five source-defined outputs")
logstash_output = lock.get("logstashPlugin", {}).get("derivedOutput")
if not isinstance(logstash_output, dict):
    raise SystemExit("derived-image lock is missing the Logstash derived output")
records = source_outputs + [logstash_output]

for record, (key, suffix, dockerfile) in zip(records, expected):
    required = {
        "key": key,
        "repositorySuffix": suffix,
        "dockerfile": dockerfile,
    }
    for field, expected_value in required.items():
        if record.get(field) != expected_value:
            raise SystemExit(
                f"invalid derived output {key} {field}: {record.get(field)!r}"
            )
    digest = record.get("outputDigest")
    if digest is not None and not (
        isinstance(digest, str)
        and len(digest) == 71
        and digest.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in digest[7:])
    ):
        raise SystemExit(f"invalid derived output digest for {key}")
    print(f"{suffix}\t{digest or ''}")
PY
)
repository_suffixes=()
locked_output_digests=()
while IFS=$'\t' read -r suffix digest; do
  if [[ -n "$suffix" ]]; then
    repository_suffixes+=("$suffix")
    locked_output_digests+=("$digest")
  fi
done <<<"$locked_suffixes"
if [[ ${#repository_suffixes[@]} -ne 6 ]]; then
  printf 'ERROR: the derived-image lock did not resolve exactly six publication targets.\n' >&2
  exit 1
fi
readonly -a repository_suffixes
readonly -a locked_output_digests

printf 'Target platform: linux/amd64\n'
printf 'Publication set:\n'
for suffix in "${repository_suffixes[@]}"; do
  printf '  %s/%s:%s\n' "$repository_prefix" "$suffix" "$version"
done
printf 'Digest evidence: %s\n' "$evidence_file"

if [[ "$execute" != true ]]; then
  printf 'PLAN ONLY: authenticate separately, review the targets, and add --execute to push.\n'
  exit 0
fi

command -v docker >/dev/null || {
  printf 'ERROR: docker is required.\n' >&2
  exit 1
}
command -v python3 >/dev/null || {
  printf 'ERROR: python3 is required.\n' >&2
  exit 1
}
if [[ -e "$evidence_file" ]]; then
  printf 'ERROR: refusing to overwrite evidence file: %s\n' "$evidence_file" >&2
  exit 1
fi

for suffix in "${repository_suffixes[@]}"; do
  image="${repository_prefix}/${suffix}:${version}"
  docker image inspect "$image" >/dev/null 2>&1 || {
    printf 'ERROR: required local image is missing: %s\n' "$image" >&2
    exit 1
  }
done

temporary_evidence=$(mktemp "${TMPDIR:-/tmp}/warehouse-publish-evidence.XXXXXX")
temporary_manifest_error=$(mktemp "${TMPDIR:-/tmp}/warehouse-manifest-error.XXXXXX")
cleanup() {
  rm -f -- "$temporary_evidence" "$temporary_manifest_error"
}
trap cleanup EXIT HUP INT TERM
: >"$temporary_evidence"

resolve_remote_digest() {
  local image=$1 descriptor
  if ! descriptor=$(docker manifest inspect --verbose "$image" 2>"$temporary_manifest_error"); then
    return 1
  fi
  python3 - "$image" "$descriptor" <<'PY'
import json
import sys

image = sys.argv[1]
descriptor = json.loads(sys.argv[2])
if isinstance(descriptor, list):
    matches = [
        item
        for item in descriptor
        if item.get("Descriptor", {}).get("platform", {}).get("os") == "linux"
        and item.get("Descriptor", {}).get("platform", {}).get("architecture")
        == "amd64"
    ]
    if len(matches) != 1:
        raise SystemExit(
            f"expected one linux/amd64 manifest descriptor for {image}, got {len(matches)}"
        )
    descriptor = matches[0]
digest = descriptor.get("Descriptor", {}).get("digest", "")
if not digest.startswith("sha256:") or len(digest) != 71:
    raise SystemExit(f"remote manifest digest unavailable for {image}")
print(digest)
PY
}

# Preflight all six tags before the first push. This prevents a later mismatch
# from leaving a partially published set and makes an already-published set a
# read-only verification operation.
remote_digests=()
publish_required=()
for index in "${!repository_suffixes[@]}"; do
  suffix=${repository_suffixes[$index]}
  image="${repository_prefix}/${suffix}:${version}"
  locked_digest=${locked_output_digests[$index]}
  if remote_digest=$(resolve_remote_digest "$image"); then
    if [[ -z "$locked_digest" ]]; then
      printf 'ERROR: remote tag already exists but the lock has no output digest; refusing to overwrite: %s\n' "$image" >&2
      exit 1
    fi
    if [[ "$remote_digest" != "$locked_digest" ]]; then
      printf 'ERROR: remote tag digest drift for %s (locked %s, observed %s); refusing to overwrite.\n' \
        "$image" "$locked_digest" "$remote_digest" >&2
      exit 1
    fi
    remote_digests+=("$remote_digest")
    publish_required+=(false)
  else
    if [[ -n "$locked_digest" ]]; then
      printf 'ERROR: locked remote tag is unavailable; refusing to recreate or push without a new reviewed version: %s\n' "$image" >&2
      exit 1
    fi
    remote_digests+=("")
    publish_required+=(true)
  fi
done

for index in "${!repository_suffixes[@]}"; do
  suffix=${repository_suffixes[$index]}
  image="${repository_prefix}/${suffix}:${version}"
  if [[ "${publish_required[$index]}" == true ]]; then
    printf 'PUSH NEW TAG: %s\n' "$image"
    docker push "$image"
    remote_digest=$(resolve_remote_digest "$image") || {
      printf 'ERROR: pushed tag could not be resolved: %s\n' "$image" >&2
      exit 1
    }
  else
    remote_digest=${remote_digests[$index]}
    printf 'REUSE LOCKED TAG: %s@%s\n' "$image" "$remote_digest"
  fi
  python3 - "$image" "$remote_digest" >>"$temporary_evidence" <<'PY'
import json
import sys

image, digest = sys.argv[1:]
print(json.dumps({"image": image, "digest": digest}, sort_keys=True))
PY
done

mkdir -p -- "$(dirname -- "$evidence_file")"
python3 - "$temporary_evidence" "$evidence_file" "$version" <<'PY'
import datetime
import json
import os
import sys

source, destination, version = sys.argv[1:]
with open(source, encoding="utf-8") as stream:
    images = [json.loads(line) for line in stream if line.strip()]
payload = {
    "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "targetPlatform": "linux/amd64",
    "version": version,
    "images": images,
}
temporary = destination + f".partial-{os.getpid()}"
with open(temporary, "x", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")
os.replace(temporary, destination)
PY

printf 'PASS: six remote tags verified and immutable digest evidence written to %s\n' "$evidence_file"
printf 'No Secret, Helm value, or OpenShift resource was changed.\n'
