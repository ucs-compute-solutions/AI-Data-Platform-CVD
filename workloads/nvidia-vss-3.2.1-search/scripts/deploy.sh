#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

env_file=""
mode="initial"
apply=false
retry_initial=false
while (( $# )); do
  case "$1" in
    --env) env_file="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; shift 2 ;;
    --apply) apply=true; shift ;;
    --retry-initial) retry_initial=true; shift ;;
    *) die "usage: $0 --env <release-inputs.env> --mode initial|steady|critic [--apply] [--retry-initial]" ;;
  esac
done
[[ -n "${env_file}" ]] || die "usage: $0 --env <release-inputs.env> --mode initial|steady|critic [--apply]"
[[ "${mode}" =~ ^(initial|steady|critic)$ ]] || die "mode must be initial, steady, or critic"
if [[ "${retry_initial}" == true && ( "${mode}" != "initial" || "${apply}" != true ) ]]; then
  die "--retry-initial is valid only with --mode initial --apply after explicit recovery approval"
fi

if [[ "${apply}" != true ]]; then
  "${SCRIPT_DIR}/render.sh" --env "${env_file}" --mode "${mode}"
  printf 'PREVIEW ONLY: review the rendered directory, then rerun with --apply after change approval.\n'
  exit 0
fi

"${SCRIPT_DIR}/preflight.sh" --env "${env_file}"
load_env "${env_file}"
if [[ "${mode}" == "critic" ]]; then
  verify_nim_operator
fi

chart_path_file="${CVD_RENDER_DIR}/chart-path-${mode}.txt"
[[ -r "${chart_path_file}" ]] || die "rendered chart-path file is unavailable: ${chart_path_file}"
chart="$(<"${chart_path_file}")"
case "${chart}" in
  "${CVD_RENDER_DIR}"/source-"${mode}".*/deploy/helm/developer-profiles/dev-profile-search) ;;
  *) die "rendered chart path is outside the approved private snapshot: ${chart}" ;;
esac
[[ -r "${chart}/Chart.yaml" ]] || die "rendered chart snapshot is unavailable: ${chart}"
digest_file="${CVD_RENDER_DIR}/review-digest-${mode}.sha256"
[[ -r "${digest_file}" ]] || die "review digest is unavailable; run preview and obtain approval first"
reviewed_digest="$(awk 'NR == 1 {print $1}' "${digest_file}")"
current_digest="$(compute_review_digest "${env_file}" "${mode}" "${chart}")"
[[ "${current_digest}" == "${reviewed_digest}" ]] || \
  die "reviewed inputs changed after preview; render and review the deployment again"
printf 'PASS: apply is bound to reviewed digest %s.\n' "${reviewed_digest}"

oc get namespace "${CVD_NAMESPACE}" -o name >/dev/null 2>&1 || \
  die "namespace ${CVD_NAMESPACE} does not exist; apply the reviewed namespace/RBAC manifest first"

release_exists=false
release_status="absent"
if [[ "$(helm -n "${CVD_NAMESPACE}" list --all --filter "^${CVD_RELEASE}$" -q)" == "${CVD_RELEASE}" ]]; then
  release_exists=true
  release_status="$(helm -n "${CVD_NAMESPACE}" status "${CVD_RELEASE}" -o json | \
    python3 -c 'import json, sys; print(json.load(sys.stdin)["info"]["status"])')"
fi
if [[ "${mode}" != "initial" && "${release_exists}" != true ]]; then
  die "${mode} mode requires an existing release created successfully in initial mode"
fi
resume_initial=false
if [[ "${mode}" == "initial" && "${release_exists}" == true ]]; then
  existing_marker="$(oc -n "${CVD_NAMESPACE}" get pvc vss-rtvi-cv-models \
    -o jsonpath='{.metadata.annotations.cvd\.cisco\.com/rt-cv-models-populated}' 2>/dev/null || true)"
  [[ "${existing_marker}" != "true" ]] || \
    die "initial mode already completed; preview and apply steady mode"
  case "${release_status}" in
    deployed)
      if [[ "${retry_initial}" == true ]]; then
        printf 'RECOVERY: recreating the initial downloader Job while retaining its model PVC.\n'
      else
        resume_initial=true
        printf 'RESUME: Helm is deployed; validating the existing initial downloader and model PVC.\n'
      fi
      ;;
    failed)
      [[ "${retry_initial}" == true ]] || \
        die "initial release is failed; inspect events and logs, obtain recovery approval, then rerun with --retry-initial"
      printf 'RECOVERY: retrying the failed initial release while retaining its model PVC.\n'
      ;;
    *)
      die "initial release is ${release_status}; resolve the active Helm operation before retrying"
      ;;
  esac
fi
if [[ "${mode}" != "initial" ]]; then
  [[ "${release_status}" == "deployed" ]] || \
    die "${mode} mode requires the existing Helm release to be deployed; observed ${release_status}"
  model_pvc_contract="$(oc -n "${CVD_NAMESPACE}" get pvc vss-rtvi-cv-models \
    -o jsonpath='{.status.phase}|{.spec.storageClassName}|{.metadata.annotations.cvd\.cisco\.com/rt-cv-models-populated}' 2>/dev/null || true)"
  [[ "${model_pvc_contract}" == "Bound|${CVD_STORAGE_CLASS}|true" ]] || \
    die "RT-CV model PVC is not marked as successfully populated by initial mode: ${model_pvc_contract:-absent}"
fi

additional_gpus=0
if [[ "${mode}" == "initial" && "${release_exists}" != true ]]; then
  additional_gpus=3
