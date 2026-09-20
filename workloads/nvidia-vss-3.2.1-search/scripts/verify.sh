#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

env_file=""
mode=""
while (( $# )); do
  case "$1" in
    --env) env_file="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; shift 2 ;;
    *) die "usage: $0 --env <release-inputs.env> --mode initial|steady|critic" ;;
  esac
done
[[ -n "${env_file}" ]] || die "usage: $0 --env <release-inputs.env> --mode initial|steady|critic"
[[ "${mode}" =~ ^(initial|steady|critic)$ ]] || die "mode must be initial, steady, or critic"

for command_name in oc helm awk comm cut python3 sed sort; do require_command "${command_name}"; done
load_env "${env_file}"
verify_context

release_status="$(helm -n "${CVD_NAMESPACE}" status "${CVD_RELEASE}" -o json | \
  python3 -c 'import json, sys; print(json.load(sys.stdin)["info"]["status"])')"
[[ "${release_status}" == "deployed" ]] || die "Helm release status is ${release_status}, not deployed"

assert_exact_names() {
  local kind="$1"
  local expected="$2"
  local actual differences
  actual="$(oc -n "${CVD_NAMESPACE}" get "${kind}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)"
  differences="$(comm -3 \
    <(printf '%s\n' "${expected}" | sed '/^$/d' | sort) \
    <(printf '%s\n' "${actual}" | sed '/^$/d' | sort))"
  [[ -z "${differences}" ]] || die "${kind} inventory differs from the validated profile: ${differences}"
}

expected_deployments=$'kibana\nlogstash\nphoenix\nsdrc\nvss-agent\nvss-agent-ui\nvss-behavior-analytics\nvss-rtvi-embed\nvss-video-analytics-api\nvss-vios-ingress\nvss-vios-nvstreamer\nvss-vios-sensor'
expected_statefulsets=$'elasticsearch\nkafka\nredis\nvss-rtvi-cv\nvss-vios-postgres\nvss-vios-streamprocessing'
expected_services=$'elasticsearch\nelasticsearch-headless\nkafka-kafka\nkafka-kafka-headless\nkibana\nphoenix\nredis\nredis-headless\nsdrc-controller\nsdrc-direct-listener\nsdrc-envoy-admin\nvss-agent\nvss-agent-ui\nvss-behavior-analytics\nvss-rtvi-cv\nvss-rtvi-cv-headless\nvss-rtvi-embed\nvss-video-analytics-api\nvss-vios-ingress\nvss-vios-nvstreamer\nvss-vios-postgres\nvss-vios-postgres-headless\nvss-vios-sensor\nvss-vios-streamprocessing\nvss-vios-streamprocessing-headless'
if [[ "${mode}" == "critic" ]]; then
  expected_deployments+=$'\nnvidia-cosmos3-reasoner'
  expected_services+=$'\nnvidia-cosmos3-reasoner'
fi
assert_exact_names deployment "${expected_deployments}"
assert_exact_names statefulset "${expected_statefulsets}"
assert_exact_names service "${expected_services}"

while IFS= read -r deployment; do
  [[ -n "${deployment}" ]] || continue
  desired="$(oc -n "${CVD_NAMESPACE}" get deployment "${deployment}" -o jsonpath='{.spec.replicas}')"
  ready="$(oc -n "${CVD_NAMESPACE}" get deployment "${deployment}" -o jsonpath='{.status.readyReplicas}')"
  available="$(oc -n "${CVD_NAMESPACE}" get deployment "${deployment}" -o jsonpath='{.status.availableReplicas}')"
  ready="${ready:-0}"
  available="${available:-0}"
  [[ "${desired}" == "${ready}" && "${desired}" == "${available}" && "${desired}" != "0" ]] || \
    die "deployment/${deployment} is not fully Ready: ${desired}|${ready}|${available}"
done <<<"${expected_deployments}"

while IFS= read -r statefulset; do
  [[ -n "${statefulset}" ]] || continue
  desired="$(oc -n "${CVD_NAMESPACE}" get statefulset "${statefulset}" -o jsonpath='{.spec.replicas}')"
  current="$(oc -n "${CVD_NAMESPACE}" get statefulset "${statefulset}" -o jsonpath='{.status.currentReplicas}')"
  ready="$(oc -n "${CVD_NAMESPACE}" get statefulset "${statefulset}" -o jsonpath='{.status.readyReplicas}')"
  current="${current:-0}"
  ready="${ready:-0}"
  if [[ "${statefulset}" == "vss-rtvi-cv" && "${mode}" == "initial" ]]; then
    [[ "${desired}|${current}|${ready}" == "0|0|0" ]] || \
      die "initial mode requires statefulset/vss-rtvi-cv at 0 replicas: ${desired}|${current}|${ready}"
  else
    [[ "${desired}" == "${current}" && "${desired}" == "${ready}" && "${desired}" != "0" ]] || \
      die "statefulset/${statefulset} is not fully Ready: ${desired}|${current}|${ready}"
  fi
