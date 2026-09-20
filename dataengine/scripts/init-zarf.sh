#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE=$(env_path_from_args "$@")
load_inputs "${ENV_FILE}"

for command_name in oc curl sha256sum grep jq; do
  require_command "${command_name}"
done
for value_name in \
  EXPECTED_OCP_API ZOT_NAMESPACE ZOT_HOST ZOT_USER DATAENGINE_BUNDLE_DIR \
  ZARF_INIT_PACKAGE ZARF_INIT_PACKAGE_SHA256 ZARF_BINARY_SHA256 \
  ZARF_ARCHITECTURE ZARF_MUTATION_NAMESPACES; do
  require_value "${value_name}"
done

print_context
verify_context
[[ -x "${DATAENGINE_BUNDLE_DIR}/zarf" ]] || die "release-supplied zarf is not executable"
[[ -f "${DATAENGINE_BUNDLE_DIR}/${ZARF_INIT_PACKAGE}" ]] || die "Zarf init package not found"
case "${ZARF_ARCHITECTURE}" in
  amd64|arm64) ;;
  *) die "unsupported ZARF_ARCHITECTURE: ${ZARF_ARCHITECTURE}" ;;
esac
verify_sha256 "${ZARF_BINARY_SHA256}" "${DATAENGINE_BUNDLE_DIR}/zarf" "Zarf binary"
verify_sha256 "${ZARF_INIT_PACKAGE_SHA256}" \
  "${DATAENGINE_BUNDLE_DIR}/${ZARF_INIT_PACKAGE}" "Zarf init package"
ZARF_INIT_HELP=$("${DATAENGINE_BUNDLE_DIR}/zarf" init --help)
for required_flag in \
  --architecture --agent-mutation-policy --registry-url \
  --registry-push-username --registry-pull-username; do
  grep -Fq -- "${required_flag}" <<< "${ZARF_INIT_HELP}" || \
    die "release-supplied Zarf init does not support required option: ${required_flag}"
done
grep -Eq 'PACKAGE_SOURCE|package source' <<< "${ZARF_INIT_HELP}" || \
  die "release-supplied Zarf init does not document an explicit init package source"
mutation_namespaces >/dev/null
assert_existing_mutation_scope_is_approved

cat <<PLAN

Planned change
  Init package: ${ZARF_INIT_PACKAGE}
  Architecture: ${ZARF_ARCHITECTURE}
  External registry: ${ZOT_HOST}
  Agent mutation policy: labeled (deny by default)
  Approved mutation namespaces: ${ZARF_MUTATION_NAMESPACES}
PLAN
if oc get namespace zarf >/dev/null 2>&1; then
  die "namespace/zarf already exists; use the release-specific Zarf upgrade procedure instead of reinitializing"
fi

if ! has_apply_flag "$@"; then
  printf 'DRY RUN: no changes made. Re-run with --apply after review.\n'
  exit 0
fi

read -r -s -p "Zot password for ${ZOT_USER}: " ZOT_PASSWORD
printf '\n'
[[ -n "${ZOT_PASSWORD}" ]] || die "Zot password must not be empty"
TMP_DIR=$(mktemp -d)
cleanup() {
  unset ZOT_PASSWORD SSL_CERT_FILE ZARF_INIT_REGISTRY_PUSH_PASSWORD ZARF_INIT_REGISTRY_PULL_PASSWORD
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT
materialize_zot_ca "${TMP_DIR}/zot-ca-bundle.pem"
export SSL_CERT_FILE="${TMP_DIR}/zot-ca-bundle.pem"
write_curl_basic_auth_config "${TMP_DIR}/curl.conf" "${ZOT_USER}" "${ZOT_PASSWORD}"
curl --fail --silent --show-error \
  --config "${TMP_DIR}/curl.conf" \
  --cacert "${TMP_DIR}/zot-ca-bundle.pem" \
  "https://${ZOT_HOST}/v2/" >/dev/null || \
  die "Zot authentication failed; Zarf initialization was not started"

# Zarf maps these environment variables to the corresponding init options.
# Keeping the password out of command arguments avoids disclosure through ps.
export ZARF_INIT_REGISTRY_PUSH_PASSWORD="${ZOT_PASSWORD}"
export ZARF_INIT_REGISTRY_PULL_PASSWORD="${ZOT_PASSWORD}"
unset ZOT_PASSWORD

(
  cd "${DATAENGINE_BUNDLE_DIR}"
  ./zarf init "${DATAENGINE_BUNDLE_DIR}/${ZARF_INIT_PACKAGE}" \
    --architecture "${ZARF_ARCHITECTURE}" \
    --agent-mutation-policy labeled \
    --registry-url "${ZOT_HOST}" \
    --registry-push-username "${ZOT_USER}" \
    --registry-pull-username "${ZOT_USER}" \
    --components= \
    --confirm
)

oc get namespaces zarf "${ZOT_NAMESPACE}"
oc -n zarf get pods -o json | jq -e '
  (.items | length) > 0 and
  all(.items[];
    (.status.phase == "Succeeded") or
    (.status.phase == "Running" and
     any(.status.conditions[]?; .type == "Ready" and .status == "True")))
' >/dev/null || die "Zarf initialization completed but one or more zarf pods are not healthy"
[[ "$(oc get namespace zarf -o jsonpath='{.metadata.labels.zarf\.dev/agent}')" == "ignore" ]] || \
  die "namespace/zarf is not labeled zarf.dev/agent=ignore after initialization"
printf 'PASS: Zarf initialized with the labeled mutation policy and explicit release package.\n'
