# NVIDIA Warehouse 3.2.1 OpenShift Porting Matrix

Status: render design only; no Warehouse manifest has been created or applied

Target namespace: `nvidia-vss-321-warehouse`

Source lock: NVIDIA VSS tag `v3.2.1`, commit `7640d917047cf7b0fd3085eefb8282754b56bc94`
Resolved profile: `BP_PROFILE=bp_wh`, `MODE=2d`, `STREAM_TYPE=kafka`, `HARDWARE_PROFILE=RTXPRO6000BW`, local `nvidia/nvidia-nemotron-nano-9b-v2`, and `nv-warehouse-4cams`

## Decision summary

The Warehouse Compose profile is not ready for deployment unchanged. The first
OpenShift render must make these deliberate adaptations:

1. Replace host networking and every `localhost`/host-IP dependency with a
   namespace-local Service DNS name.
2. Reuse the existing NVIDIA Search Kubernetes-native SDR pattern: a Deployment,
   ConfigMap, dedicated ServiceAccount, and read-only `get/list/watch` access to
   Pods, Deployments, and StatefulSets. Do not mount a Docker or CRI socket.
3. Convert one-shot Compose classes to Helm-rendered ConfigMaps, init containers,
   or Jobs. Kubernetes probes and bounded Jobs replace `depends_on` conditions.
4. Use `restricted-v2` by default. Bind `anyuid` only to dedicated ServiceAccounts
   for images proven to require root or a fixed UID. Do not grant it to the
   namespace `default` ServiceAccount.
5. Use VAST CSI PVCs with `Retain` behavior for stateful data. Use RWX only for
   VIOS paths mounted by workloads on different nodes; use RWO for single-writer
   databases and model caches.
6. Stop before deployment until the GPU audit resolves NvStreamer/RT-CV sharing
   and VIOS stream-processing. The stock three-GPU estimate is not yet a valid
   Kubernetes allocation.

The existing Search deployment is the OpenShift pattern, not a mutable dependency.
It demonstrates admitted Routes, VAST CSI RWO/RWX claims, a Kubernetes-aware SDR
controller, narrowly scoped SCC use, and the RT-CV `/usr/lib64` driver correction.
Warehouse receives separate Services, Routes, PVCs, indices, topics, and state.

## Conventions

- All DNS names below are short names in `nvidia-vss-321-warehouse`; append
  `.nvidia-vss-321-warehouse.svc` for a cross-namespace caller.
- `D`, `SS`, `J`, `CM`, and `SA` mean Deployment, StatefulSet, Job, ConfigMap,
  and ServiceAccount.
- PVC sizes are initial design values based on the accepted Search deployment or
  a source-declared capacity. They require a storage/quota review before render.
- `CPU TBD` means the Compose source specifies no CPU/memory request or limit.
  The render must still supply measured requests and limits before admission.
- No internal Kafka, Redis, PostgreSQL, Elasticsearch, model, or SDR control
  Service receives a Route.

## Porting matrix

### Data, broker, and analytics plane