elif [[ "${mode}" == "steady" ]]; then
  current_rt_cv_replicas="$(oc -n "${CVD_NAMESPACE}" get statefulset vss-rtvi-cv \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || printf '0')"
  [[ "${current_rt_cv_replicas}" == "0" ]] && additional_gpus=1
elif ! oc -n "${CVD_NAMESPACE}" get nimservice nvidia-cosmos3-reasoner >/dev/null 2>&1; then
  additional_gpus=1
fi
require_available_gpus \
  "${CVD_GPU_NODE_SELECTOR_KEY}=${CVD_GPU_NODE_SELECTOR_VALUE}" \
  "${additional_gpus}"

pull_secret_contract="$(oc -n "${CVD_NAMESPACE}" get secret "${CVD_NGC_PULL_SECRET}" \
  -o go-template='{{if and (eq .type "kubernetes.io/dockerconfigjson") (index .data ".dockerconfigjson")}}present{{else}}invalid{{end}}{{"\n"}}' 2>/dev/null || true)"
api_secret_contract="$(oc -n "${CVD_NAMESPACE}" get secret "${CVD_NGC_API_SECRET}" \
  -o go-template='{{if and (eq .type "Opaque") (index .data "NGC_API_KEY")}}present{{else}}invalid{{end}}{{"\n"}}' 2>/dev/null || true)"
[[ "${pull_secret_contract}" == "present" ]] || \
  die "pull Secret ${CVD_NGC_PULL_SECRET} must be type kubernetes.io/dockerconfigjson with key .dockerconfigjson"
[[ "${api_secret_contract}" == "present" ]] || \
  die "API Secret ${CVD_NGC_API_SECRET} must be type Opaque with key NGC_API_KEY"

oc apply -f "${CVD_RENDER_DIR}/namespace-rbac.yaml"

if [[ "${retry_initial}" == true ]]; then
  oc -n "${CVD_NAMESPACE}" delete job vss-rtvi-cv-download-models \
    --ignore-not-found=true --wait=true
fi

values_args=(-f "${CVD_RENDER_DIR}/site-values.yaml")
if [[ "${mode}" == "initial" ]]; then
  values_args+=(-f "${CVD_BOOTSTRAP_VALUES}")
elif [[ "${mode}" == "steady" ]]; then
  values_args+=(-f "${CVD_STEADY_VALUES}")
elif [[ "${mode}" == "critic" ]]; then
  values_args+=(-f "${CVD_STEADY_VALUES}" -f "${CVD_RENDER_DIR}/critic.yaml")
fi

helm_wait_args=(--wait --timeout "${CVD_HELM_TIMEOUT}")
if [[ "${mode}" == "initial" ]]; then
  helm_wait_args+=(--wait-for-jobs)
fi
if [[ "${resume_initial}" != true ]]; then
  helm upgrade --install "${CVD_RELEASE}" "${chart}" \
    -n "${CVD_NAMESPACE}" "${values_args[@]}" \
    "${helm_wait_args[@]}"
fi

if [[ "${mode}" == "initial" ]]; then
  downloader_job="vss-rtvi-cv-download-models"
  if ! oc -n "${CVD_NAMESPACE}" get "job/${downloader_job}" >/dev/null 2>&1; then
    die "initial downloader Job is absent; inspect the Helm release, obtain recovery approval, then rerun initial --apply with --retry-initial"
  fi
  oc -n "${CVD_NAMESPACE}" wait --for=condition=complete \
    "job/${downloader_job}" --timeout="${CVD_HELM_TIMEOUT}"
  job_contract="$(oc -n "${CVD_NAMESPACE}" get job "${downloader_job}" \
    -o jsonpath='{.metadata.labels.app\.kubernetes\.io/instance}|{.metadata.labels.app\.kubernetes\.io/name}|{.spec.template.spec.volumes[?(@.name=="models")].persistentVolumeClaim.claimName}|{.status.succeeded}|{.spec.ttlSecondsAfterFinished}')"
  [[ "${job_contract}" == "${CVD_RELEASE}|vss-rtvi-cv|vss-rtvi-cv-models|1|3600" ]] || \
    die "RT-CV model downloader completed with an unexpected resource contract: ${job_contract}"
  rt_cv_replicas="$(oc -n "${CVD_NAMESPACE}" get statefulset vss-rtvi-cv -o jsonpath='{.spec.replicas}')"
  [[ "${rt_cv_replicas}" == "0" ]] || \
    die "RT-CV must remain at zero replicas during initial model population"
  pvc_contract="$(oc -n "${CVD_NAMESPACE}" get pvc vss-rtvi-cv-models \
    -o jsonpath='{.status.phase}|{.spec.storageClassName}|{.spec.accessModes[0]}')"
  [[ "${pvc_contract}" == "Bound|${CVD_STORAGE_CLASS}|ReadWriteOnce" ]] || \
    die "RT-CV model PVC contract failed: ${pvc_contract}"
  oc -n "${CVD_NAMESPACE}" annotate --overwrite pvc vss-rtvi-cv-models \
    cvd.cisco.com/rt-cv-models-populated=true >/dev/null
  printf 'PASS: RT-CV model downloader completed and the retained RWO model PVC is Bound.\n'
  printf 'Review job/%s logs, then apply steady mode to start RT-CV.\n' "${downloader_job}"
fi

oc apply -f "${CVD_RENDER_DIR}/routes.yaml"
printf 'PASS: NVIDIA VSS Search deployment applied in %s mode.\n' "${mode}"
printf 'Run scripts/verify.sh --mode %s, then complete the functional acceptance in the CVD section.\n' "${mode}"
