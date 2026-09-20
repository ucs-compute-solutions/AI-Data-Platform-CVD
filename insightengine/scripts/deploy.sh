#!/usr/bin/env bash

set -euo pipefail
{ set +x; } 2>/dev/null
umask 077
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE=$(env_path_from_args "$@")
load_inputs "${ENV_FILE}"
for command_name in oc helm python3 grep sed awk sha256sum mkdir chmod mktemp mv rm rmdir cmp sort; do
  require_command "${command_name}"
done
require_inputs
print_context
verify_context
verify_delivery
require_site_files
oc get namespace "${INSIGHTENGINE_NAMESPACE}" >/dev/null || \
  die "namespace/${INSIGHTENGINE_NAMESPACE} is absent; run prepare-namespace.sh first"

[[ "${VAST_MAPPING_CONFIRMED:-false}" == "true" ]] || \
  die "VAST_MAPPING_CONFIRMED is not true; confirm compute-cluster and registry associations in VAST"
[[ "${VAST_PREREQUISITES_CONFIRMED:-false}" == "true" ]] || \
  die "VAST_PREREQUISITES_CONFIRMED is not true; complete the selected VAST provisioning path"
[[ "${MANAGER_CREDENTIAL_MATCH_CONFIRMED:-false}" == "true" ]] || \
  die "MANAGER_CREDENTIAL_MATCH_CONFIRMED is not true; match runtime manager credentials to the provisioning inputs"
[[ "${TRUSTED_CA_CONFIRMED:-false}" == "true" ]] || \
  die "TRUSTED_CA_CONFIRMED is not true; establish the required trust chains without bypassing TLS"

for secret_name in \
  "${RUNTIME_SECRET_NAME}" \
  "${POSTGRES_SECRET_NAME}" \
  "${APP_PULL_SECRET_NAME}"; do
  oc -n "${INSIGHTENGINE_NAMESPACE}" get secret "${secret_name}" >/dev/null || \
    die "required Secret is absent: ${INSIGHTENGINE_NAMESPACE}/${secret_name}"
done
if [[ "${INGEST_REGISTRY_AUTH_TYPE}" == "secret" ]]; then
  oc -n "${INSIGHTENGINE_NAMESPACE}" get secret "${INGEST_PULL_SECRET_NAME}" >/dev/null || \
    die "secret-backed DataEngine registry requires ${INSIGHTENGINE_NAMESPACE}/${INGEST_PULL_SECRET_NAME}"
fi

APPLY_MODE=false
has_apply_flag "$@" && APPLY_MODE=true
REVIEW_FILES=(
  postgres.yaml
  operator.yaml
  backend.yaml
  approved-images.txt
  deployment-intent.txt
  review-lock.sha256
)

prepare_output_directory() {
  local requested=$1 apply_mode=$2 canonical entry name review_file
  canonical=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${requested}")
  case "${canonical}" in
    /|/tmp|/private/tmp|"${HOME:-/nonexistent}"|"${INSIGHTENGINE_CVD_DIR}"|"${SCRIPT_DIR}"|"$(pwd)")
      die "refusing broad rendered-output directory: ${canonical}"
      ;;
  esac
  [[ ! -L "${requested}" ]] || die "rendered-output directory must not be a symbolic link: ${requested}"
  if [[ ! -e "${requested}" ]]; then
    [[ "${apply_mode}" == "false" ]] || \
      die "review directory is absent; run deploy.sh without --apply and review its artifacts first: ${requested}"
    mkdir -p -- "${requested}"
    chmod 0700 "${requested}"
  fi
  [[ -d "${requested}" ]] || die "rendered-output path is not a directory: ${requested}"
  python3 -c 'import os,stat,sys; raise SystemExit(0 if stat.S_IMODE(os.stat(sys.argv[1]).st_mode) & 0o077 == 0 else 1)' \
    "${requested}" || die "rendered-output directory must not grant group or other access: ${requested}"
  for entry in "${requested}"/* "${requested}"/.[!.]* "${requested}"/..?*; do
    [[ -e "${entry}" || -L "${entry}" ]] || continue
    name=${entry##*/}
    case "${name}" in
      postgres.yaml|operator.yaml|backend.yaml|approved-images.txt|deployment-intent.txt|review-lock.sha256)
        [[ -f "${entry}" && ! -L "${entry}" ]] || \
          die "rendered-output target must be a regular non-symlink file: ${entry}"
        [[ "${apply_mode}" == "true" ]] || \
          die "review directory is not empty; choose a new directory or explicitly archive the old review set: ${canonical}"
        ;;
      *) die "rendered-output directory contains an unrelated entry: ${entry}" ;;
    esac
  done
  if [[ "${apply_mode}" == "true" ]]; then
    for review_file in "${REVIEW_FILES[@]}"; do
      [[ -f "${canonical}/${review_file}" && ! -L "${canonical}/${review_file}" ]] || \
        die "review artifact is absent or unsafe: ${canonical}/${review_file}"
    done
  fi
  printf '%s\n' "${canonical}"
}