| Compose class | Intended OpenShift object, Service DNS, and Route | PVC / access | SCC and security | CPU, memory, GPU | Probe and startup concern | Validation gate |
|---|---|---|---|---|---|---|
| `kafka` | `SS/warehouse-kafka`; `warehouse-kafka:9092`; headless peer Service; no Route | 50 GiB RWO, `vastdata-filesystem`, retained | `restricted-v2`; non-root image path must be proven; no host network | CPU; at least 8 GiB memory because source sets a 6 GiB heap; 0 GPU | Startup/readiness: `kafka-topics --bootstrap-server warehouse-kafka:9092 --list`; replace controller voter `localhost` with Pod DNS | Broker Ready; restart retains offsets/topics; 10 MiB message accepted; no advertised host IP |
| `kafka-topic-init-container` | Idempotent `J/warehouse-kafka-topics`; no Service/Route | None | `restricted-v2` | Small CPU/memory; 0 GPU | Starts only after Kafka readiness; creates the 21 pinned `mdx-*` topics; bounded retries | Re-run is harmless; exact partitions, retention, and topic set match source |
| `redis` | `SS/warehouse-redis`; `warehouse-redis:6379`; headless Service; no Route | 5 GiB RWO retained, including data and logs | `restricted-v2`; read-only ConfigMap for `redis.conf` | CPU TBD; 0 GPU | TCP/PING readiness and liveness | Data survives restart; SDR/VIOS clients use Service DNS, not `localhost` |
| `broker-health-check` | Fold into bounded init containers or `J/warehouse-broker-gate`; no Service/Route | None | `restricted-v2` | Small CPU/memory; 0 GPU | Check Kafka DNS and all required topics; never sleep indefinitely | Completion is required before perception, behavior, Logstash, VLM, and Alert Bridge start |
| `elasticsearch` | `SS/warehouse-elasticsearch`; `warehouse-elasticsearch:9200`, peer `:9300`; no Route | 100 GiB data + 20 GiB logs, RWO retained | Dedicated SA; prefer `restricted-v2`; validate UID/fsGroup and kernel requirements before considering `anyuid` | CPU; at least 2 GiB memory for source `-Xmx1g`; 0 GPU | Startup/readiness uses `_cluster/health` and `yellow`, with a long initial delay | Index/template creation works; data survives restart; no bootstrap-check or mmap failure |
| `elasticsearch-init-container` | Idempotent `J/warehouse-elasticsearch-init`; no Service/Route | None | Same non-secret CM inputs; `restricted-v2` | Small CPU/memory; 0 GPU | Wait for ES health, then create ILM policy, templates, and ingest pipelines | Re-run causes no destructive replacement; expected warehouse indices/templates exist |
| `kibana` | `D/warehouse-kibana`; `warehouse-kibana:5601`; Route path `/kibana` on Warehouse UI host | None | `restricted-v2`; read-only CM | CPU TBD; 0 GPU | Readiness `GET /kibana/api/status`; depends on ES | Dashboard loads through Route with correct base path and no mixed-content error |
| `kibana-init-container-2d` | Idempotent `J/warehouse-kibana-dashboard-init` | None | `restricted-v2`; dashboard NDJSON in CM or immutable image | Small CPU/memory; 0 GPU | Run after Kibana Ready | Re-run imports the pinned dashboard without duplicate/broken saved objects |
| `logstash` | `D/warehouse-logstash`; no client Service/Route | Prefer an immutable image with plugins; otherwise 5 GiB RWO plugin/lib PVC retained | `restricted-v2`; read-only pipeline CMs | CPU; at least 2 GiB memory for source `-Xmx1g`; 0 GPU | Startup only after broker gate and ES init; add process/TCP liveness | Known event on `mdx-*` reaches the correct ES index once, with expected schema |
| `vss-behavior-analytics-base` + `vss-behavior-analytics-2d` | Base is not separate; render child as `D/warehouse-behavior-analytics`; `warehouse-behavior-analytics:8080`; no Route | Config and calibration are read-only CMs; no writable PVC unless logs are required | `restricted-v2` | CPU TBD; 0 GPU | Add startup/readiness for the API or process plus Kafka connectivity; source has no health check | RT-CV record on `mdx-raw` produces configured ROI/tripwire/proximity behavior records |
| `vss-video-analytics-api` + `vss-video-analytics-api-2d` | Base is not separate; `D/warehouse-video-analytics-api`; `warehouse-video-analytics-api:8081`; Route `/video-analytics-api` with rewrite | 5 GiB RWO retained for `/web-api-app/files` | `restricted-v2`; read-only config CM | CPU TBD; 0 GPU | Add HTTP startup/readiness; start after broker gate and ES init | UI event query, image/snapshot response, filtering, and restart retention pass |
| `import-calibration-output-container-2d` | Idempotent `J/warehouse-calibration-import` | Calibration JSON/images from read-only CM or source-data PVC | `restricted-v2` | Small CPU/memory; 0 GPU | Run after Video Analytics API readiness | Imported camera IDs exactly match the four-source camera map; re-run is harmless |
| `alert-bridge` | `D/warehouse-alert-bridge`; `warehouse-alert-bridge:9080`; Route `/alert-bridge` because browser Alerts calls it | Config CMs; memory-backed `emptyDir` for `/app/runtime`; no state PVC | `restricted-v2`; no host IPC/network | CPU TBD; 0 GPU | HTTP startup/readiness must be added; wait on Kafka topics, Redis, ES, and RTVI-VLM | One supported incident stores `confirmed`, `rejected`, or `unverified` and exposes playable evidence |

