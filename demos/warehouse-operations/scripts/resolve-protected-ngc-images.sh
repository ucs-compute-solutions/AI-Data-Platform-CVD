#!/usr/bin/env bash

set -euo pipefail
umask 077
ulimit -c 0

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../../.." && pwd)"
readonly SOURCE_LOCK="${REPO_ROOT}/demos/warehouse-operations/openshift/nvidia-warehouse/source-lock.yaml"
readonly RESOLVER="${SCRIPT_DIR}/resolve-image-digests.py"
readonly NGC_USERNAME='$oauthtoken'

readonly -a PROTECTED_IMAGES=(
  "nvcr.io/nvidia/vss-core/vss-alert-verification:3.2.0"
  "nvcr.io/nvidia/vss-core/vss-configurator:3.2.1"
  "nvcr.io/nvidia/vss-core/vss-rt-vlm:3.2.1"
  "nvcr.io/nim/nvidia/nvidia-nemotron-nano-9b-v2:1"
)

for command_name in docker oc python3; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    printf 'ERROR: required command is unavailable: %s\n' "${command_name}" >&2
    exit 1
  }
done

[[ -f "${SOURCE_LOCK}" ]] || {
  printf 'ERROR: source lock is unavailable: %s\n' "${SOURCE_LOCK}" >&2
  exit 1
}
[[ -x "${RESOLVER}" || -f "${RESOLVER}" ]] || {
  printf 'ERROR: digest resolver is unavailable: %s\n' "${RESOLVER}" >&2
  exit 1
}

warehouse_auth_dir="$(mktemp -d /tmp/warehouse-ngc-auth.XXXXXX)"
readonly warehouse_auth_dir
readonly result_file="/tmp/warehouse-ngc-digests-$(date -u +%Y%m%dT%H%M%SZ).json"

cleanup() {
  unset NGC_API_KEY DOCKER_CONFIG
  case "${warehouse_auth_dir}" in
    /tmp/warehouse-ngc-auth.*)
      if [[ -f "${warehouse_auth_dir}/config.json" ]]; then
        : >"${warehouse_auth_dir}/config.json"
        rm -f -- "${warehouse_auth_dir}/config.json"
      fi
      rm -rf -- "${warehouse_auth_dir}"
      ;;
    *)
      printf 'WARNING: refusing to clean unexpected temporary path.\n' >&2
      ;;
  esac
}
trap cleanup EXIT HUP INT TERM

read -rsp 'Paste NGC API key: ' NGC_API_KEY
printf '\n'
[[ -n "${NGC_API_KEY}" ]] || {
  printf 'ERROR: an NGC API key is required.\n' >&2
  exit 1
}

export DOCKER_CONFIG="${warehouse_auth_dir}"
if ! printf '%s' "${NGC_API_KEY}" | docker login nvcr.io \
  --username "${NGC_USERNAME}" --password-stdin >/dev/null 2>&1; then
  printf 'FAIL: NGC registry authentication was rejected.\n' >&2
  exit 1
fi
unset NGC_API_KEY
printf 'PASS: temporary NGC registry authentication succeeded.\n'

resolver_args=(
  "${RESOLVER}"
  --source-lock "${SOURCE_LOCK}"
  --registry-config "${DOCKER_CONFIG}/config.json"
  --output "${result_file}"
)
for image in "${PROTECTED_IMAGES[@]}"; do
  resolver_args+=(--image "${image}")
done

python3 "${resolver_args[@]}"
printf 'PASS: protected-image digest evidence written to %s\n' "${result_file}"
printf 'The temporary NGC login will now be removed.\n'
