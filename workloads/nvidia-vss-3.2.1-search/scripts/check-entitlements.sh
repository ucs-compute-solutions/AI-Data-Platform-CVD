#!/usr/bin/env bash

set -euo pipefail
umask 077
ulimit -c 0

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

readonly NGC_REGISTRY="nvcr.io"
readonly NGC_USERNAME='$oauthtoken'
readonly NGC_ORG="nvidia"
readonly HF_REPOSITORY="https://huggingface.co/$(lock_value huggingFaceModel)"
readonly HF_API="https://huggingface.co/api/models/$(lock_value huggingFaceModel)"
readonly HF_REVISION="$(lock_value huggingFaceRevision)"
readonly EXPECTED_NGC_CLI_VERSION="$(lock_value ngcCliVersion)"
readonly CRITIC_IMAGE="$(lock_value criticImage)"

mode=""
while (( $# )); do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    *) die "usage: $0 --mode steady|critic" ;;
  esac
done
[[ "${mode}" =~ ^(steady|critic)$ ]] || die "usage: $0 --mode steady|critic"

VSS_IMAGES=(
  "nvcr.io/nvidia/vss-core/sdr-mw-l:3.2.0"
  "nvcr.io/nvidia/vss-core/vss-agent-ui:3.2.0"
  "nvcr.io/nvidia/vss-core/vss-agent:3.2.1"
  "nvcr.io/nvidia/vss-core/vss-behavior-analytics:3.2.1"
  "nvcr.io/nvidia/vss-core/vss-rt-cv:3.2.1"
  "nvcr.io/nvidia/vss-core/vss-rt-embed:3.2.1"
  "nvcr.io/nvidia/vss-core/vss-video-analytics-api:3.2.0"
  "nvcr.io/nvidia/vss-core/vss-vios-ingress:3.2.1"
  "nvcr.io/nvidia/vss-core/vss-vios-nvstreamer:3.2.1"
  "nvcr.io/nvidia/vss-core/vss-vios-sensor:3.2.1"
  "nvcr.io/nvidia/vss-core/vss-vios-streamprocessing:3.2.1"
)
if [[ "${mode}" == "critic" ]]; then
  VSS_IMAGES+=("${CRITIC_IMAGE}")
fi
readonly -a VSS_IMAGES

readonly -a NGC_MODELS=(
  "nvidia/tao/rtdetr_2d_warehouse:deployable_rn50_v1.0.2"
  "nvidia/tao/siglip_v2:deployable_v1.1"
)

for command_name in awk curl docker find git grep head python3 sha256sum; do
  require_command "${command_name}"
done

if [[ -n "${CVD_NGC_CLI:-}" ]]; then
  NGC_CLI="${CVD_NGC_CLI}"
else
  NGC_CLI="$(command -v ngc || true)"
fi
[[ -n "${NGC_CLI}" && -x "${NGC_CLI}" ]] || \
  die "NGC CLI is unavailable; install the locked version and set CVD_NGC_CLI to its absolute path"
readonly NGC_CLI

actual_ngc_cli_version="$("${NGC_CLI}" --version 2>&1 | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[[ "${actual_ngc_cli_version}" == "${EXPECTED_NGC_CLI_VERSION}" ]] || \
  die "NGC CLI version mismatch: expected ${EXPECTED_NGC_CLI_VERSION}, found ${actual_ngc_cli_version:-unknown}"
printf 'PASS: NGC CLI version lock: %s\n' "${actual_ngc_cli_version}"

check_tmp="$(mktemp -d /tmp/cvd-vss321-entitlement.XXXXXX)"
readonly check_tmp
readonly docker_config_dir="${check_tmp}/docker"
mkdir -m 700 "${docker_config_dir}"

cleanup() {
  unset NGC_API_KEY NGC_CLI_API_KEY NGC_CLI_ORG DOCKER_CONFIG
  case "${check_tmp}" in
    /tmp/cvd-vss321-entitlement.*)
      if [[ -f "${docker_config_dir}/config.json" ]]; then
        : >"${docker_config_dir}/config.json"
        rm -f -- "${docker_config_dir}/config.json"
      fi
      rm -rf -- "${check_tmp}"
      ;;
    *) printf 'WARNING: refusing to clean an unexpected temporary path.\n' >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM

read -rsp 'Paste NGC API key: ' NGC_API_KEY
printf '\n'
[[ -n "${NGC_API_KEY}" ]] || die "an NGC API key is required"

export DOCKER_CONFIG="${docker_config_dir}"
if ! printf '%s' "${NGC_API_KEY}" | docker login "${NGC_REGISTRY}" \
  --username "${NGC_USERNAME}" --password-stdin >/dev/null 2>&1; then
  die "NGC registry authentication was rejected"
fi
printf 'PASS: NGC registry authentication succeeded.\n'

for image in "${VSS_IMAGES[@]}"; do
  docker manifest inspect "${image}" >/dev/null 2>&1 || \
    die "image manifest is inaccessible: ${image}"
  printf 'PASS: image manifest accessible: %s\n' "${image}"
done

export NGC_CLI_API_KEY="${NGC_API_KEY}"
export NGC_CLI_ORG="${NGC_ORG}"
for model_index in "${!NGC_MODELS[@]}"; do
  model="${NGC_MODELS[${model_index}]}"
  model_info="${check_tmp}/ngc-model-${model_index}.json"
  "${NGC_CLI}" registry model info "${model}" \
    --files --format_type json >"${model_info}" 2>/dev/null || \
    die "NGC model metadata is inaccessible: ${model}"

  case "${model}" in
    nvidia/tao/rtdetr_2d_warehouse:deployable_rn50_v1.0.2)
      required_files=("rtdetr_warehouse_v1.0.2.fp16.onnx" "experiment.yaml")
      expected_probe_sha="a685988df29016f86e824c9295b9fc3032e7b02861b91b23e2718c3fadd734b5"
      ;;
    nvidia/tao/siglip_v2:deployable_v1.1)
      required_files=("siglip_v2_v1.1.onnx" "siglip_v2_v1.1_weights.bin" "siglip_v2_v1.1_tokenizer" "experiment.yaml")
      expected_probe_sha="19b9a92afb93e08f20364200154d6600f44d72527f235ea20a1bdbaef52ecd80"
      ;;
    *) die "no validation rule exists for NGC model ${model}" ;;
  esac

  for required_file in "${required_files[@]}"; do
    grep -Fq "${required_file}" "${model_info}" || \
      die "required artifact is absent from ${model}: ${required_file}"
  done

  probe_dir="${check_tmp}/ngc-probe-${model_index}"
  mkdir -m 700 "${probe_dir}"
  (
    cd "${probe_dir}"
    "${NGC_CLI}" registry model download-version "${model}" \
      --file experiment.yaml >/dev/null 2>&1
  ) || die "NGC artifact download probe failed: ${model}"
  probe_file="$(find "${probe_dir}" -type f -name experiment.yaml -print -quit)"
  [[ -n "${probe_file}" ]] || die "NGC artifact probe file was not downloaded: ${model}"
  actual_probe_sha="$(sha256sum "${probe_file}" | awk '{print $1}')"
  [[ "${actual_probe_sha}" == "${expected_probe_sha}" ]] || \
    die "NGC artifact checksum mismatch: ${model}"
  printf 'PASS: NGC model files and download entitlement: %s\n' "${model}"
