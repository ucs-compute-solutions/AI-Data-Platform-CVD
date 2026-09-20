#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE=$(env_path_from_args "$@")
load_inputs "${ENV_FILE}"

for command_name in oc helm envsubst jq curl htpasswd base64 grep cut; do
  require_command "${command_name}"
done
for value_name in \
  EXPECTED_OCP_API ZOT_NAMESPACE ZOT_RELEASE ZOT_CHART_VERSION ZOT_HOST \
  ZOT_USER ZOT_STORAGE_CLASS ZOT_STORAGE_SIZE INGRESS_CLASS \
  REGISTRY_TRUST_CONFIGMAP VAST_PERMISSIONS_NAMESPACE \
  VAST_PERMISSIONS_RELEASE VAST_PERMISSIONS_CHART; do
  require_value "${value_name}"
done

print_context
verify_context

ROTATE_CREDENTIALS=false
has_flag --rotate-credentials "$@" && ROTATE_CREDENTIALS=true
ZOT_EXISTS=false
HELM_RELEASES=$(helm -n "${ZOT_NAMESPACE}" list --all --output json 2>/dev/null || printf '[]')
if [[ "$(jq -r --arg release "${ZOT_RELEASE}" \
  '[.[] | select(.name == $release)] | length' <<< "${HELM_RELEASES}")" -gt 0 ]]; then
  ZOT_EXISTS=true
fi

cat <<PLAN

Planned changes
  1. Install the release-supplied VAST permissions chart in ${VAST_PERMISSIONS_NAMESPACE}.
  2. Reconcile Zot chart ${ZOT_CHART_VERSION} in ${ZOT_NAMESPACE} without deleting existing registry users.
  3. Create a ${ZOT_STORAGE_SIZE} PVC using ${ZOT_STORAGE_CLASS}.
  4. Expose Zot through a NodePort service and edge-terminated Route at https://${ZOT_HOST}.
  5. Merge the OpenShift ingress CA into ConfigMap/${REGISTRY_TRUST_CONFIGMAP}.
  6. Preserve existing registry allowlist entries and wait for unpaused MachineConfigPools.
  Existing Zot release: ${ZOT_EXISTS}
  Rotate ${ZOT_USER} credentials: ${ROTATE_CREDENTIALS}
PLAN

if ! has_apply_flag "$@"; then
  printf '\nDRY RUN: no changes made. Re-run with --apply after review.\n'
  exit 0
fi

[[ -d "${VAST_PERMISSIONS_CHART}" || -f "${VAST_PERMISSIONS_CHART}" ]] || \
  die "permissions chart not found: ${VAST_PERMISSIONS_CHART}"
[[ -f "${VAST_PERMISSIONS_VALUES}" ]] || die "permissions values file not found: ${VAST_PERMISSIONS_VALUES}"

