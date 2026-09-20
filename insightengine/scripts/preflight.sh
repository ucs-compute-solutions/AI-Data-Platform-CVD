#!/usr/bin/env bash

set -euo pipefail
{ set +x; } 2>/dev/null
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE=$(env_path_from_args "$@")
load_inputs "${ENV_FILE}"

for command_name in oc helm python3 grep sed awk sha256sum mktemp; do
  require_command "${command_name}"
done
require_inputs
print_context
verify_context
verify_delivery
require_site_files

printf '\nOpenShift platform\n'
oc get nodes
oc get clusteroperators
oc get storageclass "${STORAGE_CLASS}"
[[ "$(oc get storageclass "${STORAGE_CLASS}" -o jsonpath='{.provisioner}')" == "${EXPECTED_CSI_PROVISIONER}" ]] || \
  die "StorageClass provisioner does not match ${EXPECTED_CSI_PROVISIONER}"
for namespace_name in \
  "${DATAENGINE_NAMESPACE}" \
  "${KNATIVE_SERVING_NAMESPACE}" \
  "${KNATIVE_EVENTING_NAMESPACE}"; do
  oc get namespace "${namespace_name}"
done

for namespace_name in \
  "${DATAENGINE_NAMESPACE}" \
  "${KNATIVE_SERVING_NAMESPACE}" \
  "${KNATIVE_EVENTING_NAMESPACE}"; do
  oc -n "${namespace_name}" get pods -o json | \
    python3 "${JSON_CHECK}" pods-ready >/dev/null || \
    die "namespace/${namespace_name} contains an unready platform pod"
done

for verb_resource in \
  'create deployments.apps' 'create statefulsets.apps' \
  'create services' 'create secrets' 'create persistentvolumeclaims'; do
  set -- ${verb_resource}
  [[ "$(oc auth can-i "$1" "$2" -n "${INSIGHTENGINE_NAMESPACE}")" == "yes" ]] || \
    die "current identity cannot $1 $2 in namespace/${INSIGHTENGINE_NAMESPACE}"
done

oc get nodes -o json | python3 "${JSON_CHECK}" nodes-ready >/dev/null || \
  die "one or more OpenShift nodes are not Ready"
oc get clusteroperators -o json | \
  python3 "${JSON_CHECK}" clusteroperators-ready >/dev/null || \
  die "one or more ClusterOperators are unavailable or degraded"

printf '\nRelease charts and reviewed values\n'
TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "${TMP_DIR}"; }
trap cleanup EXIT

helm lint "$(chart_path "${POSTGRES_CHART}")" -f "${POSTGRES_VALUES}"
helm lint "$(chart_path "${OPERATOR_CHART}")" -f "${OPERATOR_VALUES}"
helm lint "$(chart_path "${BACKEND_CHART}")" -f "${BACKEND_VALUES}"

helm template "${POSTGRES_RELEASE}" "$(chart_path "${POSTGRES_CHART}")" \
  -n "${INSIGHTENGINE_NAMESPACE}" -f "${POSTGRES_VALUES}" > "${TMP_DIR}/postgres.yaml"
helm template "${OPERATOR_RELEASE}" "$(chart_path "${OPERATOR_CHART}")" \
  -n "${INSIGHTENGINE_NAMESPACE}" -f "${OPERATOR_VALUES}" > "${TMP_DIR}/operator.yaml"
helm template "${BACKEND_RELEASE}" "$(chart_path "${BACKEND_CHART}")" \
  -n "${INSIGHTENGINE_NAMESPACE}" -f "${BACKEND_VALUES}" > "${TMP_DIR}/backend.yaml"

for manifest_file in "${TMP_DIR}"/*.yaml; do
  assert_no_latest_images "${manifest_file}"
  assert_no_inline_secrets "${manifest_file}"
done
assert_v54_render_contract \
  "${TMP_DIR}/postgres.yaml" "${TMP_DIR}/operator.yaml" "${TMP_DIR}/backend.yaml"

[[ "$(chart_version "${OPERATOR_CHART}")" == "${EXPECTED_IE_VERSION}" ]] || \
  die "operator chart version does not match EXPECTED_IE_VERSION"
[[ "$(chart_version "${BACKEND_CHART}")" == "${EXPECTED_IE_VERSION}" ]] || \
  die "backend chart version does not match EXPECTED_IE_VERSION"

printf '\nPASS: OpenShift, delivery-integrity, StorageClass, chart, and v5.4 values checks completed.\n'
