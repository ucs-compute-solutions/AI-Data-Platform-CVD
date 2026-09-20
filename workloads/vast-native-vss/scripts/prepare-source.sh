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

for command_name in git python3 sha256sum; do require_command "${command_name}"; done
load_env "${env_file}"
destination="${CVD_BUILD_DIR}/prepared-vast-vss-source"
expected_commit="$(lock_value source.commit)"

umask 077
mkdir -p "${CVD_BUILD_DIR}"
[[ ! -e "${destination}" ]] || die "prepared source already exists; review or move it before a new run: ${destination}"
"${REPOSITORY_ROOT}/scripts/prepare-vss-build-context.sh" \
  --source "${CVD_SOURCE_DIR}" \
  --destination "${destination}" \
  --expected-commit "${expected_commit}" \
  --patch-dir "${COMPANION_DIR}/source-assets/patches" \
  --lock-root "${COMPANION_DIR}/source-assets/locks"

printf 'PASS: reviewed OpenShift patches and locked dependencies were applied to a disposable build context.\n'
printf 'Prepared source: %s\n' "${destination}"
printf 'The clean pinned checkout was not changed.\n'
