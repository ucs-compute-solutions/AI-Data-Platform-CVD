#!/usr/bin/env bash

set -euo pipefail
{ set +x; } 2>/dev/null
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE="$(env_path_from_args "$@")"
for command_name in oc helm python3 awk grep sha256sum; do
  require_command "${command_name}"
done
load_env "${ENV_FILE}"
verify_context
verify_validator_lock
verify_locked_model_profile

printf '\nInsightEngine deployment verification\n'
"${CVD_INSIGHTENGINE_VERIFY}" --env "${CVD_INSIGHTENGINE_ENV_FILE}"
verify_insightengine_release

printf '\nDocument RAG execution path\n'
helm -n "${CVD_INSIGHTENGINE_NAMESPACE}" list
oc -n "${CVD_INSIGHTENGINE_NAMESPACE}" get \
  "deployment/${CVD_BACKEND_DEPLOYMENT}" -o wide
oc -n "${CVD_INSIGHTENGINE_NAMESPACE}" get \
  "ingestionpipeline.vast.io/${CVD_PIPELINE_NAME}"
oc -n "${CVD_INSIGHTENGINE_NAMESPACE}" get \
  ksvc,revisions,triggers -l "pipeline=${CVD_PIPELINE_NAME}"

available="$(oc -n "${CVD_INSIGHTENGINE_NAMESPACE}" get \
  "deployment/${CVD_BACKEND_DEPLOYMENT}" \
  -o jsonpath='{.status.availableReplicas}')"
desired="$(oc -n "${CVD_INSIGHTENGINE_NAMESPACE}" get \
  "deployment/${CVD_BACKEND_DEPLOYMENT}" \
  -o jsonpath='{.spec.replicas}')"
[[ -n "${desired}" && "${available:-0}" == "${desired}" ]] || \
  die "deployment/${CVD_BACKEND_DEPLOYMENT} is not fully available"

pipeline_phase="$(oc -n "${CVD_INSIGHTENGINE_NAMESPACE}" get \
  "ingestionpipeline.vast.io/${CVD_PIPELINE_NAME}" \
  -o jsonpath='{.status.phase}')"
[[ "${pipeline_phase}" == "Ready" ]] || \
  die "ingestionpipeline/${CVD_PIPELINE_NAME} is not Ready"

printf '\nLocal NVIDIA model services\n'
for nimservice in \
  "${CVD_EMBEDDING_NIMSERVICE}" \
  "${CVD_LLM_NIMSERVICE}" \
  "${CVD_RERANKER_NIMSERVICE}"; do
  verify_ready_condition "${CVD_NIM_NAMESPACE}" nimservice.apps.nvidia.com "${nimservice}"
  oc -n "${CVD_NIM_NAMESPACE}" get "nimservice.apps.nvidia.com/${nimservice}"
done

printf '\nPASS: Document RAG platform, ingestion path, locked validators, and model services are ready.\n'
