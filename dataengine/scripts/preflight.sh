#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE=$(env_path_from_args "$@")
load_inputs "${ENV_FILE}"

for command_name in oc helm envsubst jq curl htpasswd sha256sum base64; do
  require_command "${command_name}"
done

for value_name in \
  EXPECTED_OCP_API ZOT_NAMESPACE ZOT_RELEASE ZOT_CHART_VERSION ZOT_HOST \
  ZOT_USER ZOT_STORAGE_CLASS ZOT_STORAGE_SIZE INGRESS_CLASS \
  REGISTRY_TRUST_CONFIGMAP VAST_PERMISSIONS_NAMESPACE \
  VAST_PERMISSIONS_RELEASE VAST_PERMISSIONS_CHART \
  DATAENGINE_BUNDLE_DIR ZARF_ARCHITECTURE ZARF_BINARY_SHA256 \
  DATAENGINE_PACKAGE DATAENGINE_PACKAGE_SHA256 DATAENGINE_NAMESPACE \
  KNATIVE_SERVING_NAMESPACE KNATIVE_EVENTING_NAMESPACE \
  ZARF_MUTATION_NAMESPACES; do
  require_value "${value_name}"
done

print_context
verify_context

printf '\nOpenShift readiness\n'
oc get nodes
oc get clusteroperators
oc get machineconfigpools
oc get nodes -o json | jq -e '
  (.items | length) > 0 and
  all(.items[]; any(.status.conditions[]?; .type == "Ready" and .status == "True"))
' >/dev/null || die "one or more OpenShift nodes are not Ready"
oc get clusteroperators -o json | jq -e '
  all(.items[];
    any(.status.conditions[]?; .type == "Available" and .status == "True") and
    any(.status.conditions[]?; .type == "Degraded" and .status == "False"))
' >/dev/null || die "one or more ClusterOperators are unavailable or degraded"
oc get machineconfigpools -o json | jq -e '
  all(.items[];
    (.spec.paused == true) or
    (any(.status.conditions[]?; .type == "Updated" and .status == "True") and
     any(.status.conditions[]?; .type == "Degraded" and .status == "False")))
' >/dev/null || die "one or more unpaused MachineConfigPools are not Updated or are degraded"

for permission in \
  'create namespaces' \
  'patch image.config.openshift.io/cluster' \
  'create clusterrolebindings.rbac.authorization.k8s.io'; do
  read -r verb resource <<< "${permission}"
  [[ "$(oc auth can-i "${verb}" "${resource}" --all-namespaces)" == "yes" ]] || \
    die "current identity cannot ${verb} ${resource}"
done

printf '\nStorage and ingress\n'
oc get storageclass "${ZOT_STORAGE_CLASS}"
oc get ingresses.config/cluster
oc get ingressclass "${INGRESS_CLASS}"

printf '\nRelease inputs\n'
[[ -d "${VAST_PERMISSIONS_CHART}" || -f "${VAST_PERMISSIONS_CHART}" ]] || \
  die "permissions chart not found: ${VAST_PERMISSIONS_CHART}"
[[ -f "${VAST_PERMISSIONS_VALUES}" ]] || die "permissions values file not found: ${VAST_PERMISSIONS_VALUES}"
[[ -d "${DATAENGINE_BUNDLE_DIR}" ]] || die "bundle directory not found: ${DATAENGINE_BUNDLE_DIR}"
[[ -x "${DATAENGINE_BUNDLE_DIR}/zarf" ]] || die "release-supplied zarf is not executable: ${DATAENGINE_BUNDLE_DIR}/zarf"
[[ -f "${DATAENGINE_BUNDLE_DIR}/${DATAENGINE_PACKAGE}" ]] || die "DataEngine package not found: ${DATAENGINE_PACKAGE}"
case "${ZARF_ARCHITECTURE}" in
  amd64|arm64) ;;
  *) die "unsupported ZARF_ARCHITECTURE: ${ZARF_ARCHITECTURE}" ;;
esac

verify_sha256 "${ZARF_BINARY_SHA256}" "${DATAENGINE_BUNDLE_DIR}/zarf" "Zarf binary"
verify_sha256 "${DATAENGINE_PACKAGE_SHA256}" \
  "${DATAENGINE_BUNDLE_DIR}/${DATAENGINE_PACKAGE}" "DataEngine package"
ZARF_DEPLOY_HELP=$("${DATAENGINE_BUNDLE_DIR}/zarf" package deploy --help)
grep -Fq -- '--architecture' <<< "${ZARF_DEPLOY_HELP}" || \
  die "release-supplied Zarf package deploy does not support explicit --architecture"
mutation_namespaces >/dev/null
for namespace_name in \
  "${DATAENGINE_NAMESPACE}" \
  "${KNATIVE_SERVING_NAMESPACE}" \
  "${KNATIVE_EVENTING_NAMESPACE}"; do
  require_namespace_in_mutation_scope "${namespace_name}"
done
assert_existing_mutation_scope_is_approved

if [[ -n "${ZOT_CA_BUNDLE:-}" ]]; then
  [[ -s "${ZOT_CA_BUNDLE}" ]] || die "Zot CA bundle not found or empty: ${ZOT_CA_BUNDLE}"
fi

if ! oc get namespace zarf >/dev/null 2>&1; then
  for value_name in ZARF_INIT_PACKAGE ZARF_INIT_PACKAGE_SHA256; do
    require_value "${value_name}"
  done
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
else
  printf 'NOTICE: namespace/zarf already exists; init-zarf.sh will fail closed. Verify the installed release separately.\n'
fi

printf 'Zot chart version: %s\n' "${ZOT_CHART_VERSION}"
printf 'Zot route: https://%s\n' "${ZOT_HOST}"
printf 'Zot StorageClass: %s\n' "${ZOT_STORAGE_CLASS}"
printf 'DataEngine package: %s\n' "${DATAENGINE_PACKAGE}"
printf 'Zarf architecture: %s\n' "${ZARF_ARCHITECTURE}"
printf 'Approved Zarf mutation namespaces: %s\n' "${ZARF_MUTATION_NAMESPACES}"

printf '\nAdministration-host inotify limits\n'
sysctl fs.inotify.max_user_instances 2>/dev/null || true
sysctl fs.inotify.max_user_watches 2>/dev/null || true

printf '\nPASS: preflight readiness and release-integrity checks completed.\n'
