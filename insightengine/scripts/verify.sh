#!/usr/bin/env bash

set -euo pipefail
{ set +x; } 2>/dev/null
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE=$(env_path_from_args "$@")
load_inputs "${ENV_FILE}"
for command_name in oc helm python3 grep sed awk sha256sum mktemp sort; do
  require_command "${command_name}"
done
require_inputs
print_context
verify_context
verify_delivery
require_site_files

[[ "${VAST_MAPPING_CONFIRMED:-false}" == "true" ]] || die "VAST_MAPPING_CONFIRMED is not true"
[[ "${VAST_PREREQUISITES_CONFIRMED:-false}" == "true" ]] || die "VAST_PREREQUISITES_CONFIRMED is not true"
[[ "${MANAGER_CREDENTIAL_MATCH_CONFIRMED:-false}" == "true" ]] || die "MANAGER_CREDENTIAL_MATCH_CONFIRMED is not true"
[[ "${END_USER_POLICY_ASSIGNMENT_CONFIRMED:-false}" == "true" ]] || \
  die "END_USER_POLICY_ASSIGNMENT_CONFIRMED is not true; assign ${END_USER_POLICY_NAME} after its CR is Ready"
[[ "${TRUSTED_CA_CONFIRMED:-false}" == "true" ]] || die "TRUSTED_CA_CONFIRMED is not true"

TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "${TMP_DIR}"; }
trap cleanup EXIT

render_expected_chart() {
  local release=$1 chart=$2 values=$3 output=$4
  helm template "${release}" "$(chart_path "${chart}")" \
    -n "${INSIGHTENGINE_NAMESPACE}" -f "${values}" > "${output}"
  assert_no_latest_images "${output}"
  assert_no_inline_secrets "${output}"
}

render_expected_chart "${POSTGRES_RELEASE}" "${POSTGRES_CHART}" "${POSTGRES_VALUES}" "${TMP_DIR}/postgres.yaml"
render_expected_chart "${OPERATOR_RELEASE}" "${OPERATOR_CHART}" "${OPERATOR_VALUES}" "${TMP_DIR}/operator.yaml"
render_expected_chart "${BACKEND_RELEASE}" "${BACKEND_CHART}" "${BACKEND_VALUES}" "${TMP_DIR}/backend.yaml"
assert_v54_render_contract \
  "${TMP_DIR}/postgres.yaml" "${TMP_DIR}/operator.yaml" "${TMP_DIR}/backend.yaml"
write_manifest_image_inventory "${TMP_DIR}/postgres-images.txt" "${TMP_DIR}/postgres.yaml"
write_manifest_image_inventory "${TMP_DIR}/operator-images.txt" "${TMP_DIR}/operator.yaml"
write_manifest_image_inventory "${TMP_DIR}/backend-images.txt" "${TMP_DIR}/backend.yaml"

printf '\nHelm and application workloads\n'
helm -n "${INSIGHTENGINE_NAMESPACE}" list
for release_name in "${POSTGRES_RELEASE}" "${OPERATOR_RELEASE}" "${BACKEND_RELEASE}"; do
  helm -n "${INSIGHTENGINE_NAMESPACE}" status "${release_name}" -o json | \
    python3 "${JSON_CHECK}" helm-status --expected deployed >/dev/null || \
    die "Helm release is not deployed: ${release_name}"
done

helm -n "${INSIGHTENGINE_NAMESPACE}" get manifest "${POSTGRES_RELEASE}" > "${TMP_DIR}/live-postgres.yaml"
helm -n "${INSIGHTENGINE_NAMESPACE}" get manifest "${OPERATOR_RELEASE}" > "${TMP_DIR}/live-operator.yaml"
helm -n "${INSIGHTENGINE_NAMESPACE}" get manifest "${BACKEND_RELEASE}" > "${TMP_DIR}/live-backend.yaml"
assert_no_latest_images "${TMP_DIR}/live-postgres.yaml"
assert_no_latest_images "${TMP_DIR}/live-operator.yaml"
assert_no_latest_images "${TMP_DIR}/live-backend.yaml"
assert_no_inline_secrets "${TMP_DIR}/live-postgres.yaml"
assert_no_inline_secrets "${TMP_DIR}/live-operator.yaml"
assert_no_inline_secrets "${TMP_DIR}/live-backend.yaml"
assert_v54_render_contract \
  "${TMP_DIR}/live-postgres.yaml" "${TMP_DIR}/live-operator.yaml" "${TMP_DIR}/live-backend.yaml"

