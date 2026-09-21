# NVIDIA Warehouse Operations Source Inventory

This is a read-only source inventory, not a deployment manifest. It resolves the NVIDIA Video Search and Summarization (VSS) Warehouse Operations `bp_wh` 2D profile at the exact VSS 3.2.1 source revision below and records the published Warehouse App Data 3.2.0 metadata. No model, video, or app-data payload was downloaded, and no registry digest or cluster state was queried.

Inventory date: 2026-09-21

## Locked official sources

| Item | Locked value |
|---|---|
| Repository | [NVIDIA-AI-Blueprints/video-search-and-summarization](https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization) |
| Commit | [`7640d917047cf7b0fd3085eefb8282754b56bc94`](https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization/tree/7640d917047cf7b0fd3085eefb8282754b56bc94) |
| Git tag | [`v3.2.1`](https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization/releases/tag/v3.2.1) |
| Release metadata | `tag: 3.2.1-26.07.1` |
| Compose root | [`deploy/docker/compose.yml`](https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization/blob/7640d917047cf7b0fd3085eefb8282754b56bc94/deploy/docker/compose.yml) |
| Warehouse environment | [`deploy/docker/industry-profiles/warehouse-operations/.env`](https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization/blob/7640d917047cf7b0fd3085eefb8282754b56bc94/deploy/docker/industry-profiles/warehouse-operations/.env) |
| Warehouse 2D overlay | [`warehouse-2d-app.yml`](https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization/blob/7640d917047cf7b0fd3085eefb8282754b56bc94/deploy/docker/industry-profiles/warehouse-operations/warehouse-2d-app/warehouse-2d-app.yml) |
| Deployment documentation | [VSS 3.2.1 Warehouse Quickstart](https://docs.nvidia.com/vss/3.2.1/warehouse-docs/Quickstart-Guide.html) and [2D profile with agents](https://docs.nvidia.com/vss/3.2.1/warehouse-docs/2D-profile-with-agents.html) |
| Warehouse App Data | [`nvidia/vss-warehouse/vss-warehouse-app-data:3.2.0`](https://catalog.ngc.nvidia.com/orgs/nvidia/vss-warehouse/resources/vss-warehouse-app-data/-) |

The NGC catalog metadata identifies 3.2.0 as the current app-data version, updated 2026-06-04 UTC, with a compressed size of 2.16 GB. The repository helper names the archive `vss-warehouse-app-data.tar.gz`. The package is described as containing sample MP4 data, TensorRT assets for RT-DETR, Sparse4D, NvDCF, and BodyPose3DNet, plus calibration and configuration templates. Its archive checksum and member-level sizes remain unknown because the package was intentionally not downloaded. The applicable NGC license must be reviewed before use.

The selected sample dataset is `nv-warehouse-4cams`: four approximately 600-second, 1920x1080, 30 FPS warehouse camera recordings intended for the 2D/automatic-calibration workflow. NVIDIA requires custom inputs to be synchronized, at least 60 seconds long, and encoded without B-frames.

## Profile resolution

The planned OpenShift translation locks the following source inputs:

| Input | Locked value | Source note |
|---|---|---|
| `MODE` | `2d` | Selects Warehouse 2D application files. |
| `BP_PROFILE` | `bp_wh` | Full Warehouse profile. |
| `SAMPLE_VIDEO_DATASET` | `nv-warehouse-4cams` | App-data 3.2.0 sample. |
| `NUM_STREAMS` | `4` | Matches the sample camera count. |
| `STREAM_TYPE` | `kafka` | Activates Kafka health-check build. |
| `HARDWARE_PROFILE` | `RTXPRO6000BW` | Target override; the upstream Warehouse `.env` default is `H100`. |
| `LLM_MODE` | `local` | Activates the local Nemotron NIM. |
| `LLM_NAME` | `nvidia/nvidia-nemotron-nano-9b-v2` | Exact model name. |
| `LLM_NAME_SLUG` | `nvidia-nemotron-nano-9b-v2` | Exact Compose service/profile suffix. |
| `VLM_MODE` | `none` | The Warehouse profile uses integrated `rtvi-vlm`, not a standalone VLM NIM. |
| Active Compose profiles | `bp_wh_2d`, `llm_local_nvidia-nemotron-nano-9b-v2` | Resolves 40 services from 41 recursively included Compose files. |

`MINIMAL_PROFILE=true` does not reduce `bp_wh`: the literal `bp_wh_2d` profile still activates the full ELK, analytics API, monitoring, and agent stack. The resolution contains 28 long-running services and 12 one-shot/init services.

## Active service inventory

Ports are container listeners unless a host-to-container mapping is shown. `host` means the upstream Compose service uses host networking. A dash means the service has no application listener declared for this profile.

| Compose service | Container | Life | Exact image or build input | Port(s) | GPU |
|---|---|---:|---|---|---|
| `alert-bridge` | `vss-alert-bridge` | run | `nvcr.io/nvidia/vss-core/vss-alert-verification:3.2.0` | 9080, host | none |
| `bp-configurator-2d` | `vss-configurator` | run | `nvcr.io/nvidia/vss-core/vss-configurator:3.2.1` | 5001, host | none |
| `bp-configurator-2d-init` | `vss-configurator-2d-init` | init | local `kafka-health-check.Dockerfile`; base `confluentinc/cp-kafka:8.2.0` | - | none |
| `broker-health-check` | `vss-broker-health-check` | init | local `kafka-health-check.Dockerfile`; base `confluentinc/cp-kafka:8.2.0` | - | none |
| `cadvisor` | generated | run | `ghcr.io/google/cadvisor:0.56.2` | 18080 -> 8080 | none |
| `centralizedb` | `vss-vios-postgres` | run | `postgres:17.9-alpine` | Unix socket only | none |
| `dcgm-exporter` | `dcgm-exporter` | run | `nvidia/dcgm-exporter:3.3.6-3.4.2-ubuntu22.04` | 9400 | all visible |
| `elasticsearch` | `elasticsearch` | run | local `elasticsearch`; base `docker.elastic.co/elasticsearch/elasticsearch:9.3.3` | 9200, 9300, host | none |
| `elasticsearch-init-container` | `vss-elasticsearch-init` | init | local `elastic-init.Dockerfile`; base `alpine:3.23.4` | - | none |
| `grafana` | `grafana` | run | `grafana/grafana:13.0.1-ubuntu` | 35000 -> 3000 | none |
| `import-calibration-output-container-2d` | `vss-import-calibration-output` | init | local `import-calibration.Dockerfile`; base `alpine:3.23.4` | - | none |
| `init-dirs` | `sdrc-init-dirs` | init | `alpine:3.23.4` | - | none |
| `kafka` | `kafka` | run | `confluentinc/cp-kafka:8.2.0` | 9092, 9093, host | none |
| `kafka-topic-init-container` | `vss-kafka-topics` | init | `confluentinc/cp-kafka:8.2.0` | - | none |
| `kibana` | `kibana` | run | `docker.elastic.co/kibana/kibana:9.3.3` | 5601, host | none |
| `kibana-init-container-2d` | `vss-kibana-init` | init | local `kibana-dashboard.Dockerfile`; base `alpine:3.23.4` | - | none |
| `logstash` | `logstash` | run | `docker.elastic.co/logstash/logstash:9.3.3` | no published application port | none |
| `node-exporter` | generated | run | `quay.io/prometheus/node-exporter:v1.11.1` | 19100 -> 9100 | none |
| `nvidia-nemotron-nano-9b-v2` | same | run | `nvcr.io/nim/nvidia/nvidia-nemotron-nano-9b-v2:1` | 30081 -> 8000 | device 2 |
| `nvstreamer-2d` | `vss-vios-nvstreamer` | run | `nvcr.io/nvidia/vss-core/vss-vios-nvstreamer:3.2.1` | HTTP 31000; RTSP 31554; RTP/UDP 31000-31200; optional gRPC 50051; host | device 0 |
| `perception-2d` | `vss-rtvi-cv` | run | `nvcr.io/nvidia/vss-core/vss-rt-cv:3.2.1` | 9000, host | device 0 |
| `phoenix` | `phoenix` | run | `arizephoenix/phoenix:14.15.0` | 6006 | none |
| `prometheus` | `prometheus` | run | `quay.io/prometheus/prometheus:v3.11.3` | 9090 | none |
| `redis` | `redis` | run | `redis:8.6.2-alpine` | 6379, host | none |
| `render-config` | `sdrc-render-config` | init | `alpine:3.23.4` | - | none |
| `rtvi-vlm` | `vss-rtvi-vlm` | run | `nvcr.io/nvidia/vss-core/vss-rt-vlm:3.2.1` | intended 8018 -> 8000 | device 1 |
| `sdr-controller` | `sdr-controller` | run | `nvcr.io/nvidia/vss-core/sdr-mw-l:3.2.0` | 5003, 8011, Envoy admin 9902; host | none |
| `sensor-bp-wait-bp-configurator` | same | init | `busybox:1.37.0` | - | none |
| `sensor-ms-2d` | `vss-vios-sensor` | run | `nvcr.io/nvidia/vss-core/vss-vios-sensor:3.2.1` | 30000, host | NVIDIA runtime; no ID |
| `streamprocessing-ms-2d` | `vss-vios-streamprocessing` | run | `nvcr.io/nvidia/vss-core/vss-vios-streamprocessing:3.2.1` | 30001, host | NVIDIA runtime; no ID |
| `vss-agent` | `vss-agent` | run | `nvcr.io/nvidia/vss-core/vss-agent:3.2.1` | 8000, host | none |
| `vss-behavior-analytics-2d` | `vss-behavior-analytics` | run | `nvcr.io/nvidia/vss-core/vss-behavior-analytics:3.2.1` | 8080, host | none |
| `vss-haproxy-ingress` | same | run | `haproxy:3.0-alpine` | 7777, host | none |
| `vss-ui` | `vss-agent-ui` | run | `nvcr.io/nvidia/vss-core/vss-agent-ui:3.2.0` | 3000 | none |
| `vss-va-mcp` | same | run | `nvcr.io/nvidia/vss-core/vss-agent:3.2.1` | 9901, host | none |
| `vss-video-analytics-api-2d` | `vss-video-analytics-api` | run | `nvcr.io/nvidia/vss-core/vss-video-analytics-api:3.2.0` | 8081, host | none |
| `vst-ingress` | `vss-vios-ingress` | run | `nvcr.io/nvidia/vss-core/vss-vios-ingress:3.2.1` | 30888, host | none |
| `wait-for-docker-workloads` | `sdrc-wait-for-workloads` | init | `alpine:3.23.4` | - | none |
| `wait-for-redis` | `sdrc-wait-for-redis` | init | `redis:8.6.2-alpine` | - | none |
| `wdm-env-from-config` | `sdrc-wdm-env-from-config` | init | `alpine:3.23.4` | - | none |

The shared device-0 reservation by `perception-2d` and `nvstreamer-2d` is an explicit Compose co-location assumption. Separate OpenShift pods cannot reproduce it by independently requesting the same exclusive GPU. The source does not define MIG, MPS, or time-slicing; the OpenShift GPU topology therefore remains a design input. `rtvi-vlm` has 16 GiB shared memory and host IPC; Nemotron has 16 GiB shared memory.

### Mixed 3.2.0 and 3.2.1 component tags

The 3.2.1 source intentionally combines republished 3.2.1 components with unchanged 3.2.0 components. Preserve the source versions rather than mechanically retagging them:

- 3.2.0: alert verification, SDR middleware, agent UI, video analytics API.
- 3.2.1: configurator, RT-CV, RT-VLM, NvStreamer, VST sensor/stream-processing/ingress, agent, behavior analytics.

Every registry reference is tag-based, not digest-locked. Registry digests must be resolved and added before deployment. The local builds also fetch `jq` 1.7.1 without a checksum, and Kafka-mode Logstash installs unversioned `logstash-codec-protobuf` at startup. These are unresolved supply-chain inputs.

## Model lock

| Function | Locked model/input | Runtime consequence |
|---|---|---|
| 2D detection | `nvidia/tao/rtdetr_2d_warehouse:deployable_rn50_v1.0.2` | Materializes `models/mtmc/rtdetr_warehouse_v1.0.2.fp16.onnx`; four streams yield `rtdetr_warehouse_v1.0.2.fp16.onnx_b4_gpu0_fp16.engine`. |
| Detector labels | `Person`, `Agility_Digit_Humanoid`, `Fourier_GR1_T2_Humanoid`, `Nova_Carter`, `Transporter`, `Forklift`, `Pallet` | Seven-class Warehouse detector. |
| Tracking | NvDCF, `reidType: 0` | No ReID model is active. |
| Integrated VLM | `ngc:nim/nvidia/cosmos3-nano-reasoner:bf16-final`; selector `cosmos-reason3` | Served by `rtvi-vlm`; displayed identifier `nim_nvidia_cosmos3-nano-reasoner_bf16-final`. |
| Local LLM | `nvidia/nvidia-nemotron-nano-9b-v2`, NIM image tag `1` | Served on container port 8000. |

The target file `hw-RTXPRO6000BW.env` exists but contains only license comments; it contributes no model-tuning variables.

## Dependency and readiness inventory

Only dependencies whose services are active in the resolved profile are shown. “Optional” is the upstream Compose dependency flag, not permission to omit a required functional backend.

| Service | Active dependency gate |
|---|---|
| `alert-bridge` | Kafka healthy; Redis started; Elasticsearch healthy; Kafka topics completed; `rtvi-vlm` healthy (optional). |
| `bp-configurator-2d` | `bp-configurator-2d-init` completed. |
| `elasticsearch-init-container` | Elasticsearch healthy. |
| `import-calibration-output-container-2d` | Video analytics API started. |
| `kafka-topic-init-container` | Kafka healthy. |
| `kibana` | Elasticsearch healthy. |
| `kibana-init-container-2d` | Kibana healthy. |
| `logstash` | Broker health and Elasticsearch initialization completed. |
| `nvstreamer-2d` | Configurator healthy. |
| `perception-2d` | SDR and sensor started; broker health completed. |
| `rtvi-vlm` | Broker health completed (optional). |
| `sdr-controller` | Broker health completed (optional); directory and WDM environment initialization completed. |
| `sensor-ms-2d` | Postgres healthy; configurator wait completed and SDR started (both optional). |
| `streamprocessing-ms-2d` | Postgres healthy; Redis started. |
| `vss-agent` | local LLM, `rtvi-vlm`, and VA MCP healthy (all marked optional). |
| `vss-behavior-analytics-2d` | Broker health completed. |
| `vss-ui` | Agent healthy. |
| `vss-video-analytics-api-2d` | Broker health and Elasticsearch initialization completed. |
| `vst-ingress` | Sensor healthy (optional). |
| `wdm-env-from-config` | directory initialization and configuration rendering completed. |
| `wait-for-docker-workloads`, `wait-for-redis` | WDM environment initialization completed. |

Explicit Compose health checks exist for:

| Service | Probe | Timing (`interval / timeout / retries / start`) |
|---|---|---|
| Configurator | HTTP `GET /readyz` on 5001 | `10s / 5s / 30 / 60s` |
| Postgres | `pg_isready` over Unix socket | `5s / 3s / 60 / 2s` |
| Elasticsearch | cluster health waits for yellow and no timeout | `10s / 10s / 60 / 60s` |
| Kafka | `kafka-topics --list` on 9092 | `15s / 15s / 60 / 60s` |
| Kibana | HTTP `GET /kibana/api/status` on 5601 | `10s / 10s / 60 / 60s` |
| Nemotron NIM | ready (up to 600 seconds), then live endpoint | `60s / 650s / 2 / source default` |
| RTVI-VLM | HTTP `GET /v1/health/ready` on 8000 | `30s / 10s / 5 / 1200s` |
| VST sensor | TCP 30000 | `10s / 3s / 20 / 5s` |
| VST stream processing | TCP 30001 | `20s / 10s / 75 / 20s` |
| VSS agent | HTTP `GET /health` | `30s / 10s / 3 / 240s` |
| VA MCP | HTTP `GET /health` | `30s / 10s / 3 / 40s` |
| VST ingress | TCP 30888 | `5s / 3s / 30 / 2s` |

The other long-running services have no explicit Compose health check; init services rely on successful completion. OpenShift probes must be designed for alert bridge, behavior analytics, video analytics API, UI, NvStreamer, RT-CV, SDR, HAProxy, Redis, Logstash, Phoenix, and monitoring services rather than invented from undocumented endpoints.

## Kafka contract

The init job creates 21 topics. Defaults are 8 partitions, retention 14,400,000 ms (4 hours), segment 3,600,000 ms, and replication factor 1. `mdx-notification` overrides partitions to 1.

`mdx-raw`, `mdx-bev`, `mdx-space-utilization`, `mdx-alerts`, `mdx-behavior`, `mdx-behavior-plus`, `mdx-frames`, `mdx-mtmc`, `mdx-rtls`, `mdx-rtls-region-1`, `mdx-amr`, `mdx-vlm-alerts`, `mdx-notification`, `mdx-events`, `mdx-incidents`, `mdx-vlm-incidents`, `mdx-vlm`, `mdx-vlm-captions`, `mdx-structured-events-summary`, `mdx-embed`, `mdx-embed-filtered`.

Kafka automatic topic creation is disabled. Configuration also references `alert-bridge-enhanced-alerts`, `alert-bridge-incidents`, `vision-llm-messages`, and `vision-llm-errors`; those names are not created by the active init job. The RTVI default `vision-llm-events-incidents` is overridden by Warehouse to `mdx-vlm-incidents`. Topic use and explicit creation must be closed before deployment.

### Alert behavior present in source

The Warehouse behavior configuration enables proximity at 4 units (`Forklift` center, `Person` surrounding) and tripwire evaluation with at least four points. It enables restricted-area, confined-area, and proximity incident types with threshold 2. Always-on VLM rules are PPE, Load Quality, Pathway Obstruction, and Spillover. The alert-type map converts proximity violations to `Near Miss Violation`. The locked source contains no worker-fall rule; fall detection would require a separately defined detector/rule, event schema, validation data, and notification path.

## Storage and configuration contract

| Data class | Upstream mount/volume |
|---|---|
| Sample video | `${VSS_DATA_DIR}/videos/nv-warehouse-4cams` into NvStreamer. |
| 2D model/engine | `${VSS_DATA_DIR}/models/mtmc` into RT-CV `/opt/storage`. |
| VST | `${VSS_DATA_DIR}/data_log/vst` subpaths for database, video, temporary files, clips, and logs; named `vios_pg_data`. |
| Kafka | `mdx-kafka` backed by `${VSS_DATA_DIR}/data_log/kafka`. |
| Elasticsearch | `mdx-elastic-data` and `mdx-elastic-logs`, backed by `${VSS_DATA_DIR}/data_log/elastic/{data,logs}`. |
| Redis | `${VSS_DATA_DIR}/data_log/redis/{data,log}`. |
| Model caches | `nvidia_nemotron_nano_9b_v2_cache`, `rtvi-hf-cache`, `rtvi-ngc-model-cache`. |
| Supporting state | `phoenix-data`, `grafana-storage`, `agent-eval`, `mdx-logstash-libs`. |
| SDR generated state | `./log`, `./.wdm-env`, and the Docker socket in the Compose implementation. |

Compose monitoring host mounts (`/`, `/proc`, `/sys`, `/var/run`, and the Docker data directory) and the SDR Docker socket are not portable OpenShift volume definitions. The configurator mounts the application and data trees read-write and rewrites/backups configuration files. OpenShift must stage writable copies or generated configuration; it must not mount immutable source ConfigMaps where mutation is expected.

The following deploy-affecting source files are content-locked. SHA-256 is over the file at the locked commit:

| Source-relative path | SHA-256 |
|---|---|
| `industry-profiles/warehouse-operations/.env` | `0c6242fb720e34a4d112ed80c26b75f1cb163c1b738a8b91835e1f1a16770857` |
| `industry-profiles/warehouse-operations/blueprint-configurator/blueprint_config.yml` | `966f61849217fbd7aaf1f1f5ac5407cdb91e961d2a7685297dd816a383a4c3ba` |
| `warehouse-2d-app/calibration/sample-data/nv-warehouse-4cams/calibration.json` | `37127a068d8a6775ab1a12cffa2634c477e6bc44f0833d7d918b3569fd64e1e0` |
| `warehouse-2d-app/calibration/sample-data/nv-warehouse-4cams/images/Top.png` | `4f4872bd6a1ce7ad0a3c00de0a7420183adebe9f27f7e9215f5c40e077b39ec7` |
| `warehouse-2d-app/calibration/sample-data/nv-warehouse-4cams/images/imageMetadata.json` | `c068bbd4d410dd4594e2523afdb57dfbd1ac20a9818e721c2ffc0d2c48dbb9c7` |
| `warehouse-2d-app/deepstream/configs/ds-detector-labels.txt` | `c99e718f3e4ed3840f303b4d57919edafb9ae14e5fd1b4cfb419c8ded1b4461b` |
| `warehouse-2d-app/deepstream/configs/ds-kafka-config.txt` | `47bfaeea2ac6743d94cfd75a3bd3981c7c7af30c219f94413b9e2adf79fc10c2` |
| `warehouse-2d-app/deepstream/configs/ds-main-config.txt` | `24cf9ead36a2158f752b6916c1984e632b6690161c21ca1eec4fcec21011a07c` |
| `warehouse-2d-app/deepstream/configs/ds-nvdcf-accuracy-tracker-config.yml` | `65c83082d4dbe9bd84138c6a927c2a734a18dbc9615e8383a51d4d8712b813a5` |
| `warehouse-2d-app/deepstream/configs/ds-pgie-config.yml` | `e4394fb36f64db8b4a0c75047d176a26e53b66b1a5da85bfcaff390f79ce27f7` |
| `warehouse-2d-app/deepstream/configs/ds-redis-config.txt` | `e21a6634f37a1319513dfcffc788bb5bc0a06186c967cfc6d6311dea9d2bfee9` |
| `warehouse-2d-app/nvstreamer/configs/vst-config.json` | `b45e7c9b76b13f76eace1e2958b6cfba8a35fcaa5e0b2f3400c3ac8fc2a0ba8c` |
| `warehouse-2d-app/nvstreamer/configs/vst-storage.json` | `e8c718d1f9288965692fcb3ac88d43b927eac0d8f598f462a6eca9a328daa8f6` |
| `warehouse-2d-app/sdrc/configs/config.yml.tmpl` | `612b7926708bc94cca8849540c3da2ab86f894681f862509dcefb3d49472c268` |
| `warehouse-2d-app/sdrc/configs/docker_cluster_config-alerts-2d.json.tmpl` | `0822af0dae686758c49ecd8ef8c6653534cc37928a3380c4a668143b65c8b1c7` |
| `warehouse-2d-app/sdrc/configs/docker_cluster_config-rtvi-cv.json.tmpl` | `31e0b42715ccb233194f6e79003c8a1c5df4a73f9a78e2a1f573f431b4667127` |
| `warehouse-2d-app/sdrc/configs/docker_cluster_config-streamprocessing.json.tmpl` | `37a13d3ff1094f3c98f70a270b09f0c88112b0ed140f07566fbc17e54fab4a17` |
| `warehouse-2d-app/vss-behavior-analytics/configs/vss-behavior-analytics-config.json` | `8424ac5a6f1ce041716bf00f057514a306f684ad93d938cbfdc7b82638b3e887` |
| `warehouse-2d-app/vst/configs/vst_config.json` | `db5d7ca6e3fb37489758fae2c9a874e0cb16cfff12d667ee573abff70ddd90ec` |
| `industry-profiles/warehouse-operations/vlm-as-verifier/realtime-config.yml` | `d4413303c99da27a5aa2781d01e46fb28c05c45e54b4b155cdf8e55f26b40aca` |
| `industry-profiles/warehouse-operations/vlm-as-verifier/configs/alert_type_config.json` | `d7fd8c3b744a749f589759e18c5d0037a520e8b443e49997cdf6302067b97e85` |
| `industry-profiles/warehouse-operations/vlm-as-verifier/configs/config.yml` | `5f270907409f22a57c9e87fd9291d668d67ea6b21a4f7033adee0aba7a09dbd1` |
| `industry-profiles/warehouse-operations/vss-agent/configs/config.yml` | `0d922090a1ebcf6f4ab6d1d16378dae9be67afc7a82e63d532d662bdb3ee4bd6` |
| `industry-profiles/warehouse-operations/vss-agent/configs/va_mcp_server_config.yml` | `b1cc6cf0213fc368d8c558a32ec5f5eec04d4391303cfa6d2ada4db69ce4afc8` |
| `industry-profiles/warehouse-operations/vss-agent/templates/incident_report_template.md` | `826cf947a37b7e151c50422812f373b76f57cd89c93f5dcc1457fa8aa6a7e5e1` |
| `services/vios/vst.env` | `f430d16501a3e66e349f7e0d8195ec18fa113b9a2b9a2157c71e9465d0224bc7` |
| `services/vios/configs/nginx-vst.conf` | `8a095b07cab077fba4667200606c990bd6838e19d634158dcaaac73c0ba4e690` |
| `services/vios/configs/postgresql.conf` | `65e52049144c5bc4b1722f1d450ec6b0c5facfc218f4d12694fd4f2dbf451618` |
| `services/infra/haproxy/haproxy.cfg.template` | `c13922297c2d53d5f0b532eaba05bc8b8616836fc552f9ebc152c83a0dbb3343` |

Paths in this table are relative to `deploy/docker/`; abbreviated `warehouse-2d-app/...` paths are beneath `deploy/docker/industry-profiles/warehouse-operations/`. The YAML lock carries the same hashes in machine-readable form.

## Environment references

Only names and non-secret selection values are locked. Secret values are never stored.

- Selection: `MODE`, `BP_PROFILE`, `MINIMAL_PROFILE`, `SAMPLE_VIDEO_DATASET`, `HARDWARE_PROFILE`, `NUM_STREAMS`, `COMPOSE_PROFILES`, `STREAM_TYPE`.
- Paths and public endpoints: `VSS_APPS_DIR`, `VSS_DATA_DIR`, `HOST_IP`, `EXTERNAL_IP`, `HAPROXY_PORT`, `VSS_PUBLIC_HTTP_PROTOCOL`, `VSS_PUBLIC_WS_PROTOCOL`, `VSS_PUBLIC_HOST`, `VSS_PUBLIC_PORT`.
- GPU and models: `RT_CV_DEVICE_ID`, `RT_VLM_DEVICE_ID`, `LLM_DEVICE_ID`, `PERCEPTION_IMAGE`, `PERCEPTION_IMAGE_TAG`, `RTVI_VLM_IMAGE_TAG`, `RTVI_VLM_MODEL_PATH`, `RTVI_VLM_MODEL_TO_USE`, `LLM_NAME`, `LLM_NAME_SLUG`, `LLM_MODE`, `LLM_PORT`, `LLM_BASE_URL`, `LLM_MODEL_TYPE`, `VLM_MODE`, `VLM_NAME`, `VLM_PORT`, `VLM_BASE_URL`, `VLM_MODEL_TYPE`, `RTVI_VLM_PORT`.
- Agent and APIs: `VSS_AGENT_VERSION`, `VSS_AGENT_HOST`, `VSS_AGENT_PORT`, `VSS_AGENT_CONFIG_FILE`, `VSS_VA_MCP_PORT`, `VSS_VA_MCP_CONFIG_FILE`, `MDX_PORT`, `VIDEO_ANALYSIS_MCP_URL`, `PHOENIX_ENDPOINT`, `ALERT_BRIDGE_*`, `VST_*`, and the agent prompt/template variables.
- External credentials, references only: `NGC_CLI_API_KEY`, `NVIDIA_API_KEY`, `OPENAI_API_KEY`, `HF_TOKEN`, plus adaptor and object-store credential variables. Values belong in approved OpenShift Secrets, never this repository.
- Environment files: Warehouse `.env`, VST `vst.env`, and `services/nim/nvidia-nemotron-nano-9b-v2/hw-RTXPRO6000BW.env` with the service fallback environment file.

## Blocking source-closure items

1. `rtvi-vlm-docker-compose.yml` requires `${RTVI_VLM_PORT?}:8000`, but the Warehouse `.env` does not define `RTVI_VLM_PORT`. Warehouse URLs and NVIDIA's profile reference consistently use 8018, while `VLM_PORT=30082` belongs to the disabled standalone VLM NIM. Set and validate `RTVI_VLM_PORT=8018` explicitly before rendering; this is a proposed resolution, not a value present in the `.env`.
2. Resolve every registry tag to an immutable digest and build/publish the four local init images plus local Elasticsearch image into an approved registry.
3. Record the licensed app-data archive digest and member inventory only after an authorized download; do the same for model payloads and generated TensorRT engines.
4. Resolve unpinned build-time/runtime downloads (`jq` and the Logstash protobuf codec) and the Kafka topics referenced but not initialized.
5. Decide OpenShift GPU sharing/co-location for the two device-0 processes and replace host networking, host paths, Docker socket access, and mutable source-tree mounts with reviewed OpenShift equivalents.
6. Add probes for services without upstream health checks and validate all port/DNS rewrites. This source inventory alone is intentionally not deployable.
