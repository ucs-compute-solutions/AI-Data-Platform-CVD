#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

env_file=""
mode="initial"
while (( $# )); do
  case "$1" in
    --env) env_file="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; shift 2 ;;
    *) die "usage: $0 --env <release-inputs.env> [--mode initial|steady|critic]" ;;
  esac
done
[[ -n "${env_file}" ]] || die "usage: $0 --env <release-inputs.env> [--mode initial|steady|critic]"
[[ "${mode}" =~ ^(initial|steady|critic)$ ]] || die "mode must be initial, steady, or critic"

if [[ "${mode}" == "critic" ]]; then
  printf 'Critic mode requires a fresh entitlement check for the locked Cosmos3 image.\n'
  "${SCRIPT_DIR}/check-entitlements.sh" --mode critic
fi

"${SCRIPT_DIR}/preflight.sh" --env "${env_file}"
load_env "${env_file}"
if [[ "${mode}" == "critic" ]]; then
  verify_nim_operator
fi
mkdir -p "${CVD_RENDER_DIR}"

# Keep the pinned checkout pristine. Build a private source snapshot and apply
# the reviewed OpenShift patch only inside that snapshot.
source_snapshot="$(mktemp -d "${CVD_RENDER_DIR}/source-${mode}.XXXXXX")"
source_archive="${source_snapshot}/source.tar"
git -C "${CVD_SOURCE_DIR}" archive --format=tar --output="${source_archive}" HEAD
tar -xf "${source_archive}" -C "${source_snapshot}"
rm -f -- "${source_archive}"
(
  cd "${source_snapshot}"
  git apply "${CVD_OPENSHIFT_PATCH}"
)

site_values="${CVD_RENDER_DIR}/site-values.yaml"
critic_values="${CVD_RENDER_DIR}/critic.yaml"
namespace_manifest="${CVD_RENDER_DIR}/namespace-rbac.yaml"
routes_manifest="${CVD_RENDER_DIR}/routes.yaml"
rendered_manifest="${CVD_RENDER_DIR}/nvidia-vss-search-${mode}.yaml"
render_template "${CVD_SITE_VALUES_EXAMPLE}" "${site_values}"
render_template "${CVD_CRITIC_VALUES_EXAMPLE}" "${critic_values}"
render_template "${CVD_NAMESPACE_TEMPLATE}" "${namespace_manifest}"
render_template "${CVD_ROUTES_TEMPLATE}" "${routes_manifest}"

expected_critic_image="$(lock_value criticImage)"
critic_repository="$(awk '$1 == "repository:" {print $2; exit}' "${critic_values}")"
critic_tag="$(awk '$1 == "tag:" {gsub(/\"/, "", $2); print $2; exit}' "${critic_values}")"
[[ "${critic_repository}:${critic_tag}" == "${expected_critic_image}" ]] || \
  die "critic values do not match the locked image ${expected_critic_image}"

chart="$(chart_dir_for "${source_snapshot}")"
helm dependency build "${chart}" >/dev/null
values_args=(-f "${site_values}")
if [[ "${mode}" == "initial" ]]; then
  values_args+=(-f "${CVD_BOOTSTRAP_VALUES}")
elif [[ "${mode}" == "steady" ]]; then
  values_args+=(-f "${CVD_STEADY_VALUES}")
elif [[ "${mode}" == "critic" ]]; then
  values_args+=(-f "${CVD_STEADY_VALUES}" -f "${critic_values}")
fi

helm lint "${chart}" "${values_args[@]}"
helm template "${CVD_RELEASE}" "${chart}" \
  --namespace "${CVD_NAMESPACE}" \
  --kube-version "${CVD_KUBE_VERSION}" \
  --api-versions route.openshift.io/v1 \
  --include-crds "${values_args[@]}" >"${rendered_manifest}"

if grep -En '^[[:space:]]*image:[[:space:]].*:latest"?[[:space:]]*$' "${rendered_manifest}"; then
  die "rendered manifest contains an active latest image reference"
fi
if grep -En '^[[:space:]]*storageClassName:[[:space:]]*(""|null)?[[:space:]]*$' "${rendered_manifest}"; then
  die "rendered manifest contains an empty StorageClass"
fi
if grep -En '__[A-Z0-9_]+__' "${site_values}" "${critic_values}" "${namespace_manifest}" "${routes_manifest}"; then
  die "rendered inputs contain unresolved template tokens"
fi
grep -Fq "$(lock_value huggingFaceModel)" "${rendered_manifest}" || \
  die "rendered Search profile does not reference the locked Cosmos Embed repository"

if oc get namespace "${CVD_NAMESPACE}" >/dev/null 2>&1; then
  oc apply --dry-run=server -f "${namespace_manifest}" >/dev/null
  oc apply --dry-run=server -f "${routes_manifest}" >/dev/null
  oc apply --dry-run=server -f "${rendered_manifest}" >/dev/null
  validation_scope="server-side validation completed for all rendered resources"
else
  # A server-side dry run does not persist a Namespace, so the API server cannot
  # validate namespaced objects against a Namespace that does not yet exist.
  oc apply --dry-run=client --validate=true -f "${namespace_manifest}" >/dev/null
  oc apply --dry-run=client --validate=true -f "${routes_manifest}" >/dev/null
  oc apply --dry-run=client --validate=true -f "${rendered_manifest}" >/dev/null
  validation_scope="client validation completed for namespaced resources; rerun after the approved Namespace apply for full server-side validation"
fi

printf '%s\n' "${chart}" >"${CVD_RENDER_DIR}/chart-path-${mode}.txt"
review_digest="$(compute_review_digest "${env_file}" "${mode}" "${chart}")"
printf '%s  reviewed-artifacts-%s\n' "${review_digest}" "${mode}" > \
  "${CVD_RENDER_DIR}/review-digest-${mode}.sha256"

printf 'PASS: Helm lint and render audit completed; %s.\n' "${validation_scope}"
printf 'Mode: %s\n' "${mode}"
printf 'Rendered directory: %s\n' "${CVD_RENDER_DIR}"
printf 'Patched source snapshot: %s\n' "${source_snapshot}"
printf 'Review digest: %s\n' "${review_digest}"
printf 'No OpenShift resources or Secrets were changed.\n'
