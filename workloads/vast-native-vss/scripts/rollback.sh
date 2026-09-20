#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

env_file=""
revision=""
apply=false
while (( $# )); do
  case "$1" in
    --env) env_file="${2:-}"; shift 2 ;;
    --revision) revision="${2:-}"; shift 2 ;;
    --apply) apply=true; shift ;;
    *) die "usage: $0 --env <release-inputs.env> --revision <known-good-revision> [--apply]" ;;
  esac
done
[[ -n "${env_file}" && "${revision}" =~ ^[1-9][0-9]*$ ]] || \
  die "usage: $0 --env <release-inputs.env> --revision <known-good-revision> [--apply]"

for command_name in grep helm oc sed tr vastde wc; do require_command "${command_name}"; done
load_env "${env_file}"
verify_context
helm -n "${CVD_NAMESPACE}" history "${CVD_RELEASE}"
printf 'Planned application-only rollback: %s/%s -> revision %s\n' \
  "${CVD_NAMESPACE}" "${CVD_RELEASE}" "${revision}"
printf 'VAST S3 objects, VASTDB rows, views, topic, DataEngine objects, NIM caches, and registry images are retained.\n'

if [[ "${apply}" != "true" ]]; then
  printf 'PREVIEW ONLY: rerun with --apply after selecting and approving the known-good revision.\n'
  exit 0
fi

helm -n "${CVD_NAMESPACE}" rollback "${CVD_RELEASE}" "${revision}" \
  --wait --timeout "${CVD_HELM_TIMEOUT}"
"${SCRIPT_DIR}/verify.sh" --env "${env_file}"
