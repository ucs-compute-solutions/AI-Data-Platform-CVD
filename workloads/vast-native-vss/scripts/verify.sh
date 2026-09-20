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

for command_name in grep helm oc python3 vastde; do require_command "${command_name}"; done
load_env "${env_file}"
require_application_digests
verify_context

helm -n "${CVD_NAMESPACE}" status "${CVD_RELEASE}" >/dev/null
for deployment in vast-vss-app-backend vast-vss-app-frontend; do
  deployment_state="$(oc -n "${CVD_NAMESPACE}" get deployment "${deployment}" \
    -o jsonpath='{.spec.replicas}{"|"}{.status.updatedReplicas}{"|"}{.status.readyReplicas}{"|"}{.status.availableReplicas}{"|"}{.metadata.labels.app\.kubernetes\.io/instance}')"
  IFS='|' read -r desired updated ready available instance <<<"${deployment_state}"
  [[ "${desired}" =~ ^[1-9][0-9]*$ ]] || \
    die "Deployment ${deployment} has an invalid desired replica count: ${desired:-unset}"
  [[ "${updated}" == "${desired}" && "${ready}" == "${desired}" && \
    "${available}" == "${desired}" ]] || \
    die "Deployment ${deployment} is not fully available: desired=${desired}, updated=${updated:-0}, ready=${ready:-0}, available=${available:-0}"
  [[ "${instance}" == "${CVD_RELEASE}" ]] || \
    die "Deployment ${deployment} belongs to Helm instance ${instance:-unset}, expected ${CVD_RELEASE}"
done

for service in video-segmenter-2 video-reasoner-3 video-embedder-4 video-vastdb-writer-5; do
  state="$(oc -n "${CVD_NAMESPACE}" get ksvc "${service}" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')"
  [[ "${state}" == "True" ]] || \
    die "Knative Service ${service} is not Ready: ${state:-unknown}"
done

route_names=(vast-vss-app-backend vast-vss-app-frontend)
route_paths=(/api /)
for index in "${!route_names[@]}"; do
  route="${route_names[$index]}"
  route_state="$(oc -n "${CVD_NAMESPACE}" get route "${route}" \
    -o jsonpath='{.spec.host}{"|"}{.spec.path}{"|"}{.spec.tls.termination}{"|"}{.spec.tls.insecureEdgeTerminationPolicy}{"|"}{.status.ingress[0].conditions[?(@.type=="Admitted")].status}')"
  IFS='|' read -r host path termination insecure_policy admitted <<<"${route_state}"
  [[ "${host}" == "${CVD_ROUTE_HOST}" && "${path}" == "${route_paths[$index]}" ]] || \
    die "Route ${route} target mismatch: host=${host:-unset}, path=${path:-unset}"
  [[ "${termination}" == "edge" && "${insecure_policy}" == "Redirect" ]] || \
    die "Route ${route} does not enforce the expected edge TLS redirect"
  [[ "${admitted}" == "True" ]] || die "Route ${route} is not Admitted: ${admitted:-unknown}"
done

vast_resources=(
  vmsview/vss-video-chunks
  vmsview/vss-video-chunks-segments
  vmsview/vss-processed-videos-db
  vmsvdbview/vss-processed-videos-db
)
vast_buckets=(video-chunks video-chunks-segments processed-videos-db processed-videos-db)
for index in "${!vast_resources[@]}"; do
  resource="${vast_resources[$index]}"
  resource_state="$(oc -n "${CVD_CONTROL_NAMESPACE}" get "${resource}" \
    -o jsonpath='{.status.phase}{"|"}{.spec.bucketName}{"|"}{.spec.tenantName}')"
  IFS='|' read -r phase bucket tenant <<<"${resource_state}"
  [[ "${phase}" == "Ready" ]] || die "${resource} is not Ready: ${phase:-unknown}"
  [[ "${bucket}" == "${vast_buckets[$index]}" && "${tenant}" == "${CVD_TENANT_NAME}" ]] || \
    die "${resource} specification does not match the reviewed bucket and tenant"
