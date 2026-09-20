#!/usr/bin/env bash

set -euo pipefail
{ set +x; } 2>/dev/null
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE=$(env_path_from_args "$@")
load_inputs "${ENV_FILE}"
for command_name in oc; do require_command "${command_name}"; done
require_inputs
print_context
verify_context

cat <<PLAN

Planned change
  Create namespace/${INSIGHTENGINE_NAMESPACE} if it does not already exist.

No Secret, SCC, cluster role, VAST object, registry association, or workload
will be created by this command.
PLAN

if ! has_apply_flag "$@"; then
  printf '\nDRY RUN: no changes made. Re-run with --apply after review.\n'
  exit 0
fi

if oc get namespace "${INSIGHTENGINE_NAMESPACE}" >/dev/null 2>&1; then
  printf 'PASS: namespace/%s already exists; no change made.\n' "${INSIGHTENGINE_NAMESPACE}"
  exit 0
fi
oc create namespace "${INSIGHTENGINE_NAMESPACE}"
oc get namespace "${INSIGHTENGINE_NAMESPACE}"
printf 'PASS: application namespace is present. Complete VAST namespace and registry reconciliation next.\n'