### VIOS, perception, model, Agent, and UI plane

| Compose class | Intended OpenShift object, Service DNS, and Route | PVC / access | SCC and security | CPU, memory, GPU | Probe and startup concern | Validation gate |
|---|---|---|---|---|---|---|
| `centralizedb` | `SS/warehouse-vios-postgres`; `warehouse-vios-postgres:5432`; headless Service; no Route | 10 GiB RWO retained | Dedicated `wh-vios` SA; source runs `0:0`, so test `restricted-v2` first, then narrowly bind `anyuid` only if required | CPU TBD; 0 GPU | `pg_isready`; no Unix-socket dependency between Pods | Schema/data survive restart; VIOS clients use TCP Service DNS |
| `vst-ingress` | `D/warehouse-vios-ingress`; `warehouse-vios-ingress:30888`; Route `/vst` | Log `emptyDir` unless retention is required; config CM | `wh-vios` SA; fixed/root UID likely requires reviewed `anyuid`; read-only root FS where image permits | CPU TBD; 0 GPU | TCP/HTTP startup/readiness on 30888; wait for sensor API | VST API, upload/replay, HTTP 206 range requests, and clip URLs work through Route |
| `sensor-bp-wait-bp-configurator` | Replace with an init container on `warehouse-vios-sensor` that polls `http://warehouse-configurator:5001/readyz` with a 300-second bound | None | Same SA as VIOS sensor | Small CPU/memory; 0 GPU | Must fail rather than wait forever | Sensor Pod does not start before the configurator and reports a useful failure on timeout |
| `sensor-ms-2d` | `D/warehouse-vios-sensor`; `warehouse-vios-sensor:30000`; no Route | Shared VIOS data/video PVCs: 10 GiB RWX data and provisional 100 GiB RWX video | `wh-vios` SA; source runs `0:0`; reviewed `anyuid` likely; no host network | CPU TBD; Compose uses NVIDIA runtime but no device reservation; render 0 GPU only after runtime proof | TCP startup/readiness on 30000; PostgreSQL/configurator/SDR dependencies | Four sensors register and remain healthy; no CUDA access is attempted if rendered with 0 GPU |
| `streamprocessing-ms-2d` | `SS/warehouse-vios-streamprocessing`; `warehouse-vios-streamprocessing:30001` and internal RTSP `:30554`; headless Service; no HTTP Route to RTSP | Same RWX VIOS data/video plus 20 GiB RWX clip storage and temp `emptyDir` | `wh-vios` SA; source runs `0:0`; reviewed `anyuid`; no host network | CPU TBD; NVIDIA runtime. Treat as **1 GPU provisional** because accepted Search does, despite no Compose device reservation | TCP startup/readiness on 30001; long startup budget; PostgreSQL and Redis first | Four streams record/play; GPU request and actual utilization agree; restart keeps video and clips |
| `nvstreamer-2d` | `D/warehouse-nvstreamer`; `warehouse-nvstreamer:31000`; separate pathless Route host, matching accepted Search pattern | Source videos read-only; share 20 GiB RWX streamer/clip PVC only where VIOS requires it | Source runs `0:0`; dedicated SA and narrowly reviewed `anyuid`; no host network | Source assigns RT-CV GPU 0. Separate Pod therefore needs 1 GPU unless CPU-only behavior is vendor-validated; unresolved | Add HTTP startup/readiness on 31000; start after configurator | Four 1080p30 sources replay stably; no dropped stream; exact GPU mapping recorded |
| `perception` + `perception-2d` | Base is not separate; `SS/warehouse-rtvi-cv`; `warehouse-rtvi-cv:9000`; headless Service; no Route | 50 GiB RWO model/engine PVC retained; configs in CM | GPU SA under `restricted-v2` where possible; CDI; no host network | 1 GPU; CPU/memory TBD; GPU node selector `net-type=c845-cx7` | Startup covers TensorRT engine build; TCP/readiness on 9000; use `/usr/lib64` driver-path correction | Host driver libraries map correctly, pipeline Ready/Running, no restarts, and tracked objects publish to `mdx-raw` |
| `bp-configurator-2d-init` | Fold into `J/warehouse-configurator-broker-gate` or configurator init container | None | `restricted-v2` | Small CPU/memory; 0 GPU | Replace `localhost` with Kafka/Redis Services; bounded 120 seconds | Exact broker/topic gate completes before configurator starts |
| `bp-configurator-2d` | `D/warehouse-configurator`; `warehouse-configurator:5001`; no Route | Config/camera/calibration CMs; no source-tree RW mount | Source runs `0:0`; dedicated SA, test `restricted-v2`, narrowly bind `anyuid` only if proven | CPU TBD; 0 GPU | Startup/readiness `GET /readyz`; starts after broker gate | Creates exactly four sensors with Service DNS endpoints; no host path or mutable source mount |
| `rtvi-vlm` | `D/warehouse-rtvi-vlm`; `warehouse-rtvi-vlm:8000`; no Route | Separate RWO NGC and HF caches, sizes set from exact model bytes plus headroom; RWX read-only clip access | Fixed UID 1001 should fit `restricted-v2`; replace `ipc: host`/16 GiB SHM with size-limited memory `emptyDir` mounted at `/dev/shm`; CDI | 1 GPU; 16 GiB shared memory; CPU/memory TBD | Startup up to 20 minutes; readiness `/v1/health/ready`; Kafka and Redis DNS | Exact Cosmos3 model loads, one bounded clip returns a verdict, Kafka incident publish succeeds, no host IPC |
| `nvidia-nemotron-nano-9b-v2` | Prefer NIM Operator `NIMCache` + `NIMService`; otherwise `D/warehouse-nemotron-nano`; `warehouse-nemotron-nano:8000`; no Route | Provisional 50 GiB RWO cache, corrected after entitlement/model-size audit | NIM Operator `nonroot`/`restricted-v2`; CDI; NGC key only via Secret reference | 1 GPU; 16 GiB SHM; CPU/memory from selected RTX PRO profile | Startup probe up to model-load limit; ready/live NIM endpoints | `/v1/models` returns exact model, bounded chat completes, cache survives restart |
| `vss-va-mcp` | `D/warehouse-va-mcp`; `warehouse-va-mcp:9901`; no Route | Config CM; no PVC | `restricted-v2`; read-only filesystem where possible | CPU TBD; 0 GPU | Readiness `/health`; use VST and analytics Service DNS | Lists sensors, queries events, and fetches a snapshot/clip with no host-IP dependency |
| `vss-agent` | `D/warehouse-agent`; `warehouse-agent:8000`; Routes `/api`, `/chat`, `/websocket`, `/static` | Provisional 10 GiB RWO report/object-store PVC; eval data separate or disabled | `restricted-v2`; secrets only by references; do not install packages as root at startup—use a pinned derived image if codecs are required | CPU TBD; 0 GPU | Startup/readiness `/health`, long request budget; waits on Nano and MCP readiness | Chat lists sensors, obtains event evidence, and produces a grounded report that remains retrievable |
| `vss-ui` | `D/warehouse-ui`; `warehouse-ui:3000`; root Route on Warehouse UI host | None | `restricted-v2` | CPU TBD; 0 GPU | HTTP startup/readiness must be added; starts after Agent | Chat, Alerts, Dashboard work; Search and Video Management are absent; all URLs are HTTPS/WSS |
| `vss-haproxy-ingress` | Do not expose a host port. Preferred: replace with the path Routes above. Retain only as `D/warehouse-compat-proxy` + ClusterIP if route/path tests prove required | ConfigMap only | `restricted-v2`; no host network; no privileged ports | Small CPU/memory; 0 GPU | HTTP/TCP probe if retained | Direct Routes pass all tests; otherwise document the exact rewrite/WebSocket function that requires the proxy |
| `phoenix` | `D/warehouse-phoenix`; `warehouse-phoenix:6006`; no public Route for first demo | 10 GiB RWO retained | `restricted-v2`; ensure writable UID/fsGroup | CPU TBD; 0 GPU | Add HTTP startup/readiness | Agent traces are written without blocking Agent readiness; restart retains required traces |

