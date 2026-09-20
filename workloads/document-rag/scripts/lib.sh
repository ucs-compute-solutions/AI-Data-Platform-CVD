#!/usr/bin/env bash

set -euo pipefail

readonly CVD_DOCUMENT_RAG_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CVD_REPO_ROOT="$(cd -- "${CVD_DOCUMENT_RAG_DIR}/../.." && pwd)"
readonly CVD_SOURCE_LOCK="${CVD_DOCUMENT_RAG_DIR}/source-lock.yaml"
readonly CVD_INSIGHTENGINE_VERIFY="${CVD_REPO_ROOT}/insightengine/scripts/verify.sh"
readonly CVD_INSIGHTENGINE_JSON_CHECK="${CVD_REPO_ROOT}/insightengine/scripts/json-check.py"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

usage_env() {
  die "usage: $1 --env <release-inputs.env>"
}

env_path_from_args() {
  [[ $# -eq 2 && "$1" == "--env" && -n "${2:-}" ]] || usage_env "$0"
  printf '%s\n' "$2"
}

validate_env_syntax() {
  local env_file="$1"
  python3 - "${env_file}" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
assignment = re.compile(
    r'^[A-Z][A-Z0-9_]*=(?:"[^"$`]*"|\x27[^\x27$`]*\x27|[^\s#;|&<>`$()]+)'
    r'(?:\s+#.*)?$'
)
for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if not assignment.fullmatch(line):
        raise SystemExit(
            f"ERROR: {path}:{number} must contain one literal variable assignment; "
            "shell expansion and commands are not permitted"
        )
    name = line.split("=", 1)[0]
    if re.search(r'(PASSWORD|TOKEN|API_KEY|SECRET|CREDENTIAL)', name):
        raise SystemExit(
            f"ERROR: {path}:{number} contains prohibited credential variable {name}"
        )
PY
}

load_env() {
  local env_file="$1"
  [[ -f "${env_file}" && ! -L "${env_file}" && -r "${env_file}" ]] || \
    die "environment file must be a readable regular, non-symlink file: ${env_file}"
  validate_env_syntax "${env_file}"
  # shellcheck disable=SC1090
  source "${env_file}"

  local required=(
    CVD_EXPECTED_API CVD_EXPECTED_CONTEXT CVD_EXPECTED_USER
    CVD_INSIGHTENGINE_ENV_FILE CVD_INSIGHTENGINE_NAMESPACE
    CVD_BACKEND_RELEASE CVD_BACKEND_DEPLOYMENT CVD_BACKEND_CONTAINER
    CVD_BACKEND_BASE_URL CVD_PIPELINE_NAME CVD_RUNTIME_CONFIG_FILE
    CVD_NIM_NAMESPACE CVD_EMBEDDING_NIMSERVICE CVD_EMBEDDING_BASE_URL
    CVD_EMBEDDING_MODEL CVD_EMBEDDING_DIMENSIONS CVD_LLM_NIMSERVICE
    CVD_LLM_BASE_URL CVD_LLM_MODEL CVD_RERANKER_NIMSERVICE
    CVD_RERANKER_BASE_URL CVD_RERANKER_MODEL CVD_INGEST_TIMEOUT
    CVD_RETRIEVE_TIMEOUT CVD_PROMPT_TIMEOUT CVD_EMBEDDING_REPEAT
    CVD_HELM_TIMEOUT
  )
  local name value
  for name in "${required[@]}"; do
    value="${!name:-}"
    [[ -n "${value}" ]] || die "required variable is empty: ${name}"
    [[ "${value}" != *'<'* && "${value}" != *'>'* ]] || \
      die "replace the placeholder for ${name} in ${env_file}"
  done
  [[ "${CVD_INSIGHTENGINE_ENV_FILE}" == /* ]] || \
    die "CVD_INSIGHTENGINE_ENV_FILE must be an absolute path"
  [[ -f "${CVD_INSIGHTENGINE_ENV_FILE}" && ! -L "${CVD_INSIGHTENGINE_ENV_FILE}" ]] || \
    die "CVD_INSIGHTENGINE_ENV_FILE must name a regular, non-symlink file"
  for name in CVD_INGEST_TIMEOUT CVD_RETRIEVE_TIMEOUT CVD_PROMPT_TIMEOUT CVD_EMBEDDING_REPEAT CVD_EMBEDDING_DIMENSIONS; do
    value="${!name}"
    [[ "${value}" =~ ^[1-9][0-9]*$ ]] || die "${name} must be a positive integer"
  done
  [[ "${CVD_BACKEND_BASE_URL}" =~ ^http://127\.0\.0\.1:[0-9]+$ ]] || \
    die "CVD_BACKEND_BASE_URL must be an HTTP loopback origin with an explicit port"
}

lock_value() {
  local key="$1"
  awk -F ':' -v key="${key}" '
    $1 == key {
      value = substr($0, index($0, ":") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      gsub(/^"|"$/, "", value)
      print value
      exit
    }
  ' "${CVD_SOURCE_LOCK}"
}

verify_locked_file() {
  local path_key="$1" hash_key="$2" relative expected actual
  relative="$(lock_value "${path_key}")"
  expected="$(lock_value "${hash_key}")"
  [[ -n "${relative}" && -n "${expected}" ]] || \
    die "${path_key}/${hash_key} is absent from source-lock.yaml"
  [[ "${relative}" != /* && "${relative}" != *'..'* ]] || \
    die "locked validator path is outside the repository: ${relative}"
  [[ -f "${CVD_REPO_ROOT}/${relative}" && ! -L "${CVD_REPO_ROOT}/${relative}" ]] || \
    die "locked validator is missing or is a symlink: ${relative}"
  actual="$(sha256sum "${CVD_REPO_ROOT}/${relative}" | awk '{print $1}')"
  [[ "${actual}" == "${expected}" ]] || \
    die "locked validator checksum changed: ${relative}"
}

verify_validator_lock() {
  [[ -f "${CVD_SOURCE_LOCK}" && ! -L "${CVD_SOURCE_LOCK}" ]] || \
    die "source-lock.yaml is missing or is a symlink"
  verify_locked_file validateLocalNimsPath validateLocalNimsSha256
  verify_locked_file validateDocumentRagPath validateDocumentRagSha256
  verify_locked_file probeEmbeddingPath probeEmbeddingSha256
}

verify_insightengine_release() {
  local expected actual
  expected="$(lock_value insightEngineVersion)"
  [[ -n "${expected}" ]] || die "insightEngineVersion is absent from source-lock.yaml"
  [[ -f "${CVD_INSIGHTENGINE_JSON_CHECK}" && ! -L "${CVD_INSIGHTENGINE_JSON_CHECK}" ]] || \
    die "InsightEngine metadata checker is missing or is a symlink"
  actual="$(helm -n "${CVD_INSIGHTENGINE_NAMESPACE}" get metadata \
    "${CVD_BACKEND_RELEASE}" -o json | \
    python3 "${CVD_INSIGHTENGINE_JSON_CHECK}" helm-chart-version)"
  [[ "${actual}" == "${expected}" ]] || \
    die "backend chart version ${actual} does not match locked InsightEngine ${expected}"
  printf 'InsightEngine backend chart version: %s\n' "${actual}"
}

validator_path() {
  local key="$1" relative
  relative="$(lock_value "${key}")"
  [[ -n "${relative}" ]] || die "validator path key is absent: ${key}"
  printf '%s/%s\n' "${CVD_REPO_ROOT}" "${relative}"
}

verify_context() {
  local identity server context
  identity="$(oc whoami)"
  server="$(oc whoami --show-server)"
  context="$(oc config current-context)"
  printf 'OpenShift identity: %s\n' "${identity}"
  printf 'OpenShift API: %s\n' "${server}"
  printf 'Current context: %s\n' "${context}"
  [[ "${identity}" == "${CVD_EXPECTED_USER}" ]] || \
    die "OpenShift identity does not match CVD_EXPECTED_USER"
  [[ "${server}" == "${CVD_EXPECTED_API}" ]] || \
    die "OpenShift API does not match CVD_EXPECTED_API"
  [[ "${context}" == "${CVD_EXPECTED_CONTEXT}" ]] || \
    die "OpenShift context does not match CVD_EXPECTED_CONTEXT"
}

verify_locked_model_profile() {
  [[ "${CVD_NIM_NAMESPACE}" == "nims" ]] || \
    die "the locked model validator requires CVD_NIM_NAMESPACE=nims"
  [[ "${CVD_EMBEDDING_NIMSERVICE}" == "embedding" ]] || \
    die "the locked model validator requires CVD_EMBEDDING_NIMSERVICE=embedding"
  [[ "${CVD_LLM_NIMSERVICE}" == "llm-nemotron-35-lightning" ]] || \
    die "the locked model validator requires the documented LLM NIMService"
  [[ "${CVD_RERANKER_NIMSERVICE}" == "reranker" ]] || \
    die "the locked model validator requires CVD_RERANKER_NIMSERVICE=reranker"
  [[ "${CVD_EMBEDDING_BASE_URL}" == "http://embedding.nims.svc:8029" ]] || \
    die "the locked model validator requires the documented embedding Service origin"
  [[ "${CVD_LLM_BASE_URL}" == "http://llm-nemotron-35-lightning.nims.svc:8025" ]] || \
    die "the locked model validator requires the documented LLM Service origin"
  [[ "${CVD_RERANKER_BASE_URL}" == "http://reranker.nims.svc:8028" ]] || \
    die "the locked model validator requires the documented reranker Service origin"
  [[ "${CVD_EMBEDDING_MODEL}" == "nvidia/llama-nemotron-embed-1b-v2" ]] || \
    die "the locked model validator requires the documented embedding model"
  [[ "${CVD_LLM_MODEL}" == "nvidia/nemotron-3.5-lightning-30b-a3b" ]] || \
    die "the locked model validator requires the documented LLM"
  [[ "${CVD_RERANKER_MODEL}" == "nvidia/llama-3.2-nv-rerankqa-1b-v2" ]] || \
    die "the locked model validator requires the documented reranker"
  [[ "${CVD_EMBEDDING_DIMENSIONS}" == "2048" ]] || \
    die "the locked embedding probes require 2048 dimensions"
}

verify_ready_condition() {
  local namespace="$1" resource="$2" name="$3" condition
  condition="$(oc -n "${namespace}" get "${resource}/${name}" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')"
  [[ "${condition}" == "True" ]] || die "${resource}/${name} is not Ready"
}