assert_installed_chart_version() {
  local release=$1 chart=$2 expected installed
  expected=$(chart_version "${chart}")
  installed=$(helm -n "${INSIGHTENGINE_NAMESPACE}" get metadata "${release}" -o json | \
    python3 "${JSON_CHECK}" helm-chart-version)
  [[ "${installed}" == "${expected}" ]] || \
    die "release/${release} chart version ${installed} does not match reviewed chart ${expected}"
}
assert_installed_chart_version "${POSTGRES_RELEASE}" "${POSTGRES_CHART}"
assert_installed_chart_version "${OPERATOR_RELEASE}" "${OPERATOR_CHART}"
assert_installed_chart_version "${BACKEND_RELEASE}" "${BACKEND_CHART}"

oc -n "${INSIGHTENGINE_NAMESPACE}" get deployments,statefulsets,pods,pvc -o wide

assert_deployment_release() {
  local release=$1 allowed_images=$2 workload_json
  workload_json=$(mktemp "${TMP_DIR}/deployment.XXXXXX.json")
  oc -n "${INSIGHTENGINE_NAMESPACE}" get deployments \
    -l "app.kubernetes.io/instance=${release}" -o json > "${workload_json}"
  python3 "${JSON_CHECK}" controllers-ready --kind deployment < "${workload_json}" || \
    die "release/${release} has no fully available Deployment"
  python3 "${JSON_CHECK}" workloads-reference-secret \
    --secret "${APP_PULL_SECRET_NAME}" < "${workload_json}" || \
    die "release/${release} does not bind application pull Secret/${APP_PULL_SECRET_NAME}"
  python3 "${JSON_CHECK}" images-allowed \
    --allowed-file "${allowed_images}" < "${workload_json}" || \
    die "release/${release} runs an image outside its reviewed rendered manifest"
}
assert_deployment_release "${OPERATOR_RELEASE}" "${TMP_DIR}/operator-images.txt"
assert_deployment_release "${BACKEND_RELEASE}" "${TMP_DIR}/backend-images.txt"

postgres_json=$(mktemp "${TMP_DIR}/statefulset.XXXXXX.json")
oc -n "${INSIGHTENGINE_NAMESPACE}" get statefulsets \
  -l "app.kubernetes.io/instance=${POSTGRES_RELEASE}" -o json > "${postgres_json}"
python3 "${JSON_CHECK}" controllers-ready --kind statefulset < "${postgres_json}" || \
  die "release/${POSTGRES_RELEASE} has no fully ready StatefulSet"
python3 "${JSON_CHECK}" workloads-reference-secret \
  --secret "${APP_PULL_SECRET_NAME}" < "${postgres_json}" || \
  die "release/${POSTGRES_RELEASE} does not bind application pull Secret/${APP_PULL_SECRET_NAME}"
python3 "${JSON_CHECK}" images-allowed \
  --allowed-file "${TMP_DIR}/postgres-images.txt" < "${postgres_json}" || \
  die "release/${POSTGRES_RELEASE} runs an image outside its reviewed rendered manifest"

oc -n "${INSIGHTENGINE_NAMESPACE}" get pvc \
  -l "app.kubernetes.io/instance=${POSTGRES_RELEASE}" -o json | \
  python3 "${JSON_CHECK}" pvcs-ready --storage-class "${STORAGE_CLASS}" >/dev/null || \
  die "one or more application PVCs are absent, unbound, or use an unexpected StorageClass"

printf '\nRequired Secrets (metadata and key names only)\n'
for secret_name in "${RUNTIME_SECRET_NAME}" "${POSTGRES_SECRET_NAME}" "${APP_PULL_SECRET_NAME}"; do
  oc -n "${INSIGHTENGINE_NAMESPACE}" get secret "${secret_name}" \
    -o custom-columns='NAME:.metadata.name,TYPE:.type'
done
if [[ "${INGEST_REGISTRY_AUTH_TYPE}" == "secret" ]]; then
  oc -n "${INSIGHTENGINE_NAMESPACE}" get secret "${INGEST_PULL_SECRET_NAME}" \
    -o custom-columns='NAME:.metadata.name,TYPE:.type'