TMP_DIR=$(mktemp -d)
cleanup() {
  unset ZOT_PASSWORD ZOT_PASSWORD_CONFIRM
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT
chmod 700 "${TMP_DIR}"

prompt_for_zot_password() {
  read -r -s -p "New Zot password for ${ZOT_USER}: " ZOT_PASSWORD
  printf '\n'
  read -r -s -p "Confirm Zot password: " ZOT_PASSWORD_CONFIRM
  printf '\n'
  [[ -n "${ZOT_PASSWORD}" ]] || die "Zot password must not be empty"
  [[ "${ZOT_PASSWORD}" == "${ZOT_PASSWORD_CONFIRM}" ]] || die "passwords do not match"
  unset ZOT_PASSWORD_CONFIRM
}

if [[ "${ZOT_EXISTS}" == "true" ]]; then
  mapfile -t HTPASSWD_SECRETS < <(oc -n "${ZOT_NAMESPACE}" get secrets -o json | \
    jq -r '.items[] | select(.data.htpasswd != null) | .metadata.name')
  ((${#HTPASSWD_SECRETS[@]} == 1)) || \
    die "expected exactly one existing Zot Secret containing data.htpasswd; found ${#HTPASSWD_SECRETS[@]}"
  oc -n "${ZOT_NAMESPACE}" get secret "${HTPASSWD_SECRETS[0]}" -o json | \
    jq -r '.data.htpasswd' | base64 --decode > "${TMP_DIR}/htpasswd"
  [[ -s "${TMP_DIR}/htpasswd" ]] || die "existing Zot htpasswd is empty"
  if [[ "${ROTATE_CREDENTIALS}" == "true" ]]; then
    prompt_for_zot_password
    printf '%s\n' "${ZOT_PASSWORD}" | \
      htpasswd -i -B "${TMP_DIR}/htpasswd" "${ZOT_USER}" >/dev/null
  elif ! cut -d: -f1 "${TMP_DIR}/htpasswd" | grep -Fxq "${ZOT_USER}"; then
    die "ZOT_USER is absent from the existing registry; rerun with --rotate-credentials to add it deliberately"
  fi
else
  prompt_for_zot_password
  printf '%s\n' "${ZOT_PASSWORD}" | \
    htpasswd -i -B -c "${TMP_DIR}/htpasswd" "${ZOT_USER}" >/dev/null
fi

export ZOT_NAMESPACE ZOT_RELEASE ZOT_CHART_VERSION ZOT_HOST ZOT_USER
export ZOT_STORAGE_CLASS ZOT_STORAGE_SIZE INGRESS_CLASS
envsubst '${INGRESS_CLASS} ${ZOT_HOST} ${ZOT_USER} ${ZOT_STORAGE_CLASS} ${ZOT_STORAGE_SIZE}' \
  < "${DATAENGINE_CVD_DIR}/registry/zot-values.yaml.tpl" \
  > "${TMP_DIR}/zot-values.yaml"

# Render both charts and resolve the pinned Zot chart before any cluster write.
helm repo add zot https://zotregistry.dev/helm-charts/ --force-update
helm repo update zot
helm show chart zot/zot --version "${ZOT_CHART_VERSION}" >/dev/null
helm template "${VAST_PERMISSIONS_RELEASE}" "${VAST_PERMISSIONS_CHART}" \
  --namespace "${VAST_PERMISSIONS_NAMESPACE}" \
  --values "${VAST_PERMISSIONS_VALUES}" > "${TMP_DIR}/vast-permissions-rendered.yaml"
helm template "${ZOT_RELEASE}" zot/zot \
  --namespace "${ZOT_NAMESPACE}" \
  --version "${ZOT_CHART_VERSION}" \
  --values "${TMP_DIR}/zot-values.yaml" \
  --set-file secretFiles.htpasswd="${TMP_DIR}/htpasswd" \
  > "${TMP_DIR}/zot-rendered.yaml"

while IFS= read -r pool_name; do
  [[ -n "${pool_name}" ]] || continue
  [[ "$(oc get machineconfigpool "${pool_name}" -o jsonpath='{.spec.paused}')" != "true" ]] || \
    die "MachineConfigPool/${pool_name} is paused; resume or formally exclude it before changing registry trust"
done < <(oc get machineconfigpools -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

helm upgrade --install "${VAST_PERMISSIONS_RELEASE}" "${VAST_PERMISSIONS_CHART}" \
  --namespace "${VAST_PERMISSIONS_NAMESPACE}" \
  --create-namespace \
  --values "${VAST_PERMISSIONS_VALUES}" \
  --atomic --wait --timeout 10m

helm upgrade --install "${ZOT_RELEASE}" zot/zot \
  --namespace "${ZOT_NAMESPACE}" \
  --create-namespace \
  --version "${ZOT_CHART_VERSION}" \
  --values "${TMP_DIR}/zot-values.yaml" \
  --set-file secretFiles.htpasswd="${TMP_DIR}/htpasswd" \
  --atomic --wait --timeout 10m

deadline=$((SECONDS + 120))
until [[ "$(oc -n "${ZOT_NAMESPACE}" get pods \
  --selector "app.kubernetes.io/instance=${ZOT_RELEASE}" \
  -o json | jq '.items | length')" -gt 0 ]]; do
  ((SECONDS < deadline)) || die "timed out waiting for the Zot controller to create a pod"
  sleep 2
done
oc -n "${ZOT_NAMESPACE}" wait pods \
  --selector "app.kubernetes.io/instance=${ZOT_RELEASE}" \
  --for=condition=Ready --timeout=10m

oc -n "${ZOT_NAMESPACE}" get pods,pvc,service,route -o wide
ROUTE_HOST=$(oc -n "${ZOT_NAMESPACE}" get route \
  -o jsonpath="{range .items[?(@.spec.host=='${ZOT_HOST}')]}{.spec.host}{end}")
[[ "${ROUTE_HOST}" == "${ZOT_HOST}" ]] || die "Zot Route was not created for ${ZOT_HOST}"
oc -n "${ZOT_NAMESPACE}" get routes -o json | jq -e --arg host "${ZOT_HOST}" '
  any(.items[];
    .spec.host == $host and
    any(.status.ingress[]?.conditions[]?; .type == "Admitted" and .status == "True"))
' >/dev/null || die "Zot Route ${ZOT_HOST} is not admitted"

materialize_zot_ca "${TMP_DIR}/ca-bundle.crt"

declare -A MCP_CONFIGURATION_BEFORE
while IFS= read -r pool_name; do
  [[ -n "${pool_name}" ]] || continue
  MCP_CONFIGURATION_BEFORE["${pool_name}"]=$(oc get machineconfigpool "${pool_name}" \
    -o jsonpath='{.spec.configuration.name}')
done < <(oc get machineconfigpools -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

TRUST_CHANGED=false

CURRENT_TRUST_CM=$(oc get image.config.openshift.io cluster \
  -o jsonpath='{.spec.additionalTrustedCA.name}' 2>/dev/null || true)
TARGET_TRUST_CM=${CURRENT_TRUST_CM:-${REGISTRY_TRUST_CONFIGMAP}}

if oc -n openshift-config get configmap "${TARGET_TRUST_CM}" >/dev/null 2>&1; then
  EXISTING_BUNDLE=$(oc -n openshift-config get configmap "${TARGET_TRUST_CM}" -o json | \
    jq -r --arg host "${ZOT_HOST}" '.data[$host] // ""')
  NEW_BUNDLE=$(< "${TMP_DIR}/ca-bundle.crt")
  if [[ "${EXISTING_BUNDLE}" != "${NEW_BUNDLE}" ]]; then
    jq -n --arg host "${ZOT_HOST}" --rawfile bundle "${TMP_DIR}/ca-bundle.crt" \
      '{data:{($host):$bundle}}' > "${TMP_DIR}/registry-ca-patch.json"
    oc -n openshift-config patch configmap "${TARGET_TRUST_CM}" \
      --type=strategic \
      --patch-file="${TMP_DIR}/registry-ca-patch.json"
    TRUST_CHANGED=true
  fi
else
  oc -n openshift-config create configmap "${TARGET_TRUST_CM}" \
    --from-file="${ZOT_HOST}=${TMP_DIR}/ca-bundle.crt"
  TRUST_CHANGED=true
fi

if [[ -z "${CURRENT_TRUST_CM}" ]]; then
  oc patch image.config.openshift.io/cluster \
    --type=merge \
    --patch "{\"spec\":{\"additionalTrustedCA\":{\"name\":\"${TARGET_TRUST_CM}\"}}}"
  TRUST_CHANGED=true
fi

mapfile -t ALLOWED_REGISTRIES < <(oc get image.config.openshift.io/cluster \
  -o jsonpath='{range .spec.registrySources.allowedRegistries[*]}{.}{"\n"}{end}')
if ((${#ALLOWED_REGISTRIES[@]} > 0)); then
  if ! printf '%s\n' "${ALLOWED_REGISTRIES[@]}" | grep -Fxq "${ZOT_HOST}"; then
    oc patch image.config.openshift.io/cluster \
      --type=json \
      --patch "[{\"op\":\"add\",\"path\":\"/spec/registrySources/allowedRegistries/-\",\"value\":\"${ZOT_HOST}\"}]"
    TRUST_CHANGED=true
  fi
fi

if [[ "${TRUST_CHANGED}" == "true" ]]; then
  while IFS= read -r pool_name; do
    [[ -n "${pool_name}" ]] || continue
    deadline=$((SECONDS + 600))
    until [[ "$(oc get machineconfigpool "${pool_name}" -o jsonpath='{.spec.configuration.name}')" \
      != "${MCP_CONFIGURATION_BEFORE[$pool_name]}" ]]; do
      ((SECONDS < deadline)) || \
        die "MachineConfigPool/${pool_name} did not observe the registry-trust change"
      sleep 5
    done
    oc wait "machineconfigpool/${pool_name}" --for=condition=Updated --timeout=30m
    oc get machineconfigpool "${pool_name}" -o json | jq -e '
      .status.updatedMachineCount == .status.machineCount and
      .status.readyMachineCount == .status.machineCount and
      (.status.degradedMachineCount // 0) == 0
    ' >/dev/null || die "MachineConfigPool/${pool_name} did not converge cleanly"
  done < <(oc get machineconfigpools -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
fi

if [[ -n "${ZOT_PASSWORD:-}" ]]; then
  write_curl_basic_auth_config "${TMP_DIR}/curl.conf" "${ZOT_USER}" "${ZOT_PASSWORD}"
  curl --fail --silent --show-error \
    --config "${TMP_DIR}/curl.conf" \
    --cacert "${TMP_DIR}/ca-bundle.crt" \
    "https://${ZOT_HOST}/v2/" >/dev/null
else
  HTTP_STATUS=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --cacert "${TMP_DIR}/ca-bundle.crt" "https://${ZOT_HOST}/v2/")
  [[ "${HTTP_STATUS}" == "401" ]] || \
    die "expected unauthenticated Zot /v2/ request to return 401; got ${HTTP_STATUS}"
fi

printf '\nPASS: Zot is Ready, credentials were preserved unless rotation was requested, and registry trust converged.\n'
if [[ "${ROTATE_CREDENTIALS}" == "true" ]]; then
  printf 'ACTION: update the Zot credential stored in VAST before deploying new workloads.\n'
fi