regular_input_path() {
  local requested=$1 role=$2 canonical
  [[ -f "${requested}" && ! -L "${requested}" ]] || \
    die "${role} must be a regular non-symlink file: ${requested}"
  canonical=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${requested}")
  [[ -f "${canonical}" ]] || die "${role} does not resolve to a regular file: ${requested}"
  printf '%s\n' "${canonical}"
}

OUTPUT_DIR=$(prepare_output_directory "$(output_dir_from_args "$@")" "${APPLY_MODE}")
ENV_FILE_PATH=$(regular_input_path "${ENV_FILE}" "release input file")
POSTGRES_CHART_PATH=$(chart_path "${POSTGRES_CHART}")
OPERATOR_CHART_PATH=$(chart_path "${OPERATOR_CHART}")
BACKEND_CHART_PATH=$(chart_path "${BACKEND_CHART}")
POSTGRES_VALUES_PATH=$(regular_input_path "${POSTGRES_VALUES}" "PostgreSQL values file")
OPERATOR_VALUES_PATH=$(regular_input_path "${OPERATOR_VALUES}" "operator values file")
BACKEND_VALUES_PATH=$(regular_input_path "${BACKEND_VALUES}" "backend values file")

render_chart() {
  local release=$1 chart_path_value=$2 values_path=$3 output=$4
  helm lint "${chart_path_value}" -f "${values_path}"
  helm template "${release}" "${chart_path_value}" \
    -n "${INSIGHTENGINE_NAMESPACE}" -f "${values_path}" > "${output}"
  assert_no_latest_images "${output}"
  assert_no_inline_secrets "${output}"
}

file_sha256() {
  local file_path=$1 digest remainder
  read -r digest remainder < <(sha256sum -- "${file_path}")
  [[ "${digest}" =~ ^[0-9a-fA-F]{64}$ ]] || die "failed to hash reviewed file: ${file_path}"
  printf '%s\n' "${digest}"
}

write_hash_record() {
  local file_path=$1 label=$2 digest
  digest=$(file_sha256 "${file_path}") || die "failed to lock reviewed file: ${file_path}"
  printf '%s  %s\n' "${digest}" "${label}"
}