### SDR helpers and platform telemetry

| Compose class | Intended OpenShift object, Service DNS, and Route | PVC / access | SCC and security | CPU, memory, GPU | Probe and startup concern | Validation gate |
|---|---|---|---|---|---|---|
| `init-dirs` | Eliminate as a standalone class; use Kubernetes volume ownership/fsGroup or a narrowly scoped init container only where required | `emptyDir`/PVC as owned by target Pod | No `0777` host directories; `restricted-v2` | Small CPU/memory; 0 GPU | Must finish before target container | Pod writes required paths without root-owned host files |
| `render-config` | Helm renders immutable `CM/warehouse-sdrc-config`; no runtime Service/Route | None | No runtime privilege | Render-time only | Fail Helm render on unresolved host-IP/localhost tokens | Config contains Service DNS and exactly four workloads/sensors |
| `wdm-env-from-config` | Eliminate; encode values directly in ConfigMap/Pod env | None | No runtime privilege | Render-time only | N/A | No generated `.wdm-env` volume remains |
| `wait-for-redis` | Replace with bounded SDR init probe against `warehouse-redis:6379` | None | `restricted-v2` | Small CPU/memory; 0 GPU | Hard timeout | SDR fails clearly when Redis is absent and starts when it is Ready |
| `wait-for-docker-workloads` | **Eliminate.** Replace Docker CLI/socket polling with Kubernetes readiness and SDR API discovery | None | Docker/CRI sockets are forbidden | None | N/A | Render contains no hostPath socket and no Docker CLI install |
| `sdr-controller` | `D/warehouse-sdrc`; Services `warehouse-sdrc-controller:5003`, `warehouse-sdrc-direct:8011`, and internal admin `:9902`; no Route | ConfigMap only; logs to `emptyDir` or platform logging | Dedicated `wh-sdrc` SA; Kubernetes mode; namespace-only `get/list/watch` on Pods, Deployments, StatefulSets; existing image may require narrowly scoped `anyuid`; no create/update/delete and no socket | CPU TBD; 0 GPU | HTTP/TCP startup/readiness on 5003/8011; starts after Redis and broker gate | Discovers all expected workloads and controls streams without Docker socket or mutation privilege |
| `dcgm-exporter` | Do not deploy duplicate; use GPU Operator DCGM exporter/cluster telemetry | None | Existing operator-owned security | Existing platform workload; no application GPU request | Existing health | Metrics identify each active Warehouse GPU Pod and distinguish allocation from utilization |
| `prometheus` | Do not deploy duplicate initially; use OpenShift monitoring or approved user-workload monitoring | None | Platform-managed | Existing platform capacity | Existing health | Warehouse metrics are queryable without a second unsupported monitoring stack |
| `grafana` | Do not deploy duplicate initially; use approved cluster dashboard. If required later, `D/warehouse-grafana` + internal Service | 10 GiB RWO only if later retained | `restricted-v2`; credentials through Secret reference | CPU TBD; 0 GPU | HTTP readiness if enabled | Demo dashboard shows pod/GPU state without exposing admin credentials |
| `node-exporter` | Do not deploy; use OpenShift node metrics | None | Avoid host `/`, `/proc`, and `/sys` mounts | Existing platform workload | Existing health | Required node metrics are already present |
| `cadvisor` | Do not deploy; use kubelet/OpenShift container metrics | None | Avoid Docker paths and host-root mounts | Existing platform workload | Existing health | Required pod/container metrics are already present |

