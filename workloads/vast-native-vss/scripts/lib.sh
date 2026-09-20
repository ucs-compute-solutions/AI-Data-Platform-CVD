#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPANION_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${COMPANION_DIR}/../.." && pwd)"
SOURCE_LOCK="${COMPANION_DIR}/source-lock.yaml"
CHART_DIR="${REPOSITORY_ROOT}/charts/vast-vss-app"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

load_env() {
  local env_file="$1"
  [[ -r "${env_file}" ]] || die "environment file is unavailable: ${env_file}"
  # shellcheck disable=SC1090
  source "${env_file}"
  local required=(
    CVD_EXPECTED_API CVD_EXPECTED_CONTEXT CVD_NAMESPACE CVD_CONTROL_NAMESPACE
    CVD_NIM_NAMESPACE CVD_RELEASE CVD_ROUTE_HOST CVD_SOURCE_DIR CVD_BUILD_DIR CVD_RENDER_DIR
    CVD_HELM_TIMEOUT CVD_IMAGE_PULL_SECRET CVD_RUNTIME_SECRET
    CVD_BACKEND_REPOSITORY CVD_FRONTEND_REPOSITORY
    CVD_TENANT_NAME CVD_BUCKET_OWNER CVD_VIEW_POLICY CVD_VISIBILITY_GROUP
    CVD_VMS_CONFIGMAP CVD_VMS_CONFIG_KEY CVD_VMS_HOST_PATH
    CVD_VMS_USERNAME_PATH CVD_VMS_SECRET CVD_VMS_PASSWORD_KEY
    CVD_BROKER_RESOURCE CVD_BROKER_NAME CVD_TOPIC_NAME
    CVD_DATAENGINE_CLUSTER_VRN CVD_REGISTRY_NAME CVD_REGISTRY_PUSH_HOST
    CVD_FUNCTION_IMAGE_TAG
    CVD_SEGMENTER_ARTIFACT_SOURCE CVD_REASONER_ARTIFACT_SOURCE
    CVD_EMBEDDER_ARTIFACT_SOURCE CVD_WRITER_ARTIFACT_SOURCE
    CVD_PIPELINE_NAME CVD_EMBEDDING_NIMSERVICE CVD_COSMOS_NIMSERVICE
    CVD_LLM_NIMSERVICE
  )
  local name value
  for name in "${required[@]}"; do
    value="${!name:-}"
    [[ -n "${value}" ]] || die "required input is empty: ${name}"
    [[ "${value}" != *'<'* && "${value}" != *'>'* ]] || \
      die "replace the placeholder value for ${name}"
  done
  [[ "${CVD_FUNCTION_IMAGE_TAG}" != "latest" ]] || \
    die "latest is prohibited for DataEngine function images"

  CVD_CPU_NODE_SELECTOR_KEY="${CVD_CPU_NODE_SELECTOR_KEY:-}"
  CVD_CPU_NODE_SELECTOR_VALUE="${CVD_CPU_NODE_SELECTOR_VALUE:-}"
  if [[ -n "${CVD_CPU_NODE_SELECTOR_KEY}" || -n "${CVD_CPU_NODE_SELECTOR_VALUE}" ]]; then
    [[ -n "${CVD_CPU_NODE_SELECTOR_KEY}" && -n "${CVD_CPU_NODE_SELECTOR_VALUE}" ]] || \
      die "set both CPU node-selector inputs or leave both empty"
    [[ "${CVD_CPU_NODE_SELECTOR_KEY}" =~ ^(([a-z0-9]([-a-z0-9.]*[a-z0-9])?)/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$ ]] || \
      die "CVD_CPU_NODE_SELECTOR_KEY is not a valid label key"
    [[ "${CVD_CPU_NODE_SELECTOR_VALUE}" =~ ^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$ ]] || \
      die "CVD_CPU_NODE_SELECTOR_VALUE is not a valid label value"
    CVD_NODE_SELECTOR_YAML="{\"${CVD_CPU_NODE_SELECTOR_KEY}\": \"${CVD_CPU_NODE_SELECTOR_VALUE}\"}"
  else
    CVD_NODE_SELECTOR_YAML='{}'
  fi

  CVD_BACKEND_DIGEST="${CVD_BACKEND_DIGEST:-}"
  CVD_FRONTEND_DIGEST="${CVD_FRONTEND_DIGEST:-}"
  export "${required[@]}"
  export CVD_BACKEND_DIGEST CVD_FRONTEND_DIGEST
  export CVD_CPU_NODE_SELECTOR_KEY CVD_CPU_NODE_SELECTOR_VALUE
  export CVD_NODE_SELECTOR_YAML
}

require_application_digests() {
  [[ "${CVD_BACKEND_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]] || \
    die "CVD_BACKEND_DIGEST must be an immutable sha256 digest before rendering"
  [[ "${CVD_FRONTEND_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]] || \
    die "CVD_FRONTEND_DIGEST must be an immutable sha256 digest before rendering"
}

verify_context() {
  local identity server context
  identity="$(oc whoami)"
  server="$(oc whoami --show-server)"
  context="$(oc config current-context)"
  printf 'OpenShift identity: %s\n' "${identity}"
  printf 'OpenShift API: %s\n' "${server}"
  printf 'Current kubeconfig context: %s\n' "${context}"
  [[ "${server}" == "${CVD_EXPECTED_API}" ]] || \
    die "OpenShift API mismatch: expected ${CVD_EXPECTED_API}, found ${server}"
  [[ "${context}" == "${CVD_EXPECTED_CONTEXT}" ]] || \
    die "OpenShift context mismatch: expected ${CVD_EXPECTED_CONTEXT}, found ${context}"
}

lock_value() {
  local key="$1"
  python3 - "${SOURCE_LOCK}" "${key}" <<'PY'
import sys
from pathlib import Path

path, key = sys.argv[1:]
text = Path(path).read_text(encoding="utf-8")
values = {}
stack = []
for raw_line in text.splitlines():
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        continue
    if ":" not in stripped:
        continue
    indent = len(raw_line) - len(raw_line.lstrip(" "))
    while stack and indent <= stack[-1][0]:
        stack.pop()
    name, value = stripped.split(":", 1)
    dotted = ".".join([item[1] for item in stack] + [name])
    value = value.strip()
    if value:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[dotted] = value
    else:
        stack.append((indent, name))
if key in values:
    print(values[key])
    raise SystemExit(0)
raise SystemExit(f"lock key not found: {key}")
PY
}

render_template() {
  local source="$1" destination="$2"
  python3 - "${source}" "${destination}" <<'PY'
import os
import re
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:])
text = source.read_text(encoding="utf-8")

def replace(match):
    name = match.group(1)
    value = os.environ.get(name)
    if value is None or value == "":
        raise SystemExit(f"missing template input: {name}")
    return value

rendered = re.sub(r"__([A-Z0-9_]+)__", replace, text)
if re.search(r"__[A-Z0-9_]+__", rendered):
    raise SystemExit(f"unresolved token in {source}")
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(rendered, encoding="utf-8")
PY
}

vastde_has_name() {
  local kind="$1" name="$2" output
  if ! output="$(env TERM=dumb NO_COLOR=1 vastde "${kind}" list -o yaml 2>/dev/null)"; then
    die "unable to list DataEngine ${kind}"
  fi
  printf '%s\n' "${output}" | \
    grep -Eq "^[[:space:]]*(-[[:space:]]+)?name:[[:space:]]+${name}$"
}
