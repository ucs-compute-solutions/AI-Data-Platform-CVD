# NVIDIA Warehouse OpenShift Helm Skeleton

Status: **render-only, incomplete, and not approved for deployment**

This chart is a namespace-scoped translation skeleton for the NVIDIA
VSS 3.2.1 Warehouse 2D profile. It records pinned upstream image tags, Service
DNS names, stateful storage shapes, restricted security defaults, namespace-
only SDR observation RBAC, and disabled GPU workload candidates. It does not
claim that the NVIDIA Docker Compose profile is deployable on OpenShift.

The chart is inert with its checked-in values:

- `global.enabled=false` renders no Kubernetes resources.
- Enabling the global gate also requires
  `global.acknowledgeIncomplete=true`.
- Each CPU, stateful, and GPU component remains independently disabled.
- Any GPU component additionally requires
  `global.allowGpuPlaceholders=true`.
- Any enabled component without an immutable digest additionally requires
  `global.allowTagOnlyRender=true`; this is for local render inspection only
  and must not be used for installation.
- No Secret, credential, Namespace, SCC binding, hostPath, host network, host
  IPC, Docker socket, CRI socket, or cluster-scoped object is rendered.
- ConfigMap, Job, PVC, Route, and NetworkPolicy entries are present only as
  individually disabled scaffolding. Missing source data, helper images,
  credentials, hostnames, and policy selectors fail rendering when an
  incomplete entry is enabled.

## Source lock

| Input | Locked value |
|---|---|
| NVIDIA VSS tag | `v3.2.1` |
| Source commit | `7640d917047cf7b0fd3085eefb8282754b56bc94` |
| Release metadata | `3.2.1-26.07.1` |
| Warehouse dataset metadata | `vss-warehouse-app-data:3.2.0` |
| Intended namespace | `nvidia-vss-321-warehouse` |
| Target Kubernetes API level | `1.33.9` |

The mixed 3.2.0 and 3.2.1 component tags in `values.yaml` are intentional and
match `../source-lock.yaml`. Eighteen chart images now use the immutable
manifest or accepted-runtime digests recorded there. Alert Bridge,
Configurator, and Nemotron Nano remain unresolved. The template
helper rejects `latest`, and any enabled unresolved image remains a deployment
blocker even though its tag is pinned.

See
[`NGC-IMAGE-RESOLUTION.md`](../../../research/NGC-IMAGE-RESOLUTION.md) for the
pinned-source evidence and credential-safe resolution procedure for the
protected artifacts.

## Local render checks

Run these commands from this chart directory. They do not contact a cluster:

```bash
helm lint .
helm template warehouse . \
  --namespace nvidia-vss-321-warehouse \
  --kube-version 1.33.9
```

The default template output should contain no Kubernetes manifests. To inspect
one CPU/stateful template locally, explicitly open the two render gates and one
component gate:

```bash
helm template warehouse . \
  --namespace nvidia-vss-321-warehouse \
  --kube-version 1.33.9 \
  --set global.enabled=true \
  --set global.acknowledgeIncomplete=true \
  --set statefulServices.kafka.enabled=true
```

GPU template inspection has a third acknowledgement gate:

```bash
helm template warehouse . \
  --namespace nvidia-vss-321-warehouse \
  --kube-version 1.33.9 \
  --set global.enabled=true \
  --set global.acknowledgeIncomplete=true \
  --set global.allowGpuPlaceholders=true \
  --set configMaps.deepstream2d.enabled=true \
  --set-string configMaps.deepstream2d.data.placeholder=render-only \
  --set persistentVolumeClaims.perceptionModels.enabled=true \
  --set gpuWorkloads.rtviCv.enabled=true
```

These are rendering examples, not installation commands.

## Included scaffolding

- StatefulSet and ClusterIP/headless Service templates for Kafka, Redis,
  Elasticsearch, and VIOS PostgreSQL.
- Deployment and optional ClusterIP Service templates for the configurator,
  Kibana, Logstash, behavior analytics, video analytics API, Alert Bridge,
  VST ingress, SDR controller, VA MCP, Agent, UI, and Phoenix.
- Dedicated ServiceAccounts plus namespace-only `get`, `list`, and `watch`
  RBAC for the SDR controller. The chart grants no SCC.