write_deployment_intent() {
  local output=$1 helm_version
  helm_version=$(helm version --short) || die "unable to determine the Helm client version"
  {
    printf 'schema=insightengine-review-v1\n'
    printf 'ocp_api=%s\n' "${EXPECTED_OCP_API}"
    printf 'ocp_context=%s\n' "${EXPECTED_OCP_CONTEXT}"
    printf 'ocp_user=%s\n' "${EXPECTED_OCP_USER}"
    printf 'insightengine_version=%s\n' "${EXPECTED_IE_VERSION}"
    printf 'namespace=%s\n' "${INSIGHTENGINE_NAMESPACE}"
    printf 'postgres_release=%s\n' "${POSTGRES_RELEASE}"
    printf 'operator_release=%s\n' "${OPERATOR_RELEASE}"
    printf 'backend_release=%s\n' "${BACKEND_RELEASE}"
    printf 'postgres_chart=%s\n' "${POSTGRES_CHART_PATH}"
    printf 'operator_chart=%s\n' "${OPERATOR_CHART_PATH}"
    printf 'backend_chart=%s\n' "${BACKEND_CHART_PATH}"
    printf 'postgres_values=%s\n' "${POSTGRES_VALUES_PATH}"
    printf 'operator_values=%s\n' "${OPERATOR_VALUES_PATH}"
    printf 'backend_values=%s\n' "${BACKEND_VALUES_PATH}"
    printf 'helm_version=%s\n' "${helm_version}"
  } > "${output}"
}

write_review_lock() {
  local artifact_dir=$1 output=$2
  {
    printf '# InsightEngine deployment review lock v1\n'
    write_hash_record "${ENV_FILE_PATH}" 'input/release-inputs.env'
    write_hash_record "${POSTGRES_CHART_PATH}" 'chart/postgresql'
    write_hash_record "${OPERATOR_CHART_PATH}" 'chart/operator'
    write_hash_record "${BACKEND_CHART_PATH}" 'chart/backend'
    write_hash_record "${POSTGRES_VALUES_PATH}" 'values/postgresql'
    write_hash_record "${OPERATOR_VALUES_PATH}" 'values/operator'
    write_hash_record "${BACKEND_VALUES_PATH}" 'values/backend'
    write_hash_record "${artifact_dir}/postgres.yaml" 'artifact/postgres.yaml'
    write_hash_record "${artifact_dir}/operator.yaml" 'artifact/operator.yaml'
    write_hash_record "${artifact_dir}/backend.yaml" 'artifact/backend.yaml'
    write_hash_record "${artifact_dir}/approved-images.txt" 'artifact/approved-images.txt'
    write_hash_record "${artifact_dir}/deployment-intent.txt" 'artifact/deployment-intent.txt'
  } > "${output}"
}

render_review_set() {
  local artifact_dir=$1
  render_chart "${POSTGRES_RELEASE}" "${POSTGRES_CHART_PATH}" \
    "${POSTGRES_VALUES_PATH}" "${artifact_dir}/postgres.yaml"
  render_chart "${OPERATOR_RELEASE}" "${OPERATOR_CHART_PATH}" \
    "${OPERATOR_VALUES_PATH}" "${artifact_dir}/operator.yaml"
  render_chart "${BACKEND_RELEASE}" "${BACKEND_CHART_PATH}" \
    "${BACKEND_VALUES_PATH}" "${artifact_dir}/backend.yaml"
  assert_v54_render_contract \
    "${artifact_dir}/postgres.yaml" "${artifact_dir}/operator.yaml" "${artifact_dir}/backend.yaml"
  write_manifest_image_inventory "${artifact_dir}/approved-images.txt" \
    "${artifact_dir}/postgres.yaml" "${artifact_dir}/operator.yaml" "${artifact_dir}/backend.yaml"
  write_deployment_intent "${artifact_dir}/deployment-intent.txt"
  chmod 0600 "${artifact_dir}/postgres.yaml" "${artifact_dir}/operator.yaml" \
    "${artifact_dir}/backend.yaml" "${artifact_dir}/approved-images.txt" \
    "${artifact_dir}/deployment-intent.txt"
}

assert_review_lock_matches() {
  local scratch_dir=$1 candidate_lock
  candidate_lock="${scratch_dir}/review-lock.current.sha256"
  write_review_lock "${OUTPUT_DIR}" "${candidate_lock}"
  cmp -s "${OUTPUT_DIR}/review-lock.sha256" "${candidate_lock}" || \
    die "review lock mismatch; generate and review a new output directory before applying"
}

