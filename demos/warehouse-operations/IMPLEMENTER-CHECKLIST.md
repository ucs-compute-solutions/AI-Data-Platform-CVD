# Warehouse Operations Implementer Checklist

Status: **implementation starter; not deployable yet**

Use this checklist to complete the OpenShift adaptation of the NVIDIA VSS
3.2.1 Warehouse profile. The checked-in chart is deliberately inert and the
mode-switching tools are plan-first. Do not install the chart or enable a mode
switch until every blocker and exit condition below has been cleared.

## Required inputs

| Area | Required input |
|---|---|
| Platform | Target OpenShift and Kubernetes versions, cluster context, namespace, and an administration client with `oc`, Helm 3, Bash, and Python 3 |
| Accelerators | One GPU node with an accepted allocation of 1–8 NVIDIA GPUs; the final render must specify the exact count |
| NVIDIA runtime | GPU Operator, container runtime, driver, and device plug-in versions verified for the selected Warehouse images and models |
| Storage | A reviewed StorageClass and capacity plan for VIOS media, PostgreSQL, Kafka, Elasticsearch, model caches, reports, and agent state |
| NVIDIA access | An entitled NGC organization service account key (SAK) with the minimum container-read scopes required for the protected repositories |
| Dataset | NVIDIA Warehouse App Data 3.2.0 or separately licensed, synchronized warehouse video with an approved camera map and checksums |
| Permissions | Approval to use the dataset, create namespace-scoped resources, reference pre-created Secrets, provision storage, expose approved Routes, and schedule the accepted GPU allocation |
| Baseline | Exact controller inventory and functional tests for every workload that must be restored after Warehouse mode |

Do not store passwords, service keys, registry authentication, kubeconfigs,
private certificates, or Kubernetes Secret values in this repository.

## Ordered implementation stages