fi

oc -n "${INSIGHTENGINE_NAMESPACE}" get secret "${APP_PULL_SECRET_NAME}" -o json | \
  python3 "${JSON_CHECK}" pull-secret-valid >/dev/null || \
  die "Secret/${APP_PULL_SECRET_NAME} is not a valid dockerconfigjson pull Secret"
if [[ "${INGEST_REGISTRY_AUTH_TYPE}" == "secret" ]]; then
  oc -n "${INSIGHTENGINE_NAMESPACE}" get secret "${INGEST_PULL_SECRET_NAME}" -o json | \
    python3 "${JSON_CHECK}" pull-secret-valid >/dev/null || \
    die "Secret/${INGEST_PULL_SECRET_NAME} is not a valid dockerconfigjson pull Secret"
fi

oc -n "${INSIGHTENGINE_NAMESPACE}" get secret "${RUNTIME_SECRET_NAME}" -o json | \
  python3 "${JSON_CHECK}" secret-has-keys --keys "${RUNTIME_SECRET_REQUIRED_KEYS}" >/dev/null || \
  die "Secret/${RUNTIME_SECRET_NAME} is missing a required key"
oc -n "${INSIGHTENGINE_NAMESPACE}" get secret "${POSTGRES_SECRET_NAME}" -o json | \
  python3 "${JSON_CHECK}" secret-has-keys --keys "${POSTGRES_SECRET_REQUIRED_KEYS}" >/dev/null || \
  die "Secret/${POSTGRES_SECRET_NAME} is missing a required key"
oc -n "${INSIGHTENGINE_NAMESPACE}" get secret "${RUNTIME_SECRET_NAME}" -o json | \
  python3 "${JSON_CHECK}" secret-value-equals \
    --key MGMT__USERNAME --expected "${EXPECTED_APP_MANAGER_USERNAME}" >/dev/null || \
  die "runtime MGMT__USERNAME does not match EXPECTED_APP_MANAGER_USERNAME"

# Compare encoded values without decoding, displaying, or retaining complete
# Secret objects. Xtrace is disabled at script entry.
runtime_postgres_password=$(oc -n "${INSIGHTENGINE_NAMESPACE}" get secret \
  "${RUNTIME_SECRET_NAME}" -o jsonpath='{.data.POSTGRES__PASSWORD}')
postgres_user_password=$(oc -n "${INSIGHTENGINE_NAMESPACE}" get secret \
  "${POSTGRES_SECRET_NAME}" -o jsonpath='{.data.password}')
postgres_admin_password=$(oc -n "${INSIGHTENGINE_NAMESPACE}" get secret \
  "${POSTGRES_SECRET_NAME}" -o jsonpath='{.data.postgres-password}')
[[ -n "${runtime_postgres_password}" && \
   ("${runtime_postgres_password}" == "${postgres_user_password}" || \
    "${runtime_postgres_password}" == "${postgres_admin_password}") ]] || \
  die "runtime POSTGRES__PASSWORD does not match the PostgreSQL chart Secret"
unset runtime_postgres_password postgres_user_password postgres_admin_password

oc -n "${INSIGHTENGINE_NAMESPACE}" get secret "${RUNTIME_SECRET_NAME}" \
  -o go-template='{{range $key, $_ := .data}}{{$key}}{{"\n"}}{{end}}' | sort

printf '\nVAST custom resources\n'
resource_types=(
  vmsusers.vast.io
  vmsgroups.vast.io
  vmss3policies.vast.io
  vmsviews.vast.io
  vmsvdbviews.vast.io
  vmskafkabrokers.vast.io
  ingestionpipelines.vast.io
)
for resource_type in "${resource_types[@]}"; do
  oc api-resources --api-group=vast.io -o name | grep -Fxq "${resource_type}" || \
    die "required VAST API is absent: ${resource_type}"
  oc -n "${INSIGHTENGINE_NAMESPACE}" get "${resource_type}" || true
done