done <<<"${expected_statefulsets}"

expected_pvcs=$'data-redis-0|5Gi|ReadWriteOnce\nes-data-elasticsearch-0|100Gi|ReadWriteOnce\nes-logs-elasticsearch-0|20Gi|ReadWriteOnce\nkafka-data-kafka-0|50Gi|ReadWriteOnce\nlogstash-logstash-libs|5Gi|ReadWriteOnce\n'
expected_pvcs+="${CVD_RELEASE}-vst-data|10Gi|ReadWriteMany"$'\n'
expected_pvcs+="${CVD_RELEASE}-vst-streamer-videos|20Gi|ReadWriteMany"$'\n'
expected_pvcs+="${CVD_RELEASE}-vst-video|20Gi|ReadWriteMany"$'\n'
expected_pvcs+=$'phoenix-data|10Gi|ReadWriteOnce\nvss-rtvi-cv-models|50Gi|ReadWriteOnce\nvss-rtvi-embed-rtvi-hf-cache|50Gi|ReadWriteOnce\nvss-rtvi-embed-rtvi-ngc-cache|50Gi|ReadWriteOnce\nvss-video-analytics-api-files|5Gi|ReadWriteOnce\nvss-vios-postgres-data|10Gi|ReadWriteOnce'
if [[ "${mode}" == "critic" ]]; then
  expected_pvcs+=$'\nnvidia-cosmos3-reasoner-pvc|200Gi|ReadWriteOnce'
fi
pvc_inventory_file="$(mktemp)"
oc -n "${CVD_NAMESPACE}" get pvc -o json >"${pvc_inventory_file}"
python3 - "${pvc_inventory_file}" "${CVD_STORAGE_CLASS}" "${mode}" "${expected_pvcs}" <<'PY'
import json
import sys

path, storage_class, mode, expected_text = sys.argv[1:]
items = json.load(open(path, encoding="utf-8")).get("items", [])
expected = {}
for line in expected_text.splitlines():
    name, size, access_mode = line.split("|")
    expected[name] = (size, access_mode)

actual = {item.get("metadata", {}).get("name", ""): item for item in items}
retained_critic_cache = "nvidia-cosmos3-reasoner-pvc"
allowed = set(expected)
if mode == "steady" and retained_critic_cache in actual:
    allowed.add(retained_critic_cache)
    expected[retained_critic_cache] = ("200Gi", "ReadWriteOnce")

missing = sorted(set(expected) - set(actual))
extra = sorted(set(actual) - allowed)
errors = []
if missing:
    errors.append(f"missing PVCs: {', '.join(missing)}")
if extra:
    errors.append(f"unexpected PVCs: {', '.join(extra)}")
for name in sorted(set(actual) & set(expected)):
    item = actual[name]
    phase = item.get("status", {}).get("phase", "")
    spec = item.get("spec", {})
    observed_class = spec.get("storageClassName", "")
    observed_size = spec.get("resources", {}).get("requests", {}).get("storage", "")
    access_modes = spec.get("accessModes", [])
    observed_access = access_modes[0] if len(access_modes) == 1 else ",".join(access_modes)
    expected_size, expected_access = expected[name]
    observed = (phase, observed_class, observed_size, observed_access)
    wanted = ("Bound", storage_class, expected_size, expected_access)
    if observed != wanted:
        errors.append(f"{name}: observed {observed}, expected {wanted}")
if errors:
    raise SystemExit("ERROR: PVC contract failed: " + "; ".join(errors))
print(f"PVC contract: {len(expected)} validated claims ({mode} mode)")
PY
rm -f -- "${pvc_inventory_file}"
model_pvc_contract="$(oc -n "${CVD_NAMESPACE}" get pvc vss-rtvi-cv-models \
  -o jsonpath='{.status.phase}|{.spec.storageClassName}|{.spec.accessModes[0]}|{.metadata.annotations.cvd\.cisco\.com/rt-cv-models-populated}')"
[[ "${model_pvc_contract}" == "Bound|${CVD_STORAGE_CLASS}|ReadWriteOnce|true" ]] || \
  die "RT-CV model PVC contract failed: ${model_pvc_contract}"

