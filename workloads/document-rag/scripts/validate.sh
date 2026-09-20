#!/usr/bin/env bash

set -euo pipefail
{ set +x; } 2>/dev/null
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

env_file=""
stage=""
while (( $# )); do
  case "$1" in
    --env) env_file="${2:-}"; shift 2 ;;
    --stage) stage="${2:-}"; shift 2 ;;
    *) die "usage: $0 --env <release-inputs.env> --stage <models|functional|embedding|all>" ;;
  esac
done
[[ -n "${env_file}" && "${stage}" =~ ^(models|functional|embedding|all)$ ]] || \
  die "usage: $0 --env <release-inputs.env> --stage <models|functional|embedding|all>"

for command_name in oc helm python3 awk grep sha256sum; do
  require_command "${command_name}"
done
load_env "${env_file}"
verify_context
verify_validator_lock
verify_locked_model_profile

"${SCRIPT_DIR}/preflight.sh" --env "${env_file}"

run_in_backend() {
  local script="$1"
  shift
  oc -n "${CVD_INSIGHTENGINE_NAMESPACE}" exec -i \
    "deployment/${CVD_BACKEND_DEPLOYMENT}" \
    -c "${CVD_BACKEND_CONTAINER}" -- \
    python3 -u - "$@" < "${script}"
}

run_models() {
  printf '\nCVD-RAG-02: local model API smoke test\n'
  run_in_backend "$(validator_path validateLocalNimsPath)" \
    --embedding-base "${CVD_EMBEDDING_BASE_URL}" \
    --embedding-model "${CVD_EMBEDDING_MODEL}" \
    --llm-base "${CVD_LLM_BASE_URL}" \
    --llm-model "${CVD_LLM_MODEL}" \
    --reranker-base "${CVD_RERANKER_BASE_URL}" \
    --reranker-model "${CVD_RERANKER_MODEL}" \
    --expected-dimensions "${CVD_EMBEDDING_DIMENSIONS}"
}

run_functional() {
  printf '\nCVD-RAG-03 through CVD-RAG-09: functional acceptance\n'
  printf 'The suite creates and retains private synthetic collections, documents, and conversations.\n'
  run_in_backend "$(validator_path validateDocumentRagPath)" \
    --base "${CVD_BACKEND_BASE_URL}" \
    --secret-file "${CVD_RUNTIME_CONFIG_FILE}" \
    --ingest-timeout "${CVD_INGEST_TIMEOUT}" \
    --retrieve-timeout "${CVD_RETRIEVE_TIMEOUT}" \
    --prompt-timeout "${CVD_PROMPT_TIMEOUT}" \
    --with-rerank
}

run_embedding() {
  local input_type
  printf '\nCVD-RAG-10: bounded embedding reliability\n'
  for input_type in query passage; do
    run_in_backend "$(validator_path probeEmbeddingPath)" \
      --base "${CVD_EMBEDDING_BASE_URL}" \
      --model "${CVD_EMBEDDING_MODEL}" \
      --expected-dimensions "${CVD_EMBEDDING_DIMENSIONS}" \
      --repeat "${CVD_EMBEDDING_REPEAT}" \
      --input-type "${input_type}"
  done
}

case "${stage}" in
  models) run_models ;;
  functional) run_functional ;;
  embedding) run_embedding ;;
  all)
    run_models
    run_functional
    run_embedding
    ;;
esac

printf '\nPASS: requested Document RAG acceptance stage completed.\n'