done
unset NGC_CLI_ORG NGC_CLI_API_KEY NGC_API_KEY

curl -fsSL --retry 2 --max-time 30 "${HF_API}" -o "${check_tmp}/hf-model.json"
curl -fsSL --retry 2 --max-time 30 \
  "${HF_REPOSITORY}/resolve/${HF_REVISION}/config.json" \
  -o "${check_tmp}/hf-config.json"

python3 - "${check_tmp}/hf-model.json" "$(lock_value huggingFaceModel)" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    model = json.load(stream)

if model.get("id") != sys.argv[2]:
    raise SystemExit(f"FAIL: unexpected Hugging Face model id: {model.get('id')}")
if model.get("private"):
    raise SystemExit("FAIL: Hugging Face repository is private")
if model.get("gated") not in (False, None):
    raise SystemExit(f"FAIL: Hugging Face repository is gated: {model.get('gated')}")
print("PASS: Hugging Face model repository is public and ungated.")
PY

hf_revision="$(git ls-remote "${HF_REPOSITORY}" HEAD | awk 'NR == 1 {print $1}')"
[[ "${hf_revision}" =~ ^[0-9a-f]{40}$ ]] || \
  die "could not resolve the Hugging Face repository revision"
[[ "${hf_revision}" == "${HF_REVISION}" ]] || \
  die "Hugging Face HEAD changed; expected ${HF_REVISION}, observed ${hf_revision}; review before deployment"

printf 'PASS: Hugging Face config artifact is accessible.\n'
printf 'PASS: Hugging Face HEAD matches the validated revision gate: %s\n' "${hf_revision}"
printf 'PASS: pinned VSS images and required model artifacts are accessible for %s mode.\n' "${mode}"
printf 'No OpenShift resources, Secrets, or persistent Docker credentials were changed.\n'
