#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE=$(env_path_from_args "$@")
load_inputs "${ENV_FILE}"

for command_name in oc sha256sum; do
  require_command "${command_name}"
done
for value_name in \
  EXPECTED_OCP_API DATAENGINE_BUNDLE_DIR DATAENGINE_PACKAGE \
  DATAENGINE_PACKAGE_SHA256 ZARF_BINARY_SHA256 ZARF_ARCHITECTURE \
  DATAENGINE_NAMESPACE KNATIVE_SERVING_NAMESPACE \
  KNATIVE_EVENTING_NAMESPACE ZARF_MUTATION_NAMESPACES; do
  require_value "${value_name}"
done

print_context
verify_context
[[ -x "${DATAENGINE_BUNDLE_DIR}/zarf" ]] || die "release-supplied zarf is not executable"
PACKAGE_PATH="${DATAENGINE_BUNDLE_DIR}/${DATAENGINE_PACKAGE}"
[[ -f "${PACKAGE_PATH}" ]] || die "DataEngine package not found: ${PACKAGE_PATH}"
case "${ZARF_ARCHITECTURE}" in
  amd64|arm64) ;;
  *) die "unsupported ZARF_ARCHITECTURE: ${ZARF_ARCHITECTURE}" ;;
esac

# Artifact integrity is verified before any namespace is created or labeled.
verify_sha256 "${ZARF_BINARY_SHA256}" "${DATAENGINE_BUNDLE_DIR}/zarf" "Zarf binary"
verify_sha256 "${DATAENGINE_PACKAGE_SHA256}" "${PACKAGE_PATH}" "DataEngine package"
"${DATAENGINE_BUNDLE_DIR}/zarf" package deploy --help | grep -Fq -- '--architecture' || \
  die "release-supplied Zarf package deploy does not support explicit --architecture"
mutation_namespaces >/dev/null
for namespace_name in \
  "${DATAENGINE_NAMESPACE}" \
  "${KNATIVE_SERVING_NAMESPACE}" \
  "${KNATIVE_EVENTING_NAMESPACE}"; do
  require_namespace_in_mutation_scope "${namespace_name}"
done
assert_existing_mutation_scope_is_approved
oc get namespace zarf >/dev/null 2>&1 || \
  die "namespace/zarf is absent; initialize the reviewed Zarf release before deploying DataEngine"

cat <<PLAN

Planned changes
  1. Create only these approved mutation namespaces: ${ZARF_MUTATION_NAMESPACES}.
  2. Apply zarf.dev/agent=mutate and zarf.dev/vast=mutate to that allowlist.
  3. Deploy checksum-verified ${DATAENGINE_PACKAGE} for ${ZARF_ARCHITECTURE}.
PLAN

if ! has_apply_flag "$@"; then
  printf '\nDRY RUN: no changes made. Re-run with --apply after review.\n'
  exit 0
fi

while IFS= read -r namespace_name; do
  oc create namespace "${namespace_name}" --dry-run=client -o yaml | oc apply -f -
  oc label namespace "${namespace_name}" \
    zarf.dev/agent=mutate \
    zarf.dev/vast=mutate \
    --overwrite
done < <(mutation_namespaces)

TMP_DIR=$(mktemp -d)
cleanup() {
  unset SSL_CERT_FILE
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT
materialize_zot_ca "${TMP_DIR}/zot-ca-bundle.pem"
export SSL_CERT_FILE="${TMP_DIR}/zot-ca-bundle.pem"

(
  cd "${DATAENGINE_BUNDLE_DIR}"
  ./zarf package deploy "${DATAENGINE_PACKAGE}" \
    --architecture "${ZARF_ARCHITECTURE}" \
    --confirm
)

oc -n "${DATAENGINE_NAMESPACE}" get pods -o wide
oc -n "${KNATIVE_SERVING_NAMESPACE}" get pods -o wide
oc -n "${KNATIVE_EVENTING_NAMESPACE}" get pods -o wide
assert_existing_mutation_scope_is_approved
printf 'PASS: DataEngine package deployment command completed. Run verify-dataengine.sh before tenant enablement.\n'
