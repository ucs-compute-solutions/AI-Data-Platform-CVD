#!/usr/bin/env bash

set -euo pipefail
{ set +x; } 2>/dev/null
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

for command_name in oc helm python3; do
  require_command "${command_name}"
done
load_env "${env_file}"
verify_context

history_json="$(helm -n "${CVD_INSIGHTENGINE_NAMESPACE}" history \
  "${CVD_BACKEND_RELEASE}" -o json)"
printf '%s\n' "${history_json}" | python3 -c '
import json
import sys
revision = int(sys.argv[1])
history = json.load(sys.stdin)
if not any(int(item.get("revision", 0)) == revision for item in history):
    raise SystemExit(f"ERROR: Helm revision {revision} does not exist")
' "${revision}"

helm -n "${CVD_INSIGHTENGINE_NAMESPACE}" history "${CVD_BACKEND_RELEASE}"
printf '\nPlanned application rollback\n'
printf '  Namespace: %s\n' "${CVD_INSIGHTENGINE_NAMESPACE}"
printf '  Helm release: %s\n' "${CVD_BACKEND_RELEASE}"
printf '  Target revision: %s\n' "${revision}"
printf '  Retained: PVCs, collections, documents, conversations, VAST resources, NIM caches, and model services\n'

if [[ "${apply}" != "true" ]]; then
  printf '\nPREVIEW ONLY: no changes made. Re-run with --apply after the known-good revision is reviewed and approved.\n'
  exit 0
fi

helm -n "${CVD_INSIGHTENGINE_NAMESPACE}" rollback \
  "${CVD_BACKEND_RELEASE}" "${revision}" \
  --wait --timeout "${CVD_HELM_TIMEOUT}"
oc -n "${CVD_INSIGHTENGINE_NAMESPACE}" rollout status \
  "deployment/${CVD_BACKEND_DEPLOYMENT}" \
  --timeout "${CVD_HELM_TIMEOUT}"

status="$(helm -n "${CVD_INSIGHTENGINE_NAMESPACE}" status \
  "${CVD_BACKEND_RELEASE}" -o json | \
  python3 -c 'import json,sys; print(json.load(sys.stdin).get("info", {}).get("status", ""))')"
[[ "${status}" == "deployed" ]] || die "Helm release is not deployed after rollback"
printf 'PASS: application rollback completed; retained data and model resources were not deleted.\n'