verify_locked_review() {
  local scratch_dir=$1 current_render artifact
  assert_review_lock_matches "${scratch_dir}"

  write_deployment_intent "${scratch_dir}/deployment-intent.txt"
  cmp -s "${OUTPUT_DIR}/deployment-intent.txt" "${scratch_dir}/deployment-intent.txt" || \
    die "deployment intent changed after review; generate and review a new output directory"

  current_render="${scratch_dir}/current-render"
  mkdir -p -- "${current_render}"
  render_review_set "${current_render}"
  for artifact in postgres.yaml operator.yaml backend.yaml approved-images.txt; do
    cmp -s "${OUTPUT_DIR}/${artifact}" "${current_render}/${artifact}" || \
      die "current render differs from reviewed ${artifact}; generate and review a new output directory"
  done
}

if [[ "${APPLY_MODE}" == "false" ]]; then
  STAGING_DIR=$(mktemp -d "${OUTPUT_DIR}/.review-staging.XXXXXX")
  cleanup_staging() { rm -rf -- "${STAGING_DIR}"; }
  trap cleanup_staging EXIT
  render_review_set "${STAGING_DIR}"
  write_review_lock "${STAGING_DIR}" "${STAGING_DIR}/review-lock.sha256"
  chmod 0600 "${STAGING_DIR}/review-lock.sha256"
  for artifact in postgres.yaml operator.yaml backend.yaml approved-images.txt deployment-intent.txt; do
    mv -- "${STAGING_DIR}/${artifact}" "${OUTPUT_DIR}/${artifact}"
  done
  mv -- "${STAGING_DIR}/review-lock.sha256" "${OUTPUT_DIR}/review-lock.sha256"
  rmdir -- "${STAGING_DIR}"
  trap - EXIT
else
  VERIFY_DIR=$(mktemp -d)
  cleanup_verify() { rm -rf -- "${VERIFY_DIR}"; }
  trap cleanup_verify EXIT
  verify_locked_review "${VERIFY_DIR}"
fi

cat <<PLAN

Planned Helm sequence in namespace/${INSIGHTENGINE_NAMESPACE}
  1. ${POSTGRES_RELEASE} from ${POSTGRES_CHART}
  2. ${OPERATOR_RELEASE} from ${OPERATOR_CHART}
  3. ${BACKEND_RELEASE} from ${BACKEND_CHART}

All charts were rendered with the reviewed site-values files and contain no
unqualified latest image reference.
Rendered manifests for exact review:
  ${OUTPUT_DIR}/postgres.yaml
  ${OUTPUT_DIR}/operator.yaml
  ${OUTPUT_DIR}/backend.yaml
  ${OUTPUT_DIR}/approved-images.txt
  ${OUTPUT_DIR}/deployment-intent.txt
  ${OUTPUT_DIR}/review-lock.sha256

Commands that --apply will run after server-side dry runs:
PLAN
printf '  helm upgrade --install %q %q -n %q -f %q --dry-run=server --hide-secret >/dev/null\n' \
  "${POSTGRES_RELEASE}" "${POSTGRES_CHART_PATH}" "${INSIGHTENGINE_NAMESPACE}" "${POSTGRES_VALUES_PATH}"
printf '  helm upgrade --install %q %q -n %q -f %q --wait --timeout 15m\n' \
  "${POSTGRES_RELEASE}" "${POSTGRES_CHART_PATH}" "${INSIGHTENGINE_NAMESPACE}" "${POSTGRES_VALUES_PATH}"
printf '  helm upgrade --install %q %q -n %q -f %q --dry-run=server --hide-secret >/dev/null\n' \
  "${OPERATOR_RELEASE}" "${OPERATOR_CHART_PATH}" "${INSIGHTENGINE_NAMESPACE}" "${OPERATOR_VALUES_PATH}"
printf '  helm upgrade --install %q %q -n %q -f %q --wait --timeout 15m\n' \
  "${OPERATOR_RELEASE}" "${OPERATOR_CHART_PATH}" "${INSIGHTENGINE_NAMESPACE}" "${OPERATOR_VALUES_PATH}"