All 40 active profile classes are represented above. The standalone
`vss-behavior-analytics-base`, `vss-video-analytics-api`, and `perception`
definitions are `extends` sources and are deliberately folded into their 2D
children rather than deployed twice.

## Required DNS substitutions

| Compose assumption | OpenShift target |
|---|---|
| `localhost:9092` / `$HOST_IP:9092` | `warehouse-kafka:9092` |
| `localhost:6379` / `$HOST_IP:6379` | `warehouse-redis:6379` |
| `localhost:9200` | `warehouse-elasticsearch:9200` |
| `localhost:5001` | `warehouse-configurator:5001` |
| `$HOST_IP:8018` RTVI-VLM | `warehouse-rtvi-vlm:8000` |
| `$HOST_IP:8000` Agent | `warehouse-agent:8000` |
| `$HOST_IP:9901` MCP | `warehouse-va-mcp:9901` |
| `$HOST_IP:9080` Alert Bridge | `warehouse-alert-bridge:9080` |
| `$HOST_IP:30888` VST | `warehouse-vios-ingress:30888` internally; HTTPS Route externally |
| `localhost:30000` sensor | `warehouse-vios-sensor:30000` |
| `localhost:30001` stream processing | `warehouse-vios-streamprocessing:30001` |
| `$HOST_IP:31000` NvStreamer | `warehouse-nvstreamer:31000` |
| `$HOST_IP:6006` Phoenix | `warehouse-phoenix:6006` |

