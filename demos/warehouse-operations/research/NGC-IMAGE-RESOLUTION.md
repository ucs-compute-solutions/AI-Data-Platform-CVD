# Protected NVIDIA Image Resolution

Status: RTVI-VLM is resolved. Three Warehouse source images still require an
NGC service account key or additional entitlement before their immutable
digests can be recorded. No image was pulled and no OpenShift resource was
changed during this check.

## Evidence from the pinned release

The references below are present in NVIDIA VSS `v3.2.1`, commit
`7640d917047cf7b0fd3085eefb8282754b56bc94`. This establishes the intended
source tags, but it does not replace a registry manifest check.

| Component | Exact source-locked image | Pinned NVIDIA source evidence | Authenticated registry result | Required access or disposition |
|---|---|---|---|---|
| Alert Bridge | `nvcr.io/nvidia/vss-core/vss-alert-verification:3.2.0` | `deploy/docker/services/alert/compose.yml`; `deploy/helm/services/alert/values.yaml` | Login succeeded; registry required a SAK | Use an NGC service key with Catalog container-read scope for the entitled organization |
| Blueprint Configurator | `nvcr.io/nvidia/vss-core/vss-configurator:3.2.1` | `deploy/docker/industry-profiles/warehouse-operations/warehouse-2d-app/warehouse-2d-app.yml` | Login succeeded; registry required a SAK | Use an NGC service key with Catalog container-read scope for the entitled organization |
| RTVI-VLM | `nvcr.io/nvidia/vss-core/vss-rt-vlm:3.2.1` | `deploy/docker/services/rtvi/rtvi-vlm/rtvi-vlm-docker-compose.yml`; `deploy/helm/services/rtvi/charts/rtvi-vlm/values.yaml` | Resolved: manifest `sha256:5403e0c8...9504`; linux/amd64 `sha256:156bc152...46cc` | Complete; recorded in the source lock and chart |
| Nemotron Nano NIM | `nvcr.io/nim/nvidia/nvidia-nemotron-nano-9b-v2:1` | `deploy/docker/services/nim/nvidia-nemotron-nano-9b-v2/compose.yml`; the Base, Search, and Alerts Helm profiles | Login succeeded; image absent or unauthorized | Retry with the service key; if it still fails, request NIM image entitlement or tag confirmation from NVIDIA |

The successful RTVI-VLM result proves that the supplied personal key and
registry login were valid. The other responses are authorization failures.
They do not justify changing a tag or weakening the chart's fail-closed digest
gate.

## Service key required for the remaining images

An NGC organization owner or `user_admin` must create the service key from
**Organization > Service Keys > Create Service Key**. Use the NGC Catalog
service with the minimum container-read scopes **Get Container** and
**Get Container list**. Authorize the exact repositories where possible; the
broader NVIDIA-managed paths are `nvidia/*/*` for VSS Core and `nim/*/*` for
NIM. Access to restricted NIM artifacts still depends on the organization's
active NVIDIA entitlement.

NVIDIA documents these key types, scopes, paths, and the meaning of 403 errors
in the [NGC User Guide](https://docs.nvidia.com/ngc/latest/ngc-user-guide.html).
New or changed service-key permissions can take several minutes to propagate.
Use the service-key value as the password at the same hidden prompt below; the
registry username remains the literal `$oauthtoken`.

## Safe digest-resolution procedure

Run the wrapper from the deployment client. It stores registry authentication
only in a temporary directory, never prints the API key, limits resolution to
the four protected source references, writes a digest-only JSON result under
`/tmp`, and removes the temporary login on exit.

```bash
# Run from the repository root.
./demos/warehouse-operations/scripts/resolve-protected-ngc-images.sh
```

Expected result with the required service key: all four records have
`status: resolved`, a manifest-list or image `digest`, and the selected
`platformDigest` for `linux/amd64`. A 401 is an authentication failure. A 403,
`Please use sak key`, or `does not exist or you do not have permission` is an
authorization or entitlement failure. Do not substitute `latest` or another
tag.

After a successful run, copy only newly resolved digest values into
[`source-lock.yaml`](../openshift/nvidia-warehouse/source-lock.yaml) and the
matching entries in the chart
[`values.yaml`](../openshift/nvidia-warehouse/chart/values.yaml). Do not copy
the temporary registry-auth file into the repository.
