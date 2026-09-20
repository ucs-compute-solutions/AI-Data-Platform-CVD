#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DATAENGINE_CVD_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)

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

has_flag() {
  local wanted=$1
  shift
  local arg
  for arg in "$@"; do
    [[ "${arg}" == "${wanted}" ]] && return 0
  done
  return 1
}

verify_sha256() {
  local expected=${1,,} file_path=$2 description=$3
  [[ "${expected}" =~ ^[[:xdigit:]]{64}$ ]] || \
    die "${description} SHA-256 must contain exactly 64 hexadecimal characters"
  [[ -f "${file_path}" ]] || die "${description} not found: ${file_path}"
  printf '%s  %s\n' "${expected}" "${file_path}" | sha256sum --check - >/dev/null || \
    die "${description} SHA-256 verification failed: ${file_path}"
  printf 'Verified SHA-256: %s\n' "${description}"
}

write_curl_basic_auth_config() {
  local target=$1 username=$2 password=$3 escaped
  [[ "${username}" != *:* ]] || die "registry username must not contain ':'"
  [[ "${username}" != *$'\n'* && "${password}" != *$'\n'* ]] || \
    die "registry credentials must not contain a newline"
  escaped="${username}:${password}"
  escaped=${escaped//\\/\\\\}
  escaped=${escaped//\"/\\\"}
  (umask 077; printf 'user = "%s"\n' "${escaped}" > "${target}")
}

mutation_namespaces() {
  require_value ZARF_MUTATION_NAMESPACES
  local raw_namespace
  local -a _mutation_namespaces
  IFS=',' read -r -a _mutation_namespaces <<< "${ZARF_MUTATION_NAMESPACES}"
  for raw_namespace in "${_mutation_namespaces[@]}"; do
    # Trim whitespace around comma-separated values, but reject whitespace
    # inside a namespace instead of silently rewriting a typo.
    raw_namespace="${raw_namespace#"${raw_namespace%%[![:space:]]*}"}"
    raw_namespace="${raw_namespace%"${raw_namespace##*[![:space:]]}"}"
    [[ -n "${raw_namespace}" ]] || die "ZARF_MUTATION_NAMESPACES contains an empty entry"
    [[ "${raw_namespace}" != *[[:space:]]* ]] || \
      die "invalid whitespace in Zarf mutation namespace: ${raw_namespace}"
    [[ ${#raw_namespace} -le 63 && "${raw_namespace}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || \
      die "invalid Kubernetes namespace in ZARF_MUTATION_NAMESPACES: ${raw_namespace}"
    case "${raw_namespace}" in
      default|kube-*|openshift-*|zarf|zot|vast-permissions)
        die "refusing unsafe Zarf mutation namespace: ${raw_namespace}"
        ;;
    esac
    printf '%s\n' "${raw_namespace}"
  done
}

namespace_is_in_mutation_scope() {
  local candidate=$1 approved
  while IFS= read -r approved; do
    [[ "${candidate}" == "${approved}" ]] && return 0
  done < <(mutation_namespaces)
  return 1
}

require_namespace_in_mutation_scope() {
  local namespace_name=$1
  namespace_is_in_mutation_scope "${namespace_name}" || \
    die "required namespace/${namespace_name} is absent from ZARF_MUTATION_NAMESPACES"
}

assert_existing_mutation_scope_is_approved() {
  local namespace_name resource_type resource_ref resource_namespace
  mutation_namespaces >/dev/null
  while IFS= read -r namespace_name; do
    [[ -n "${namespace_name}" ]] || continue
    namespace_is_in_mutation_scope "${namespace_name}" || \
      die "namespace/${namespace_name} is already labeled zarf.dev/agent=mutate but is not in ZARF_MUTATION_NAMESPACES"
  done < <(oc get namespaces -l 'zarf.dev/agent=mutate' \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

  # A resource label overrides its namespace label in Zarf. Check the common
  # PodSpec-bearing workload kinds as well as namespace labels so an isolated
  # resource cannot silently widen the approved mutation scope.
  for resource_type in \
    pods deployments.apps statefulsets.apps daemonsets.apps \
    jobs.batch cronjobs.batch; do
    while IFS= read -r resource_ref; do
      [[ -n "${resource_ref}" ]] || continue
      resource_namespace=${resource_ref%%/*}
      [[ "${resource_namespace}" == "zarf" ]] && continue
      namespace_is_in_mutation_scope "${resource_namespace}" || \
        die "${resource_type}/${resource_ref} is labeled zarf.dev/agent=mutate outside the approved namespace scope"
    done < <(oc get "${resource_type}" --all-namespaces \
      -l 'zarf.dev/agent=mutate' \
      -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{"\n"}{end}')
  done
}

load_inputs() {
  local env_file=$1
  [[ -f "${env_file}" ]] || die "input file not found: ${env_file}"
  set -a
  # shellcheck disable=SC1090
  source "${env_file}"
  set +a

  VAST_PERMISSIONS_VALUES=${VAST_PERMISSIONS_VALUES:-permissions/vast-permissions-values.yaml}
  if [[ "${VAST_PERMISSIONS_VALUES}" != /* ]]; then
    VAST_PERMISSIONS_VALUES="${DATAENGINE_CVD_DIR}/${VAST_PERMISSIONS_VALUES}"
  fi
}

print_context() {
  printf 'OpenShift identity: %s\n' "$(oc whoami)"
  printf 'OpenShift API: %s\n' "$(oc whoami --show-server)"
  printf 'Current kubeconfig context: %s\n' "$(oc config current-context)"
}

verify_context() {
  require_value EXPECTED_OCP_API
  local actual
  actual=$(oc whoami --show-server)
  [[ "${actual}" == "${EXPECTED_OCP_API}" ]] || \
    die "OpenShift API mismatch: expected ${EXPECTED_OCP_API}; got ${actual}"
}

materialize_zot_ca() {
  local target=$1
  if [[ -n "${ZOT_CA_BUNDLE:-}" ]]; then
    [[ -f "${ZOT_CA_BUNDLE}" ]] || die "Zot CA bundle not found: ${ZOT_CA_BUNDLE}"
    cp "${ZOT_CA_BUNDLE}" "${target}"
  else
    oc extract configmap/default-ingress-cert \
      --namespace openshift-config-managed \
      --keys ca-bundle.crt \
      --to "$(dirname "${target}")" \
      --confirm >/dev/null
    if [[ "$(dirname "${target}")/ca-bundle.crt" != "${target}" ]]; then
      mv "$(dirname "${target}")/ca-bundle.crt" "${target}"
    fi
  fi
  [[ -s "${target}" ]] || die "Zot CA bundle is empty: ${target}"
}

has_apply_flag() {
  has_flag --apply "$@"
}

env_path_from_args() {
  local previous="" arg
  for arg in "$@"; do
    if [[ "${previous}" == "--env" ]]; then
      printf '%s\n' "${arg}"
      return 0
    fi
    previous=${arg}
  done
  printf '%s\n' "${DATAENGINE_CVD_DIR}/release-inputs.env"
}