done
vdb_schema="$(oc -n "${CVD_CONTROL_NAMESPACE}" get vmsvdbview/vss-processed-videos-db \
  -o jsonpath='{.spec.schemaName}')"
[[ "${vdb_schema}" == "processed-videos-schema" ]] || \
  die "VASTDB schema mismatch: ${vdb_schema:-unset}"

topic_state="$(oc -n "${CVD_CONTROL_NAMESPACE}" get vmskafkatopic/vss-video-events \
  -o jsonpath='{.status.phase}{"|"}{.spec.topicName}{"|"}{.spec.brokerRef}{"|"}{.spec.tenantName}')"
IFS='|' read -r topic_phase topic_name broker_ref topic_tenant <<<"${topic_state}"
[[ "${topic_phase}" == "Ready" ]] || \
  die "vmskafkatopic/vss-video-events is not Ready: ${topic_phase:-unknown}"
[[ "${topic_name}" == "${CVD_TOPIC_NAME}" && "${broker_ref}" == "${CVD_BROKER_RESOURCE}" && \
  "${topic_tenant}" == "${CVD_TENANT_NAME}" ]] || \
  die "vmskafkatopic/vss-video-events does not match the reviewed topic, broker, and tenant"

nim_services=("${CVD_EMBEDDING_NIMSERVICE}" "${CVD_COSMOS_NIMSERVICE}" "${CVD_LLM_NIMSERVICE}")
nim_models=(
  "$(lock_value models.embedding.id)"
  "$(lock_value models.reasoning.id)"
  "$(lock_value models.synthesis.id)"
)
nim_versions=(
  "$(lock_value models.embedding.nimVersion)"
  "$(lock_value models.reasoning.nimVersion)"
  "$(lock_value models.synthesis.nimVersion)"
)
for index in "${!nim_services[@]}"; do
  service="${nim_services[$index]}"
  nim_state="$(oc -n "${CVD_NIM_NAMESPACE}" get nimservice "${service}" \
    -o jsonpath='{.status.state}{"|"}{.spec.image.repository}{"|"}{.spec.image.tag}')"
  IFS='|' read -r state repository tag <<<"${nim_state}"
  [[ "${state}" == "Ready" ]] || die "NIMService ${service} is not Ready: ${state:-unknown}"
  [[ "${repository}" == */"${nim_models[$index]}" && "${tag}" == "${nim_versions[$index]}" ]] || \
    die "NIMService ${service} image does not match the locked model and version"
done
for name in video-segmenter video-reasoner video-embedder video-vastdb-writer; do
  vastde_has_name functions "${name}" || die "required function is absent: ${name}"
done
for name in video-chunk-land-trigger video-segment-land-trigger; do
  vastde_has_name triggers "${name}" || die "required trigger is absent: ${name}"
done
vastde_has_name pipelines "${CVD_PIPELINE_NAME}" || die "required pipeline is absent: ${CVD_PIPELINE_NAME}"

warning_events="$(oc -n "${CVD_NAMESPACE}" get events \
  --field-selector type=Warning --sort-by=.lastTimestamp \
  -o jsonpath='{range .items[*]}{.lastTimestamp}{"\t"}{.involvedObject.kind}{"/"}{.involvedObject.name}{"\t"}{.reason}{": "}{.message}{"\n"}{end}')"
if [[ -n "${warning_events}" ]]; then
  printf 'WARNING: review these events before functional acceptance:\n%s\n' "${warning_events}"
  die "Warning events remain in ${CVD_NAMESPACE}; investigate or document and clear them before acceptance"
else
  printf 'PASS: no Warning events are currently recorded in %s.\n' "${CVD_NAMESPACE}"
fi

printf 'PASS: exact application Deployments, Routes, Knative Services, VAST resources, locked NIM images, functions, triggers, and pipeline passed readiness checks.\n'