| Stage | Input | Action or command | Expected artifact | Exit condition |
|---:|---|---|---|---|
| 0. Confirm scope | Approved demo story and repository checkout | Review the [implementation plan](../../plans/warehouse-operations-control-tower/IMPLEMENTATION-PLAN.md), supported claims, and stop conditions | Agreed story, owner list, target dates, and acceptance scope | Everyone agrees that this package is an OpenShift adaptation requiring local validation, not a deployable or support-certified release |
| 1. Prepare site profile | Verified cluster, GPU node, namespace, releases, and base controllers | `cp demos/warehouse-operations/profiles.example.yaml demos/warehouse-operations/profiles.yaml`; replace every placeholder in the ignored local copy | Site-local `profiles.yaml` that is not committed | `scripts/show-status.sh` can read the profile, and every baseline controller and replica count matches the target environment |
| 2. Freeze data and evidence | Licensed four-camera dataset and camera mapping | Complete [warehouse-data-manifest.yaml](data/warehouse-data-manifest.yaml) and review [ALERT-SCENARIO-MATRIX.md](data/ALERT-SCENARIO-MATRIX.md) | Names, sizes, checksums, durations, camera IDs, visible activities, and allowed claims | All sources are reproducible and the proposed demo makes no claim unsupported by visible evidence |
| 3. Resolve NVIDIA inputs | NGC organization SAK and required entitlements | Run `./demos/warehouse-operations/scripts/resolve-protected-ngc-images.sh`; copy only verified digests into the [source lock](openshift/nvidia-warehouse/source-lock.yaml) and chart values | Immutable digest record for every external image | Alert Verification, Configurator, Nemotron Nano, all derived images, and helper/init images are digest-pinned; no `latest` or unresolved tag remains |
| 4. Complete application configuration | Pinned NVIDIA source and [configuration inventory](research/WAREHOUSE-CONFIG-INVENTORY.md) | Supply reviewed ConfigMaps, probes, Service DNS, init Jobs, existing-Secret references, Routes, NetworkPolicies, and SCC requirements in the [chart](openshift/nvidia-warehouse/chart/) | Application-complete Helm values and rendered manifests | No placeholder, blocked init Job, host-network dependency, unresolved `localhost`, or embedded credential remains |
| 5. Lock storage and GPU design | Capacity estimates, StorageClass behavior, Compose device map, and GPU policy | Map every stateful path to reviewed PVCs; map every GPU container to an OpenShift controller or reviewed multi-container Pod | Storage/quota matrix and exact 1–8 GPU controller map | PVC access modes/capacities are accepted, total rendered GPU requests equal the approved count, and no undocumented time slicing, MIG, MPS, or CPU inference is introduced |
| 6. Run offline quality gates | Completed chart and local tooling | `bash -n demos/warehouse-operations/scripts/*.sh`; `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s demos/warehouse-operations/tests -p 'test_*.py' -v`; `helm lint demos/warehouse-operations/openshift/nvidia-warehouse/chart` | Passing shell, unit, chart-safety, and Helm lint results | All offline checks pass and the checked-in default render remains inert |
| 7. Render and admission-test | Final values, exact API level, pre-created Secret names, and approved namespace | Render locally with Helm; inspect every image, PVC, Route, security context, replica, selector, and `nvidia.com/gpu` request; then run an OpenShift server-side dry-run | Reviewed manifest bundle and sanitized dry-run evidence | Client render and server admission pass with no resource creation, no unresolved SCC issue, and no deviation from the accepted capacity map |
| 8. Review execution and recovery | Passing dry run, baseline status, exact manifests, and rollback steps | Use `scripts/set-demo-mode.sh warehouse` and `scripts/set-demo-mode.sh base-cvd` in plan-only mode; review commands, effects, timeouts, quiescence checks, and recovery | Approved change plan and baseline evidence set | Reviewers can account for every scaling and scheduling effect and can restore all retained base services without deletion or re-ingestion |
| 9. Perform controlled live validation | Separate deployment approval and accepted change plan | Follow the approved render and runbook; validate stateful services before GPU services; retain sanitized events, readiness, and GPU evidence | Running isolated Warehouse namespace with recorded evidence | Four streams are healthy, expected data persists across a controlled restart, intended pods schedule, and no unexpected Pending pods, restarts, pressure, or warnings remain |
| 10. Validate both demo lanes | Running Warehouse profile and retained VAST-native lane | Execute the [acceptance matrix](../../plans/warehouse-operations-control-tower/IMPLEMENTATION-PLAN.md#acceptance-matrix), alert scoring, agent report, playback, VAST search, and grounded synthesis checks | Timestamped acceptance record for NVIDIA Warehouse and the complementary VAST-native lane | Every in-scope UI, event, verification, retrieval, synthesis, telemetry, and playback test passes |
| 11. Prove recovery and hand off | Accepted Warehouse run and exact baseline inventory | Complete three Warehouse/base switch cycles, five warm rehearsals, and one fallback rehearsal; record operator steps and sanitized evidence | Implementer run card, evidence pack, fallback recording, and restore record | Both base search lanes return from retained state after every cycle, with no data loss, reinstall, or re-ingestion |

Stages 7–11 require the approvals and access appropriate to the target
environment. The commands above describe the required gates; this checklist
does not authorize cluster changes.

## Current blockers

| Blocker | Required resolution | Evidence location |
|---|---|---|
| Three protected images remain unresolved | Resolve immutable digests for Alert Verification, Blueprint Configurator, and Nemotron Nano using an entitled NGC organization SAK; confirm the exact tag with NVIDIA if access still fails | [NGC-IMAGE-RESOLUTION.md](research/NGC-IMAGE-RESOLUTION.md) |
| Runtime configuration and initialization are incomplete | Populate the source-derived ConfigMaps, helper/init images and Jobs, probes, Service endpoints, Routes, NetworkPolicies, SCC requirements, and existing-Secret references | [WAREHOUSE-CONFIG-INVENTORY.md](research/WAREHOUSE-CONFIG-INVENTORY.md) and [chart README](openshift/nvidia-warehouse/chart/README.md) |
| Final GPU map is not accepted | Reconcile Compose device sharing with OpenShift scheduling, choose the exact 1–8 GPU allocation, and replace all controller placeholders | [OPENSHIFT-PORTING-MATRIX.md](research/OPENSHIFT-PORTING-MATRIX.md) and [profiles.example.yaml](profiles.example.yaml) |
| Full render and server dry-run are outstanding | Render the complete namespace, audit it, and pass the target OpenShift server-side dry-run without creating resources | [HELM-RENDER-CHECK.md](validation/HELM-RENDER-CHECK.md) |
| Live deployment and functional acceptance are outstanding | Obtain separate approval, deploy the isolated profile, validate all NVIDIA and VAST flow checks, and prove restoration through repeated switch cycles | [PROFILE-SWITCHING.md](runbooks/PROFILE-SWITCHING.md), [ALERT-SCORING.md](runbooks/ALERT-SCORING.md), and the [implementation plan](../../plans/warehouse-operations-control-tower/IMPLEMENTATION-PLAN.md) |

## Handoff completion record

The implementer should attach or link the following before declaring the
package ready for a live demonstration:

- Approved dataset manifest and claim matrix.
- Complete image/model/source lock with immutable digests.
- Reviewed Helm values and rendered manifest bundle.
- Storage and exact GPU allocation review.
- Passing offline test, Helm lint, render-audit, and server dry-run results.
- Approved execution and recovery plan.
- NVIDIA Warehouse and VAST-native acceptance evidence.
- Three successful recovery cycles, five warm rehearsals, and one fallback
  rehearsal.