printf '  helm upgrade --install %q %q -n %q -f %q --dry-run=server --hide-secret >/dev/null\n' \
  "${BACKEND_RELEASE}" "${BACKEND_CHART_PATH}" "${INSIGHTENGINE_NAMESPACE}" "${BACKEND_VALUES_PATH}"
printf '  helm upgrade --install %q %q -n %q -f %q --wait --timeout 15m\n' \
  "${BACKEND_RELEASE}" "${BACKEND_CHART_PATH}" "${INSIGHTENGINE_NAMESPACE}" "${BACKEND_VALUES_PATH}"

if [[ "${APPLY_MODE}" == "false" ]]; then
  printf '\nREVIEW SET CREATED: no Helm release changed. Inspect every artifact, then re-run with --apply against this unchanged directory.\n'
  exit 0
fi

assert_release_version_compatible() {
  local release=$1 chart=$2 expected installed
  expected=$(chart_version "${chart}")
  if helm -n "${INSIGHTENGINE_NAMESPACE}" status "${release}" -o json >/dev/null 2>&1; then
    installed=$(helm -n "${INSIGHTENGINE_NAMESPACE}" get metadata "${release}" -o json | \
      python3 "${JSON_CHECK}" helm-chart-version)
    [[ -n "${installed}" && "${installed}" == "${expected}" ]] || \
      die "release/${release} is an upgrade (${installed:-unknown} -> ${expected}); use the release-specific upgrade procedure so CRDs and rollback are reviewed"
  fi
}

assert_release_version_compatible "${POSTGRES_RELEASE}" "${POSTGRES_CHART}"
assert_release_version_compatible "${OPERATOR_RELEASE}" "${OPERATOR_CHART}"
assert_release_version_compatible "${BACKEND_RELEASE}" "${BACKEND_CHART}"

helm upgrade --install "${POSTGRES_RELEASE}" "${POSTGRES_CHART_PATH}" \
  -n "${INSIGHTENGINE_NAMESPACE}" -f "${POSTGRES_VALUES_PATH}" \
  --dry-run=server --hide-secret >/dev/null
assert_review_lock_matches "${VERIFY_DIR}"
helm upgrade --install "${POSTGRES_RELEASE}" "${POSTGRES_CHART_PATH}" \
  -n "${INSIGHTENGINE_NAMESPACE}" -f "${POSTGRES_VALUES_PATH}" \
  --wait --timeout 15m
helm upgrade --install "${OPERATOR_RELEASE}" "${OPERATOR_CHART_PATH}" \
  -n "${INSIGHTENGINE_NAMESPACE}" -f "${OPERATOR_VALUES_PATH}" \
  --dry-run=server --hide-secret >/dev/null
assert_review_lock_matches "${VERIFY_DIR}"
helm upgrade --install "${OPERATOR_RELEASE}" "${OPERATOR_CHART_PATH}" \
  -n "${INSIGHTENGINE_NAMESPACE}" -f "${OPERATOR_VALUES_PATH}" \
  --wait --timeout 15m
helm upgrade --install "${BACKEND_RELEASE}" "${BACKEND_CHART_PATH}" \
  -n "${INSIGHTENGINE_NAMESPACE}" -f "${BACKEND_VALUES_PATH}" \
  --dry-run=server --hide-secret >/dev/null
assert_review_lock_matches "${VERIFY_DIR}"
helm upgrade --install "${BACKEND_RELEASE}" "${BACKEND_CHART_PATH}" \
  -n "${INSIGHTENGINE_NAMESPACE}" -f "${BACKEND_VALUES_PATH}" \
  --wait --timeout 15m

helm -n "${INSIGHTENGINE_NAMESPACE}" list
printf 'PASS: Helm installation sequence completed. Wait for the end-user policy CR, assign it to query users, then run verify.sh.\n'