A rendered-manifest check must reject `networkMode: host`, `hostIPC: true`,
`/var/run/docker.sock`, `/var/run/crio`, unresolved `<HOST_IP>`, and application
references to `localhost` except probes that intentionally address the same
container.

## Dependency conversion

The intended startup sequence is:

1. PVCs, Secrets by reference, ConfigMaps, Services, SAs, and RBAC.
2. PostgreSQL, Kafka, Redis, and Elasticsearch StatefulSets.
3. Kafka-topic and Elasticsearch initialization Jobs.
4. SDR, VIOS, Configurator, Kibana, and dashboard/calibration Jobs.
5. NvStreamer, RT-CV, Behavior Analytics, Video Analytics API, Logstash, and
   VIOS stream processing.
6. RTVI-VLM and Nano NIM.
7. Alert Bridge, MCP, Agent, and UI.

Kubernetes scheduling order is not used as correctness. Every consumer must
tolerate restarts and remain NotReady until its dependency API is usable.

## GPU allocation decision gate

| Workload | Compose behavior | OpenShift planning value | Unresolved point |
|---|---|---:|---|
| RT-CV perception | Explicit device `RT_CV_DEVICE_ID=0` | 1 GPU | None after driver/runtime gate |
| RTVI-VLM/Cosmos3 | Explicit device `RT_VLM_DEVICE_ID=1` | 1 GPU | Exact model memory/profile must pass |
| Nano 9B LLM | Explicit device `LLM_DEVICE_ID=2` | 1 GPU | Exact RTX PRO profile must pass |
| NvStreamer | Explicitly reuses RT-CV device 0 | 0 or 1 GPU, unresolved | Kubernetes cannot assign one exclusive device to two independent Pods |
| VIOS stream processing | NVIDIA runtime but no Compose device reservation | 1 GPU provisional | Accepted Search allocates one; Warehouse must prove whether it is required |
| VIOS sensor | NVIDIA runtime but no Compose reservation | 0 GPU provisional | Must prove it does not open CUDA/NVML |
| DCGM exporter | Sees all devices | Replaced by platform exporter | Must not reserve an application GPU |

