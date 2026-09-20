#!/usr/bin/env bash

set -euo pipefail
{ set +x; } 2>/dev/null

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INSIGHTENGINE_CVD_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
JSON_CHECK="${SCRIPT_DIR}/json-check.py"
MANIFEST_CHECK="${SCRIPT_DIR}/manifest-check.py"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is not installed: $1"
}

require_value() {
  local name=$1
  [[ -n "${!name:-}" ]] || die "required value is empty: ${name}"
  [[ "${!name}" != *"<"* ]] || die "replace placeholder value: ${name}"
}

reject_example_value() {
  local name=$1
  case "${!name}" in
    *example.com*|*'<cluster>'*|*'<namespace>'*|*'<version>'*)
      die "replace example value: ${name}"
      ;;
  esac
}

has_flag() {
  local wanted=$1
  shift
  local argument
  for argument in "$@"; do
    [[ "${argument}" == "${wanted}" ]] && return 0
  done
  return 1
}

has_apply_flag() {
  has_flag --apply "$@"
}

env_path_from_args() {
  local previous="" argument
  for argument in "$@"; do
    if [[ "${previous}" == "--env" ]]; then
      printf '%s\n' "${argument}"
      return 0
    fi
    previous=${argument}
  done
  printf '%s\n' "${INSIGHTENGINE_CVD_DIR}/release-inputs.env"
}

output_dir_from_args() {
  local previous="" argument
  for argument in "$@"; do
    if [[ "${previous}" == "--output-dir" ]]; then
      printf '%s\n' "${argument}"
      return 0
    fi
    previous=${argument}
  done
  printf '%s\n' "${INSIGHTENGINE_CVD_DIR}/rendered"
}

load_inputs() {
  local env_file=$1
  [[ -f "${env_file}" && ! -L "${env_file}" ]] || \
    die "input file must be a regular non-symlink file: ${env_file}"
  set -a
  # shellcheck disable=SC1090
  source "${env_file}"
  { set +x; } 2>/dev/null
  set +a
  # Treat the site file as trusted configuration, but do not allow it to leave
  # safety or tracing options changed for the deployment scripts that follow.
  set -euo pipefail
}