route_contracts=$'vss-ui||vss-agent-ui\nvss-ui-chat-api|/api/chat|vss-agent-ui\nvss-agent-api|/api|vss-agent\nvss-agent-chat|/chat|vss-agent\nvss-agent-websocket|/websocket|vss-agent\nvss-agent-static|/static|vss-agent\nvss-vst|/vst|vss-vios-ingress\nvss-video-analytics-api|/video-analytics-api|vss-video-analytics-api\nvss-streamer||vss-vios-nvstreamer'
expected_route_names="$(printf '%s\n' "${route_contracts}" | cut -d'|' -f1 | sort)"
actual_route_names="$(oc -n "${CVD_NAMESPACE}" get route \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)"
[[ "${actual_route_names}" == "${expected_route_names}" ]] || \
  die "Route inventory differs from the validated nine-Route contract"
while IFS='|' read -r route_name route_path service_name; do
  expected_host="${CVD_ROUTE_HOST}"
  [[ "${route_name}" == "vss-streamer" ]] && expected_host="${CVD_STREAMER_ROUTE_HOST}"
  route_contract="$(oc -n "${CVD_NAMESPACE}" get route "${route_name}" \
    -o jsonpath='{.spec.host}|{.spec.path}|{.spec.to.name}|{.spec.tls.termination}|{.spec.tls.insecureEdgeTerminationPolicy}|{.metadata.annotations.haproxy\.router\.openshift\.io/timeout}|{.status.ingress[0].conditions[?(@.type=="Admitted")].status}')"
  [[ "${route_contract}" == "${expected_host}|${route_path}|${service_name}|edge|Redirect|3600s|True" ]] || \
    die "route/${route_name} contract failed: ${route_contract}"
done <<<"${route_contracts}"
rewrite_target="$(oc -n "${CVD_NAMESPACE}" get route vss-video-analytics-api \
  -o jsonpath='{.metadata.annotations.haproxy\.router\.openshift\.io/rewrite-target}')"
[[ "${rewrite_target}" == "/" ]] || die "video analytics Route rewrite target is not /"

endpoint_services=$'elasticsearch\nelasticsearch-headless\nkafka-kafka\nkafka-kafka-headless\nkibana\nphoenix\nredis\nredis-headless\nsdrc-controller\nsdrc-direct-listener\nsdrc-envoy-admin\nvss-agent\nvss-agent-ui\nvss-behavior-analytics\nvss-rtvi-embed\nvss-video-analytics-api\nvss-vios-ingress\nvss-vios-nvstreamer\nvss-vios-postgres\nvss-vios-postgres-headless\nvss-vios-sensor\nvss-vios-streamprocessing\nvss-vios-streamprocessing-headless'
if [[ "${mode}" != "initial" ]]; then
  endpoint_services+=$'\nvss-rtvi-cv\nvss-rtvi-cv-headless'
fi
while IFS= read -r service_name; do
  [[ -n "${service_name}" ]] || continue
  endpoint_count="$(oc -n "${CVD_NAMESPACE}" get endpoints "${service_name}" \
    -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{"\n"}{end}' | sed '/^$/d' | awk 'END {print NR+0}')"
  [[ "${endpoint_count}" -gt 0 ]] || die "service/${service_name} has no ready endpoint"
done <<<"${endpoint_services}"

if [[ "${mode}" == "critic" ]]; then
  endpoint_count="$(oc -n "${CVD_NAMESPACE}" get endpoints nvidia-cosmos3-reasoner \
    -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{"\n"}{end}' | \
    sed '/^$/d' | awk 'END {print NR+0}')"
  [[ "${endpoint_count}" -gt 0 ]] || die "service/nvidia-cosmos3-reasoner has no ready endpoint"
fi

oc -n "${CVD_NAMESPACE}" exec -i deployment/vss-agent -- \
  env "CVD_LLM_BASE_URL=${CVD_LLM_BASE_URL}" "CVD_LLM_MODEL=${CVD_LLM_MODEL}" \
  python3 - <<'PY'
import json
import os
import urllib.request

base = os.environ["CVD_LLM_BASE_URL"]
api = f"{base}/v1"
model = os.environ["CVD_LLM_MODEL"]

with urllib.request.urlopen(f"{api}/models", timeout=30) as response:
    models = json.load(response)
model_ids = {item.get("id") for item in models.get("data", [])}
if model not in model_ids:
    raise SystemExit(f"expected LLM model is absent from /v1/models: {model}")

marker = "CVD-VSS-VERIFY-314159"
payload = json.dumps({
    "model": model,
    "messages": [{"role": "user", "content": f"Return only this token: {marker}"}],
    "temperature": 0,
    "max_tokens": 128,
    "stream": False,
    "chat_template_kwargs": {"enable_thinking": False},
}).encode("utf-8")
request = urllib.request.Request(
    f"{api}/chat/completions",
    data=payload,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=180) as response:
    completion = json.load(response)