- Disabled GPU/NVIDIA-runtime candidates for RT-CV, RTVI-VLM, Nemotron Nano,
  NvStreamer, VIOS stream processing, and VIOS sensor.
- Enabled GPU controllers render in a zero-replica standby state. Their
  separate `activeReplicas` values are copied into the reviewed mode lock; the
  chart itself does not start a GPU workload.
- A source/DNS ConfigMap containing only non-secret lock metadata and
  namespace-local endpoint names.
- Disabled ConfigMap groups tied to exact Warehouse 2D source paths. They fail
  rendering until reviewed data from the locked commit is supplied.
- Disabled retained VAST CSI PVCs for VIOS media/data, clips, model caches,
  analytics files, reports, and Phoenix state. Provisional capacities remain
  explicitly marked.
- Disabled, bounded initialization Job shapes for Kafka topics, broker gates,
  Elasticsearch initialization, Kibana dashboard import, and calibration
  import. Every Job remains blocked until its exact script or derived image is
  pinned and its status is changed to `render-ready`.
- Disabled edge Routes for the UI, Agent browser paths, Alert Bridge, VST,
  Video Analytics API, Kibana, and the separate NvStreamer host. A non-empty
  reviewed hostname is mandatory before any Route renders.
- Disabled NetworkPolicy scaffolding for default deny, release-local traffic,
  DNS, and OpenShift router ingress. DNS and router selectors are deliberately
  unset; enabling NetworkPolicy requires a second acknowledgement.
- Existing-Secret references for NGC and VST adaptor credentials. Additional
  external-provider credentials are not yet wired into this local profile;
  Secret objects and values are never generated or stored by this chart.
- GPU startup/readiness probes taken from documented upstream endpoints where
  available, with TCP placeholders clearly retained for services whose source
  has no HTTP readiness endpoint.

Platform telemetry agents from Compose are intentionally omitted. OpenShift
and GPU Operator telemetry are the intended replacement for cAdvisor,
node-exporter, Prometheus, Grafana, and DCGM exporter in this first design.

## Gaps that block deployment

1. Resolve the three remaining chart images to immutable digests. Build, pin,
   and publish the source-defined Elasticsearch and init/helper images; pin
   `jq` and the Logstash protobuf codec inputs.
2. Populate the disabled configuration groups with exact reviewed files from
   the pinned source. Build and pin the helper images, then replace each Job's
   blocked status, command, and arguments. The endpoint ConfigMap is not an
   application configuration substitute.
3. Supply separately approved existing Secret names for NGC access and VST
   adaptor credentials. This chart must never embed their values.
4. Measure and approve CPU/memory requests, PVC capacity, access modes,
   probes, writable paths, and UID/SCC requirements for every image.
5. Resolve the GPU design. NvStreamer shares device 0 with RT-CV in Compose;
   VIOS stream processing and sensor have unresolved runtime requirements.
   The placeholder map currently exposes five candidate GPU requests without
   asserting that those allocations are correct. The site policy permits an
   accepted one-through-eight-GPU allocation, but additional or consolidated
   controllers/replicas must come from the final reviewed render.
6. Complete and validate the disabled NetworkPolicies and OpenShift Routes,
   including path rewrites, WebSocket/SSE timeouts, media range requests, and
   browser-origin behavior.
7. Prove StatefulSet restart/retention behavior on VAST CSI and run API
   admission and SCC tests on Kubernetes 1.33.9. NVIDIA documents a newer
   Kubernetes level for its developer Helm profiles and supplies no integrated
   Warehouse Helm profile.
8. Add complete alert-flow tests: RT-CV event, behavior rule, VLM verification,
   Alert API evidence, UI display, Agent report, and repeatability. Worker-fall
   detection is not present in the locked source.

The Warehouse namespace and release must remain isolated from the accepted
NVIDIA Search and VAST-native VSS lanes. This skeleton contains no switch,
scale, patch, or deletion action for either base demo; an independently
reviewed mode-switch runbook is required before any GPU scheduling change.

## Design authorities

- [`../source-lock.yaml`](../source-lock.yaml)
- [`../../../research/NVIDIA-SOURCE-INVENTORY.md`](../../../research/NVIDIA-SOURCE-INVENTORY.md)
- [`../../../research/OPENSHIFT-PORTING-MATRIX.md`](../../../research/OPENSHIFT-PORTING-MATRIX.md)
