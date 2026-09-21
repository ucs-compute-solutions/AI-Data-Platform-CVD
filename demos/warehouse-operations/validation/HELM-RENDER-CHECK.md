# Warehouse Helm Render Check

Date: 2026-09-21
Helm client: `v3.21.3`
Chart: `nvidia-warehouse-openshift-skeleton` `0.2.0`
Target API level: Kubernetes `1.33.9`

This is local render evidence only. No OpenShift API request, namespace,
resource, Secret, replica, GPU, or scheduling change was made.

## Results

| Check | Result |
|---|---|
| `helm lint` with checked-in defaults | PASS; one chart, zero failures |
| Default `helm template` | PASS; no Kubernetes resource manifests |
| Resolved CPU/stateful subset render | PASS; 16 resources across ConfigMap, ServiceAccount, Role, RoleBinding, Service, StatefulSet, Deployment, and Route |
| RT-CV GPU template review | PASS; one-GPU request/limit and `replicas: 0` standby rendered |
| Unresolved Configurator image | PASS; rendering stopped with the immutable-digest error |

The subset render enabled only Kafka, Elasticsearch, the Warehouse UI, and a
placeholder `.invalid` UI Route so the resource templates could be inspected.
It was not an application-complete Warehouse render and is not an installation
values file.

The GPU render supplied a placeholder ConfigMap entry solely to exercise the
RT-CV template. It verified the zero-replica standby design; it did not supply
or validate the actual NVIDIA configuration.

## Remaining render gates

- Resolve Alert Verification, Configurator, and Nemotron Nano image digests.
- Build and lock the derived Elasticsearch and helper/init images.
- Populate and verify the source-locked configuration payloads.
- Lock Secret names, PVC sizing, SCC behavior, Routes, NetworkPolicies, and
  the final one-through-eight-GPU allocation.
- Render the complete application and run the Kubernetes 1.33.9 server-side
  dry-run before requesting deployment approval.
