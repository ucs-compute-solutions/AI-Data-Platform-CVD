#!/usr/bin/env bash

set -euo pipefail
{ set +x; } 2>/dev/null
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE=$(env_path_from_args "$@")
load_inputs "${ENV_FILE}"
for command_name in python3 docker; do require_command "${command_name}"; done
require_inputs
verify_delivery

LOADER=$(bundle_path tools/load-and-push-images.sh)
[[ -x "${LOADER}" ]] || die "delivery image loader is not executable: ${LOADER}"

cat <<PLAN

Planned action
  Run the VAST-supplied image loader from the verified delivery.
  Registry target: ${REGISTRY_TARGET}

The container client must already be authenticated. No registry password is
accepted by this script or placed in its command line.
PLAN

if ! has_apply_flag "$@"; then
  printf '\nDRY RUN: no images loaded or pushed. Re-run with --apply after review.\n'
  exit 0
fi

(
  cd "${INSIGHTENGINE_BUNDLE_DIR}"
  tools/load-and-push-images.sh "${REGISTRY_TARGET}"
)
printf 'PASS: the VAST image loader completed. Apply its exact image output to the reviewed values before deployment.\n'