validate_namespace() {
  local namespace_name=$1
  [[ ${#namespace_name} -le 63 && "${namespace_name}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || \
    die "invalid Kubernetes namespace: ${namespace_name}"
  case "${namespace_name}" in
    default|kube-*|openshift-*|zarf|zot|vast-permissions)
      die "refusing unsafe InsightEngine namespace: ${namespace_name}"
      ;;
  esac
}

print_context() {
  printf 'OpenShift identity: %s\n' "$(oc whoami)"
  printf 'OpenShift API: %s\n' "$(oc whoami --show-server)"
  printf 'Current kubeconfig context: %s\n' "$(oc config current-context)"
}

verify_context() {
  require_value EXPECTED_OCP_API
  require_value EXPECTED_OCP_CONTEXT
  require_value EXPECTED_OCP_USER
  local actual_api actual_context actual_user
  actual_api=$(oc whoami --show-server)
  actual_context=$(oc config current-context)
  actual_user=$(oc whoami)
  [[ "${actual_api}" == "${EXPECTED_OCP_API}" ]] || \
    die "OpenShift API mismatch: expected ${EXPECTED_OCP_API}; got ${actual_api}"
  [[ "${actual_context}" == "${EXPECTED_OCP_CONTEXT}" ]] || \
    die "OpenShift context mismatch: expected ${EXPECTED_OCP_CONTEXT}; got ${actual_context}"
  [[ "${actual_user}" == "${EXPECTED_OCP_USER}" ]] || \
    die "OpenShift identity mismatch: expected ${EXPECTED_OCP_USER}; got ${actual_user}"
}

bundle_path() {
  local relative_path=$1
  printf '%s/%s\n' "${INSIGHTENGINE_BUNDLE_DIR%/}" "${relative_path}"
}

chart_path() {
  local relative_path=$1
  python3 - "${INSIGHTENGINE_BUNDLE_DIR}" "${relative_path}" <<'PY' || \
    die "release chart must be a regular non-symlink file canonically beneath INSIGHTENGINE_BUNDLE_DIR: ${relative_path}"
import os
import stat
import sys

bundle_input, relative = sys.argv[1:]
if not os.path.isdir(bundle_input):
    raise SystemExit(1)
bundle = os.path.realpath(bundle_input)
if os.path.isabs(relative) or not relative:
    raise SystemExit(1)
parts = relative.split(os.sep)
if any(part in ("", ".", "..") for part in parts):
    raise SystemExit(1)
if any(any(ord(character) < 32 for character in part) for part in parts):
    raise SystemExit(1)
candidate = bundle
try:
    for part in parts:
        candidate = os.path.join(candidate, part)
        metadata = os.lstat(candidate)
        if stat.S_ISLNK(metadata.st_mode):
            raise SystemExit(1)
except OSError:
    raise SystemExit(1)
if os.path.commonpath((bundle, os.path.realpath(candidate))) != bundle:
    raise SystemExit(1)
if not stat.S_ISREG(os.stat(candidate).st_mode):
    raise SystemExit(1)
print(os.path.realpath(candidate))
PY
}

verify_delivery() {
  require_value INSIGHTENGINE_BUNDLE_DIR
  [[ -d "${INSIGHTENGINE_BUNDLE_DIR}" ]] || \
    die "delivery directory not found: ${INSIGHTENGINE_BUNDLE_DIR}"
  [[ -f "$(bundle_path README.md)" ]] || die "delivery README is missing"
  [[ -f "$(bundle_path SHA256SUMS)" ]] || die "delivery SHA256SUMS is missing"
  [[ -f "$(bundle_path tools/verify-checksums.py)" ]] || \
    die "delivery checksum verifier is missing"
  local crd_file
  for crd_file in \
    ingestionpipeline.yaml vmsuser.yaml vmsgroup.yaml vmss3policy.yaml \
    vmsview.yaml vmsvdbview.yaml vmskafkabroker.yaml vmskafkatopic.yaml; do
    [[ -f "$(bundle_path "crds/${crd_file}")" ]] || \
      die "required delivery CRD is missing: crds/${crd_file}"
  done
  (
    cd "${INSIGHTENGINE_BUNDLE_DIR}"
    python3 tools/verify-checksums.py
  )
  [[ "${EXPECTED_IE_VERSION}" == "5.4.3" ]] || \
    die "this companion validates InsightEngine 5.4.3 only; use the selected release runbook for ${EXPECTED_IE_VERSION}"
}

require_inputs() {
  local name
  for name in \
    EXPECTED_OCP_API EXPECTED_OCP_CONTEXT EXPECTED_OCP_USER \
    INSIGHTENGINE_BUNDLE_DIR EXPECTED_IE_VERSION \
    INSIGHTENGINE_NAMESPACE \
    PIPELINE_NAME PIPELINE_TOPIC_NAME PIPELINE_BROKER_NAME \
    EXPECTED_VMS_BROKER_CR_NAME EXPECTED_VAST_BROKER_NAME \
    EXPECTED_VMS_USER_CR_NAME EXPECTED_VMS_GROUP_CR_NAME \
    EXPECTED_END_USER_POLICY_CR_NAME EXPECTED_S3_VIEW_CR_NAME EXPECTED_VDB_VIEW_CR_NAME \
    POSTGRES_RELEASE OPERATOR_RELEASE BACKEND_RELEASE \
    POSTGRES_CHART OPERATOR_CHART BACKEND_CHART POSTGRES_VALUES \
    OPERATOR_VALUES BACKEND_VALUES POSTGRES_SERVICE_NAME POSTGRES_SERVICE_HOST \
    S3_VIEW_POLICY_NAME END_USER_POLICY_NAME EXPECTED_APP_MANAGER_USERNAME \
    REGISTRY_TARGET INGEST_REGISTRY_NAME INGEST_K8S_CLUSTER_NAME \
    INGEST_IMAGE_REPOSITORY INGEST_IMAGE_TAG INGEST_REGISTRY_AUTH_TYPE \
    EXPECTED_EMBEDDING_MODEL EXPECTED_EMBEDDING_DIMENSIONS \
    STORAGE_CLASS EXPECTED_CSI_PROVISIONER \
    RUNTIME_SECRET_NAME POSTGRES_SECRET_NAME APP_PULL_SECRET_NAME \
    RUNTIME_SECRET_REQUIRED_KEYS POSTGRES_SECRET_REQUIRED_KEYS DATAENGINE_NAMESPACE \
    KNATIVE_SERVING_NAMESPACE KNATIVE_EVENTING_NAMESPACE \
    PROVISIONING_MODE; do
    require_value "${name}"
  done
  validate_namespace "${INSIGHTENGINE_NAMESPACE}"
  reject_example_value REGISTRY_TARGET
  [[ "${PROVISIONING_MODE}" == "vendor-helper" || "${PROVISIONING_MODE}" == "preprovisioned" ]] || \
    die "PROVISIONING_MODE must be vendor-helper or preprovisioned"
  [[ "${INGEST_REGISTRY_AUTH_TYPE}" == "secret" || \
     "${INGEST_REGISTRY_AUTH_TYPE}" == "password" || \
     "${INGEST_REGISTRY_AUTH_TYPE}" == "none" ]] || \
    die "INGEST_REGISTRY_AUTH_TYPE must be secret, password, or none"
  if [[ "${INGEST_REGISTRY_AUTH_TYPE}" == "secret" ]]; then
    require_value INGEST_PULL_SECRET_NAME
  fi
  [[ "${PIPELINE_BROKER_NAME}" == "${EXPECTED_VAST_BROKER_NAME}" ]] || \
    die "PIPELINE_BROKER_NAME must match VMSKafkaBroker.spec.brokerName"
  [[ "${EXPECTED_EMBEDDING_DIMENSIONS}" =~ ^[1-9][0-9]*$ ]] || \
    die "EXPECTED_EMBEDDING_DIMENSIONS must be a positive integer"
  [[ ! "${INGEST_IMAGE_TAG}" =~ ^[Ll][Aa][Tt][Ee][Ss][Tt]$ ]] || \
    die "INGEST_IMAGE_TAG must be an explicit release tag, never latest"
  [[ -x "${JSON_CHECK}" ]] || die "JSON assertion helper is not executable: ${JSON_CHECK}"
  [[ -x "${MANIFEST_CHECK}" ]] || die "manifest assertion helper is not executable: ${MANIFEST_CHECK}"
}

require_site_files() {
  local file_path
  for file_path in "${POSTGRES_VALUES}" "${OPERATOR_VALUES}" "${BACKEND_VALUES}"; do
    [[ -f "${file_path}" ]] || die "reviewed values file not found: ${file_path}"
  done
  for file_path in "${POSTGRES_CHART}" "${OPERATOR_CHART}" "${BACKEND_CHART}"; do
    chart_path "${file_path}" >/dev/null
  done
}

assert_no_latest_images() {
  local manifest_file=$1
  if grep -Eiq "image:[[:space:]]*[^[:space:]]*:latest([[:space:]\"']|$)" "${manifest_file}"; then
    die "rendered manifest contains an unpinned latest image: ${manifest_file}"
  fi
  local image reference
  while IFS= read -r image; do
    image=${image#image:}
    image=${image#${image%%[![:space:]]*}}
    image=${image%\"}; image=${image#\"}
    image=${image%\'}; image=${image#\'}
    [[ -n "${image}" ]] || continue
    reference=${image##*/}
    [[ "${image}" == *@sha256:* || "${reference}" == *:* ]] || \
      die "rendered manifest contains an untagged image: ${image}"
  done < <(grep -E '^[[:space:]]*image:[[:space:]]*' "${manifest_file}" | sed -E 's/^[[:space:]]*//')
}

assert_no_inline_secrets() {
  local manifest_file=$1
  if grep -Eq '^kind:[[:space:]]*Secret[[:space:]]*$' "${manifest_file}"; then
    die "rendered chart creates a Secret; use approved existing Secret references: ${manifest_file}"
  fi
}

chart_version() {
  helm show chart "$(chart_path "$1")" | awk '/^version:[[:space:]]/ {print $2; exit}'
}

expected_ingest_image() {
  printf '%s/%s:%s\n' \
    "${REGISTRY_TARGET%/}" "${INGEST_IMAGE_REPOSITORY#/}" "${INGEST_IMAGE_TAG}"
}

assert_contains_literal() {
  local manifest_file=$1 literal=$2 description=$3
  grep -Fq -- "${literal}" "${manifest_file}" || \
    die "rendered manifest does not contain ${description}: ${literal}"
}

assert_contains_pattern() {
  local manifest_file=$1 pattern=$2 description=$3
  grep -Eq -- "${pattern}" "${manifest_file}" || \
    die "rendered manifest does not contain ${description}"
}

write_manifest_image_inventory() {
  local output_file=$1
  shift
  : > "${output_file}"
  local manifest_file image
  for manifest_file in "$@"; do
    while IFS= read -r image; do
      image=${image#image:}
      image=${image#${image%%[![:space:]]*}}
      image=${image%\"}; image=${image#\"}
      image=${image%\'}; image=${image#\'}
      [[ -n "${image}" ]] || continue
      printf '%s\n' "${image}" >> "${output_file}"
    done < <(grep -E '^[[:space:]]*image:[[:space:]]*' "${manifest_file}" | sed -E 's/^[[:space:]]*//')
  done
  sort -u -o "${output_file}" "${output_file}"
  [[ -s "${output_file}" ]] || die "rendered manifests contain no workload images"
}

assert_v54_render_contract() {
  local postgres_manifest=$1 operator_manifest=$2 backend_manifest=$3
  assert_contains_literal "${postgres_manifest}" "${POSTGRES_SERVICE_NAME}" \
    "the expected PostgreSQL Service name"
  assert_contains_literal "${postgres_manifest}" "${POSTGRES_SECRET_NAME}" \
    "the existing PostgreSQL authentication Secret"
  assert_contains_literal "${postgres_manifest}" "${APP_PULL_SECRET_NAME}" \
    "the application image-pull Secret"
  assert_contains_literal "${operator_manifest}" "${APP_PULL_SECRET_NAME}" \
    "the application image-pull Secret"
  assert_contains_literal "${backend_manifest}" "name: v54-secret-shim" \
    "the v5.4 Secret shim (v54.enabled must be true)"
  assert_contains_literal "${backend_manifest}" "enable_impersonation: false" \
    "the validated v5.4 impersonation setting"
  assert_contains_literal "${backend_manifest}" "withMedia: false" \
    "document-only ingestion"
  assert_contains_literal "${backend_manifest}" "${POSTGRES_SERVICE_HOST}" \
    "the exact PostgreSQL Service host"
  assert_contains_literal "${backend_manifest}" "${S3_VIEW_POLICY_NAME}" \
    "the approved S3 view policy"
  assert_contains_literal "${backend_manifest}" "${PIPELINE_NAME}" \
    "the expected pipeline name"
  assert_contains_literal "${backend_manifest}" "${PIPELINE_TOPIC_NAME}" \
    "the dedicated pipeline topic"
  assert_contains_literal "${backend_manifest}" "${PIPELINE_BROKER_NAME}" \
    "the existing VAST Event Broker name"
  assert_contains_literal "${backend_manifest}" "${INGEST_REGISTRY_NAME}" \
    "the selected DataEngine container registry"
  assert_contains_literal "${backend_manifest}" "${INGEST_K8S_CLUSTER_NAME}" \
    "the selected DataEngine Kubernetes cluster"
  assert_contains_literal "${backend_manifest}" "${INGEST_IMAGE_REPOSITORY}" \
    "the pinned ingestion image repository"
  assert_contains_literal "${backend_manifest}" "${INGEST_IMAGE_TAG}" \
    "the pinned ingestion image tag"
  assert_contains_literal "${backend_manifest}" "${EXPECTED_EMBEDDING_MODEL}" \
    "the reviewed embedding model"
  assert_contains_pattern "${backend_manifest}" \
    "dimensions:[[:space:]]*[\"']?${EXPECTED_EMBEDDING_DIMENSIONS}([\"']?[[:space:]]*$|[,}])" \
    "the reviewed embedding dimension"
  assert_contains_literal "${backend_manifest}" "${RUNTIME_SECRET_NAME}" \
    "the existing runtime Secret"
  assert_contains_literal "${backend_manifest}" "${APP_PULL_SECRET_NAME}" \
    "the application image-pull Secret"
  python3 "${MANIFEST_CHECK}" \
      --name "${PIPELINE_NAME}" \
      --topic "${PIPELINE_TOPIC_NAME}" \
      --broker-name "${PIPELINE_BROKER_NAME}" \
      --registry "${INGEST_REGISTRY_NAME}" \
      --cluster "${INGEST_K8S_CLUSTER_NAME}" \
      --image-repository "${INGEST_IMAGE_REPOSITORY}" \
      --image-tag "${INGEST_IMAGE_TAG}" < "${backend_manifest}" || \
    die "rendered IngestionPipeline does not match the reviewed topic, broker, registry, cluster, and pinned image"
}