The definite inference count is three GPUs. The defensible first Kubernetes
capacity model is four GPUs when VIOS stream processing is included, and five
if NvStreamer must receive a separate device. The planned three-GPU claim is
accepted only if runtime tests prove both of these points:

- NvStreamer runs correctly without a separate exclusive GPU or an NVIDIA-
  reviewed same-Pod design safely exposes the RT-CV device.
- VIOS stream processing needs no GPU allocation in this Warehouse profile.

Do not introduce time slicing, MIG partition changes, MPS, device-ID pinning,
or untracked `NVIDIA_VISIBLE_DEVICES` as a shortcut. With the four shared
RAG/VAST GPUs retained, a five-GPU Warehouse result cannot fit on the eight-GPU
node and is a stop condition.

## NVIDIA driver 595.91.07 verification

Read-only inspection of the source environment on 2026-09-21 confirmed:

- OpenShift `4.20.18`, Kubernetes `1.33.9`.
- The configured GPU node is Ready, cordoned, and exposes eight allocatable
  GPUs.
- Node driver label is `595.91.07`; MIG strategy is `single`.
- GPU Operator ClusterPolicy is Ready with driver, Toolkit, and CDI enabled.

These facts do not constitute Warehouse runtime acceptance. Before any GPU
controller is approved, the rendered Pod and then the running workload must
pass all of the following:

1. Exactly the reviewed controllers request `nvidia.com/gpu: 1`, select
   `net-type=c845-cx7`, and sum to no more than the available mode budget.
2. `nvidia-smi` in each GPU container reports driver `595.91.07`, the assigned
   device, and no unexpected visible GPUs.
3. RT-CV prepends `/usr/lib64` to the running DeepStream process library path.
   `/proc/<pid>/maps` must show host `libcuda` and `libnvidia-ml` from
   `/usr/lib64` at `595.91.07`, not the image's zero-byte placeholders.
4. RT-CV reaches Pipeline Ready/Running, Cosmos3 returns a bounded clip verdict,
   and Nano returns a bounded chat completion without CUDA error 803, illegal
   instruction, Xid, OOM, or restart.
5. NvStreamer, VIOS sensor, and VIOS stream processing are individually audited
   for CUDA/NVML device opens. Their requests must match observed need.

Any failure blocks Warehouse deployment; changing the driver, GPU Operator,
Toolkit, CDI mode, MIG strategy, or node scheduling requires a separate plan.

## Kubernetes 1.33.9 and OpenShift gaps

NVIDIA's VSS 3.2.1 Helm guide lists Kubernetes 1.34 or later for developer
profiles, and it does not provide an integrated Warehouse Helm profile. The
source environment runs Kubernetes 1.33.9. Consequently, no support or
compatibility claim is inferred from the successful Search adaptation.

The render gate must include:

1. `helm lint` and `helm template --kube-version 1.33.9` with every image/tag,
   API version, selector, Service, claim, probe, security context, and GPU
   request audited.
2. Kubernetes 1.33.9 server-side dry-run of the complete render and Routes.
3. SCC admission tests under the intended ServiceAccounts; no fallback to
   privileged SCC, host networking, host IPC, or runtime sockets.