declare -A expected_vast_objects=(
  [vmsusers.vast.io]="${EXPECTED_VMS_USER_CR_NAME}"
  [vmsgroups.vast.io]="${EXPECTED_VMS_GROUP_CR_NAME}"
  [vmss3policies.vast.io]="${EXPECTED_END_USER_POLICY_CR_NAME}"
  [vmsviews.vast.io]="${EXPECTED_S3_VIEW_CR_NAME}"
  [vmsvdbviews.vast.io]="${EXPECTED_VDB_VIEW_CR_NAME}"
  [vmskafkabrokers.vast.io]="${EXPECTED_VMS_BROKER_CR_NAME}"
)
for resource_type in "${!expected_vast_objects[@]}"; do
  object_name=${expected_vast_objects[$resource_type]}
  oc -n "${INSIGHTENGINE_NAMESPACE}" get "${resource_type}/${object_name}" -o json | \
    python3 "${JSON_CHECK}" resource-ready >/dev/null || \
    die "expected VAST object is not Ready: ${resource_type}/${object_name}"
done

oc -n "${INSIGHTENGINE_NAMESPACE}" get \
  "vmss3policies.vast.io/${EXPECTED_END_USER_POLICY_CR_NAME}" -o json | \
  python3 "${JSON_CHECK}" policy-name --expected "${END_USER_POLICY_NAME}" >/dev/null || \
  die "expected end-user policy name is not rendered by ${EXPECTED_END_USER_POLICY_CR_NAME}"

oc -n "${INSIGHTENGINE_NAMESPACE}" get \
  "vmskafkabrokers.vast.io/${EXPECTED_VMS_BROKER_CR_NAME}" -o json | \
  python3 "${JSON_CHECK}" broker --expected "${EXPECTED_VAST_BROKER_NAME}" >/dev/null || \
  die "VMSKafkaBroker/${EXPECTED_VMS_BROKER_CR_NAME} does not resolve ${EXPECTED_VAST_BROKER_NAME}"

oc -n "${INSIGHTENGINE_NAMESPACE}" get \
  "ingestionpipelines.vast.io/${PIPELINE_NAME}" -o json | \
  python3 "${JSON_CHECK}" pipeline \
    --topic "${PIPELINE_TOPIC_NAME}" \
    --broker-name "${PIPELINE_BROKER_NAME}" \
    --registry "${INGEST_REGISTRY_NAME}" \
    --cluster "${INGEST_K8S_CLUSTER_NAME}" \
    --image-repository "${INGEST_IMAGE_REPOSITORY}" \
    --image-tag "${INGEST_IMAGE_TAG}" >/dev/null || \
  die "IngestionPipeline/${PIPELINE_NAME} does not match the reviewed topic, broker, registry, cluster, or image"

if [[ -n "${EXPECTED_AUXILIARY_TOPIC_NAMES:-}" ]]; then
  oc api-resources --api-group=vast.io -o name | grep -Fxq vmskafkatopics.vast.io || \
    die "auxiliary topic CR verification requested, but vmskafkatopics.vast.io is absent"
  IFS=',' read -r -a expected_topics <<< "${EXPECTED_AUXILIARY_TOPIC_NAMES}"
  for topic_name in "${expected_topics[@]}"; do
    oc -n "${INSIGHTENGINE_NAMESPACE}" get vmskafkatopics.vast.io -o json | \
      python3 "${JSON_CHECK}" topic --expected "${topic_name}" >/dev/null || \
      die "required Ready VMSKafkaTopic is absent: ${topic_name}"
  done
fi

printf '\nKnative execution objects\n'
expected_ingest_image=$(expected_ingest_image)
expected_ingest_repository="${REGISTRY_TARGET%/}/${INGEST_IMAGE_REPOSITORY#/}"

ksvc_json=$(mktemp "${TMP_DIR}/ksvc.XXXXXX.json")
oc -n "${INSIGHTENGINE_NAMESPACE}" get ksvc \
  -l "pipeline=${PIPELINE_NAME}" -o json > "${ksvc_json}"
python3 "${JSON_CHECK}" knative-all-ready < "${ksvc_json}" || \
  die "the pipeline KService is absent or not Ready"
service_name=$(python3 "${JSON_CHECK}" single-item-name < "${ksvc_json}") || \
  die "release 5.4.3 expects exactly one KService for pipeline/${PIPELINE_NAME}"
python3 "${JSON_CHECK}" image-present --expected "${expected_ingest_image}" < "${ksvc_json}" || \
  die "the pipeline KService does not use ${expected_ingest_image}"