answer = completion["choices"][0]["message"]["content"]
if marker not in answer:
    raise SystemExit("LLM marker is absent from the completion")
print(f"PASS: shared LLM model discovery and marker completion: {model}")
PY

if [[ "${mode}" == "initial" ]]; then
  printf 'NOTE: the transient downloader Job may be absent after its 3600-second TTL; the retained PVC annotation records its verified completion.\n'
else
  if oc -n "${CVD_NAMESPACE}" get job vss-rtvi-cv-download-models >/dev/null 2>&1; then
    die "RT-CV model downloader Job remains in ${mode} mode"
  fi
fi

if [[ "${mode}" == "critic" ]]; then
  oc -n "${CVD_NAMESPACE}" wait \
    --for=jsonpath='{.status.state}'=Ready nimcache/nvidia-cosmos3-reasoner \
    --timeout="${CVD_HELM_TIMEOUT}"
  oc -n "${CVD_NAMESPACE}" wait \
    --for=jsonpath='{.status.state}'=Ready nimservice/nvidia-cosmos3-reasoner \
    --timeout="${CVD_HELM_TIMEOUT}"
  oc -n "${CVD_NAMESPACE}" rollout status deployment/nvidia-cosmos3-reasoner \
    --timeout="${CVD_HELM_TIMEOUT}"
  nimcache_contract="$(oc -n "${CVD_NAMESPACE}" get nimcache nvidia-cosmos3-reasoner \
    -o jsonpath='{.status.state}|{.status.pvc}|{.status.conditions[?(@.type=="NIM_CACHE_JOB_COMPLETED")].status}')"
  [[ "${nimcache_contract}" == "Ready|nvidia-cosmos3-reasoner-pvc|True" ]] || \
    die "Cosmos3 NIMCache is not Ready: ${nimcache_contract}"
  nimservice_contract="$(oc -n "${CVD_NAMESPACE}" get nimservice nvidia-cosmos3-reasoner \
    -o jsonpath='{.status.state}|{.spec.replicas}|{.status.conditions[?(@.type=="Ready")].status}')"
  [[ "${nimservice_contract}" == "Ready|1|True" ]] || \
    die "Cosmos3 NIMService is not Ready: ${nimservice_contract}"
  cosmos_deployment="$(oc -n "${CVD_NAMESPACE}" get deployment nvidia-cosmos3-reasoner \
    -o jsonpath='{.spec.replicas}|{.status.readyReplicas}|{.status.availableReplicas}')"
  [[ "${cosmos_deployment}" == "1|1|1" ]] || die "Cosmos3 deployment is not Ready: ${cosmos_deployment}"
  cosmos_pvc="$(oc -n "${CVD_NAMESPACE}" get pvc nvidia-cosmos3-reasoner-pvc \
    -o jsonpath='{.status.phase}|{.spec.storageClassName}|{.spec.accessModes[0]}')"
  [[ "${cosmos_pvc}" == "Bound|${CVD_STORAGE_CLASS}|ReadWriteOnce" ]] || \
    die "Cosmos3 model PVC contract failed: ${cosmos_pvc}"
  cosmos_service="$(oc -n "${CVD_NAMESPACE}" get service nvidia-cosmos3-reasoner \
    -o jsonpath='{.metadata.name}|{.spec.ports[0].port}')"
  [[ "${cosmos_service}" == "nvidia-cosmos3-reasoner|8000" ]] || \
    die "Cosmos3 Service contract failed: ${cosmos_service}"
  cosmos_endpoint_count="$(oc -n "${CVD_NAMESPACE}" get endpoints nvidia-cosmos3-reasoner \
    -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{"\n"}{end}' | \
    sed '/^$/d' | awk 'END {print NR+0}')"
  [[ "${cosmos_endpoint_count}" -gt 0 ]] || \
    die "Cosmos3 Service has no ready endpoint"
else
  if oc -n "${CVD_NAMESPACE}" get nimcache nvidia-cosmos3-reasoner >/dev/null 2>&1 || \
     oc -n "${CVD_NAMESPACE}" get nimservice nvidia-cosmos3-reasoner >/dev/null 2>&1; then
    die "Cosmos3 critic resources are present while ${mode} mode was requested"
  fi
fi

helm -n "${CVD_NAMESPACE}" list
oc -n "${CVD_NAMESPACE}" get deployments,statefulsets,pods,pvc,services,routes -o wide
oc -n "${CVD_NAMESPACE}" get events --field-selector type=Warning --sort-by=.lastTimestamp

printf 'PASS: %s-mode Helm status, exact workload inventory, readiness, PVCs, service endpoints, and Routes are verified.\n' "${mode}"
printf 'Platform verification is complete; run the fresh-MP4 functional acceptance described in the CVD section.\n'