4. CRI-O/CDI tests for each GPU image, including the RT-CV library correction.
5. StatefulSet claim retention and controlled restart tests on VAST CSI.
6. NetworkPolicy tests for DNS, Kafka, Redis, Elasticsearch, VST/VIOS, Agent,
   MCP, and model flows.
7. A source/render check showing no Docker Compose `build`, `depends_on`, host
   path, named-volume, device-ID, `tmpfs`, `ulimit`, or profile behavior was
   silently dropped.

A dry-run pass proves API admission only. It does not close NVIDIA support,
runtime, persistence, media, or functional gaps.

## Route, upload, WebSocket, and timeout tests

Use a dedicated Warehouse hostname and a separate pathless NvStreamer hostname.
Apply `haproxy.router.openshift.io/timeout: 3600s` only to the long-running
Agent/VST paths that prove they need it.

| Test | Pass condition |
|---|---|
| TLS and host isolation | HTTP redirects to HTTPS; Warehouse and Search hosts do not cross-route; certificate chain is accepted by the demo client |
| UI assets | Root UI and all static chunks return 200 without path or mixed-content errors |
| Agent WebSocket | `/websocket` returns HTTP 101, carries messages for at least ten minutes, and reconnects after a controlled backend restart |
| Streaming chat/SSE | `/chat/stream` delivers the first event and completes a request longer than the router default without buffering/truncation |
| API and report | `/api/v1` calls work; a generated `/static/` report remains downloadable after Agent restart |
| Alert API | `/alert-bridge/api/v1` returns the selected incident and verification status through the browser origin |
| Upload/replay size | A file at the documented maximum plus the four exact sample files uploads or mounts without HTTP 413; an over-limit file fails clearly |
| Playback and range | VST returns `206 Partial Content`, correct `Content-Range`, seeking works, and four simultaneous clips play |
| VST/NvStreamer | VST `/vst` and the separate NvStreamer host preserve required HTTP/WebSocket behavior with no host-port dependency |
| Kibana | `/kibana` dashboard loads, including subresources and any upgrade connection, through the Route base path |
| Long inference | VLM verification and report generation complete within the declared application and Route timeouts; timeout failure is surfaced, not hidden |

Only the UI, Agent browser paths, Alert browser API, VST browser API,
Video Analytics API, Kibana dashboard, and NvStreamer browser endpoint are
Route candidates. PostgreSQL, Kafka, Redis, Elasticsearch, SDR admin/control,
RT-CV, RTVI-VLM, MCP, and Nano remain cluster-internal.

## Checkpoint exit criteria

This matrix is ready to drive a render only when the next review supplies:

- exact image digests and locally built image digests for all `build:` classes;
- exact model/cache byte requirements and approved PVC sizes;
- measured CPU/memory requests and limits;
- the final three-, four-, or five-GPU controller map;
- per-workload ServiceAccounts and only the SCC grants proven necessary;
- the exact Route host/path list and NetworkPolicies; and
- a complete Helm render that passes lint, static checks, and Kubernetes 1.33.9
  server-side dry-run.

No namespace, manifest, Secret, PVC, replica, node scheduling, or stored-data
change is authorized by this document. The configured GPU node remains
cordoned.

## Source basis

- `plans/warehouse-operations-control-tower/IMPLEMENTATION-PLAN.md`
- Pinned NVIDIA VSS source commit
  `7640d917047cf7b0fd3085eefb8282754b56bc94`, especially
  `deploy/docker/industry-profiles/warehouse-operations/`,
  `deploy/docker/services/`, and the Warehouse `.env`
- `workloads/nvidia-vss-3.2.1-search/values/site-values.example.yaml`
- `workloads/nvidia-vss-3.2.1-search/manifests/routes.yaml.tpl`
- `workloads/nvidia-vss-3.2.1-search/manifests/namespace-rbac.yaml.tpl`
- `patches/nvidia-vss-3.2.1-search/openshift-v321.patch`
- Read-only source-environment queries performed on 2026-09-21; no cluster
  state changed