trigger_json=$(mktemp "${TMP_DIR}/trigger.XXXXXX.json")
oc -n "${INSIGHTENGINE_NAMESPACE}" get triggers \
  -l "pipeline=${PIPELINE_NAME}" -o json > "${trigger_json}"
trigger_broker=$(python3 "${JSON_CHECK}" trigger-contract \
  --service "${service_name}" < "${trigger_json}") || \
  die "the pipeline Trigger is absent, not Ready, or does not target Service/${service_name}"

broker_json=$(mktemp "${TMP_DIR}/broker.XXXXXX.json")
oc -n "${INSIGHTENGINE_NAMESPACE}" get broker "${trigger_broker}" -o json > "${broker_json}"
python3 "${JSON_CHECK}" broker-contract --topic "${PIPELINE_TOPIC_NAME}" < "${broker_json}" || \
  die "Broker/${trigger_broker} is not Ready or does not use topic ${PIPELINE_TOPIC_NAME}"

revision_name=$(oc -n "${INSIGHTENGINE_NAMESPACE}" get ksvc "${service_name}" -o json | \
  python3 "${JSON_CHECK}" latest-ready-revision) || \
  die "Service/${service_name} has no latest Ready revision"
revision_json=$(mktemp "${TMP_DIR}/revision.XXXXXX.json")
oc -n "${INSIGHTENGINE_NAMESPACE}" get revision "${revision_name}" -o json > "${revision_json}"
python3 "${JSON_CHECK}" knative-all-ready < "${revision_json}" || \
  die "Revision/${revision_name} is not Ready"
python3 "${JSON_CHECK}" image-present --expected "${expected_ingest_image}" < "${revision_json}" || \
  die "Revision/${revision_name} does not use ${expected_ingest_image}"

ingest_pods_json=$(mktemp "${TMP_DIR}/ingest-pods.XXXXXX.json")
oc -n "${INSIGHTENGINE_NAMESPACE}" get pods \
  -l "serving.knative.dev/service=${service_name},serving.knative.dev/revision=${revision_name}" \
  -o json > "${ingest_pods_json}"
python3 "${JSON_CHECK}" pods-ready < "${ingest_pods_json}" || \
  die "the current pipeline Knative Pod is absent or not Ready"
python3 "${JSON_CHECK}" image-repository-present \
  --repository "${expected_ingest_repository}" < "${ingest_pods_json}" || \
  die "the current pipeline Pod does not use repository ${expected_ingest_repository}"

if [[ "${INGEST_REGISTRY_AUTH_TYPE}" == "secret" ]]; then
  for object_json in "${ksvc_json}" "${revision_json}" "${ingest_pods_json}"; do
    python3 "${JSON_CHECK}" workloads-reference-secret \
      --secret "${INGEST_PULL_SECRET_NAME}" < "${object_json}" >/dev/null || \
      die "the selected pipeline workload does not reference Secret/${INGEST_PULL_SECRET_NAME}"
  done
fi

oc -n "${INSIGHTENGINE_NAMESPACE}" get ksvc "${service_name}"
oc -n "${INSIGHTENGINE_NAMESPACE}" get trigger -l "pipeline=${PIPELINE_NAME}"
oc -n "${INSIGHTENGINE_NAMESPACE}" get broker "${trigger_broker}"
oc -n "${INSIGHTENGINE_NAMESPACE}" get revision "${revision_name}"
oc -n "${INSIGHTENGINE_NAMESPACE}" get pods \
  -l "serving.knative.dev/service=${service_name},serving.knative.dev/revision=${revision_name}"

printf '\nCurrent image-pull state\n'
# Warning events can outlive the condition that created them. Current pod and
# controller assertions are authoritative; retain these events as evidence.
oc -n "${INSIGHTENGINE_NAMESPACE}" get events \
  --field-selector type=Warning --sort-by=.lastTimestamp || true

printf '\nPASS: chart versions, rendered images, workloads, PVCs, Secrets, VAST resources, and Knative objects match the reviewed 5.4.3 inputs.\n'
printf 'FINAL GATES: prove model discovery, an uncached pinned-image pull, and a fresh private-collection ingestion, retrieval, reranking, and grounded response.\n'
