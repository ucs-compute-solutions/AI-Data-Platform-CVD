#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ENV_FILE=$(env_path_from_args "$@")
load_inputs "${ENV_FILE}"

for command_name in oc helm jq grep; do
  require_command "${command_name}"
done
for value_name in \
  EXPECTED_OCP_API ZOT_NAMESPACE ZOT_RELEASE ZOT_HOST ZOT_STORAGE_CLASS \
  DATAENGINE_NAMESPACE KNATIVE_SERVING_NAMESPACE KNATIVE_EVENTING_NAMESPACE \
  ZARF_MUTATION_NAMESPACES; do
  require_value "${value_name}"
done

print_context
verify_context
mutation_namespaces >/dev/null
for namespace_name in \
  "${DATAENGINE_NAMESPACE}" \
  "${KNATIVE_SERVING_NAMESPACE}" \
  "${KNATIVE_EVENTING_NAMESPACE}"; do
  require_namespace_in_mutation_scope "${namespace_name}"
done
assert_existing_mutation_scope_is_approved

assert_namespace_pods_healthy() {
  local namespace_name=$1 pod_json
  pod_json=$(oc -n "${namespace_name}" get pods -o json)
  jq -e '
    (.items | length) > 0 and
    all(.items[];
      .metadata.deletionTimestamp == null and
      ((.status.phase == "Succeeded") or
       (.status.phase == "Running" and
        any(.status.conditions[]?; .type == "Ready" and .status == "True"))) and
      all(((.status.initContainerStatuses // []) + (.status.containerStatuses // []))[]?;
        ((.state.waiting.reason // "") |
         test("^(CrashLoopBackOff|ErrImagePull|ImagePullBackOff|CreateContainer.*Error)$") | not)))
  ' <<< "${pod_json}" >/dev/null || \
    die "namespace/${namespace_name} has no pods or contains an unready/failed pod"
}

assert_namespace_workloads_available() {
  local namespace_name=$1 deployment_json statefulset_json
  deployment_json=$(oc -n "${namespace_name}" get deployments -o json)
  statefulset_json=$(oc -n "${namespace_name}" get statefulsets -o json)
  [[ "$(jq '(.items | length)' <<< "${deployment_json}")" -gt 0 || \
     "$(jq '(.items | length)' <<< "${statefulset_json}")" -gt 0 ]] || \
    die "namespace/${namespace_name} contains no Deployment or StatefulSet"
  jq -e '
    all(.items[];
      ((.spec.replicas // 1) == 0) or
      ((.status.availableReplicas // 0) >= (.spec.replicas // 1)))
  ' <<< "${deployment_json}" >/dev/null || \
    die "namespace/${namespace_name} contains an unavailable Deployment"
  jq -e '
    all(.items[];
      ((.spec.replicas // 1) == 0) or
      ((.status.readyReplicas // 0) >= (.spec.replicas // 1)))
  ' <<< "${statefulset_json}" >/dev/null || \
    die "namespace/${namespace_name} contains an unready StatefulSet"
}

printf '\nZot registry\n'
oc -n "${ZOT_NAMESPACE}" get pods,pvc,route -o wide
oc -n "${ZOT_NAMESPACE}" get pvc \
  -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,STORAGECLASS:.spec.storageClassName,CAPACITY:.status.capacity.storage'
oc -n "${ZOT_NAMESPACE}" get pvc -o json | jq -e --arg storage_class "${ZOT_STORAGE_CLASS}" '
  (.items | length) > 0 and
  all(.items[]; .status.phase == "Bound" and .spec.storageClassName == $storage_class)
' >/dev/null || die "Zot PVC is absent, unbound, or uses an unexpected StorageClass"
oc -n "${ZOT_NAMESPACE}" get routes -o json | jq -e --arg host "${ZOT_HOST}" '
  any(.items[];
    .spec.host == $host and
    any(.status.ingress[]?.conditions[]?; .type == "Admitted" and .status == "True"))
' >/dev/null || die "Zot Route ${ZOT_HOST} is absent or not admitted"
oc -n "${ZOT_NAMESPACE}" get services -o json | jq -e --arg release "${ZOT_RELEASE}" '
  any(.items[];
    .metadata.labels["app.kubernetes.io/instance"] == $release and
    .spec.type == "NodePort")
' >/dev/null || die "Zot does not expose the validated NodePort service"
assert_namespace_pods_healthy "${ZOT_NAMESPACE}"

printf '\nNamespace mutation labels\n'
while IFS= read -r namespace_name; do
  oc get namespace "${namespace_name}" -L zarf.dev/agent -L zarf.dev/vast
  [[ "$(oc get namespace "${namespace_name}" -o jsonpath='{.metadata.labels.zarf\.dev/agent}')" == "mutate" ]] || \
    die "namespace/${namespace_name} is missing zarf.dev/agent=mutate"
  [[ "$(oc get namespace "${namespace_name}" -o jsonpath='{.metadata.labels.zarf\.dev/vast}')" == "mutate" ]] || \
    die "namespace/${namespace_name} is missing zarf.dev/vast=mutate"
done < <(mutation_namespaces)

printf '\nDataEngine and Knative resources\n'
oc -n "${DATAENGINE_NAMESPACE}" get deployments,statefulsets,pods -o wide
oc -n "${KNATIVE_SERVING_NAMESPACE}" get deployments,pods -o wide
oc -n "${KNATIVE_EVENTING_NAMESPACE}" get deployments,pods -o wide
for namespace_name in \
  "${DATAENGINE_NAMESPACE}" \
  "${KNATIVE_SERVING_NAMESPACE}" \
  "${KNATIVE_EVENTING_NAMESPACE}"; do
  assert_namespace_pods_healthy "${namespace_name}"
  assert_namespace_workloads_available "${namespace_name}"
done

printf '\nHelm releases\n'
helm -n "${DATAENGINE_NAMESPACE}" list

printf '\nContainer image references\n'
for namespace_name in \
  "${DATAENGINE_NAMESPACE}" \
  "${KNATIVE_SERVING_NAMESPACE}" \
  "${KNATIVE_EVENTING_NAMESPACE}"; do
  printf '%s\n' "[${namespace_name}]"
  oc -n "${namespace_name}" get pods \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{range .spec.initContainers[*]}  init: {.image}{"\n"}{end}{range .spec.containers[*]}  container: {.image}{"\n"}{end}{end}'
done

for namespace_name in \
  "${DATAENGINE_NAMESPACE}" \
  "${KNATIVE_SERVING_NAMESPACE}" \
  "${KNATIVE_EVENTING_NAMESPACE}"; do
  if oc -n "${namespace_name}" get pods \
    -o jsonpath='{range .items[*]}{range .spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' \
    | grep -Eq '(^|[./])[^ ]*\.ecr\.|amazonaws\.com'; then
    die "namespace/${namespace_name} still references an external ECR image; verify mutation and redeploy"
  fi
done

printf '\nRecent Warning events\n'
for namespace_name in \
  "${ZOT_NAMESPACE}" \
  "${DATAENGINE_NAMESPACE}" \
  "${KNATIVE_SERVING_NAMESPACE}" \
  "${KNATIVE_EVENTING_NAMESPACE}"; do
  printf '%s\n' "[${namespace_name}]"
  oc -n "${namespace_name}" get events \
    --field-selector type=Warning \
    --sort-by=.lastTimestamp || true
done

if command -v vastde >/dev/null 2>&1; then
  printf '\nVAST DataEngine client\n'
  vastde version
  vastde functions list
  vastde triggers list
  vastde pipelines list
else
  printf '\nNOTICE: vastde is not installed or configured on this host; run the client checks from an approved administration host.\n'
fi

printf '\nPASS: Zot storage/route, scoped mutation labels, pods, workloads, and image-reference checks passed.\n'
printf 'FINAL GATE: confirm VAST UI enablement and run one new pinned-image function or pipeline.\n'
