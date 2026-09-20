# Deploy VAST InsightEngine 5.4.3

This directory contains the deployment and verification scripts for the
**Deploy VAST InsightEngine** section of the AI Data Platform CVD. It verifies a VAST-supplied
InsightEngine delivery, prepares the application namespace, imports the
release-pinned images, deploys the reviewed PostgreSQL, operator, and backend
charts, and checks the resulting OpenShift and VAST resources.

The VAST delivery README, runbook, compatibility statement, checksums, charts,
CRDs, and tools remain authoritative. These scripts validate release 5.4.3;
use every artifact from that delivery and do not mix components from another
release. A later release requires its own runbook and validator update.

## Files

| Path | Purpose |
| --- | --- |
| `release-inputs.example.env` | Site and release inputs. Copy it locally; do not commit the completed file. |
| `scripts/preflight.sh` | Read-only context, platform, delivery-integrity, values, and chart checks. |
| `scripts/prepare-namespace.sh` | Creates only the reviewed application namespace; dry-run by default. |
| `scripts/import-images.sh` | Calls the VAST delivery image loader after checksum verification; dry-run by default. |
| `scripts/deploy.sh` | Creates an immutable local review set, then installs PostgreSQL, the namespace-scoped operator, and the backend only when that set still matches; review mode by default. |
| `scripts/verify.sh` | Read-only assertions for Helm, workloads, PVCs, VAST resources, Knative objects, Secrets, and image-pull state. |
| `scripts/json-check.py` | Internal fixed-purpose JSON assertions; it never prints Secret values. |
| `scripts/manifest-check.py` | Offline structural check of the rendered IngestionPipeline; it uses only Python's standard library and does not require installed CRDs. |

The Zot, Zarf, VAST permissions, DataEngine, and DataEngine client procedures
remain in [`../dataengine/`](../dataengine/). Complete that foundation before
using these scripts.

## Required inputs

- Vendor compatibility guidance covering the exact VAST, DataEngine,
  InsightEngine, OpenShift, Knative, and VAST CSI versions. Record the live
  versions with the deployment evidence and stop when the site combination is
  outside that guidance.
- The complete VAST InsightEngine delivery archive for the selected release.
- Python 3, used for fixed-purpose JSON and offline rendered-manifest checks.
- An approved OCI registry reachable by OpenShift and DataEngine.
- A tested VAST CSI StorageClass.
- Separate VAST PROTOCOLS and Query Engine VIP services for S3/Kafka and vector
  queries.
- An existing Event Broker. The operator creates the dedicated pipeline topic
  from `IngestionPipeline.spec.topic`; provision any auxiliary audit, metrics,
  or status topics explicitly when the release and site design require them.
  In release 5.4.3, `IngestionPipeline.spec.topic.broker` contains the
  underlying VAST Event Broker name, not the `VMSKafkaBroker` custom-resource
  metadata name. Record and verify both values separately.
- Reviewed copies of the delivery's PostgreSQL, operator, and backend values.
  Credentials must be referenced through existing Kubernetes Secrets, not
  stored in the values files.
- Reachable model endpoints whose embedding dimension is identical in the
  ingest and backend configurations.
- One application/manager username and password used consistently by VAST
  provisioning and runtime `MGMT__USERNAME` / `MGMT__PASSWORD`.

## 1. Prepare the release inputs

Run from the repository root:

```bash
cd AI-Data-Platform-CVD/insightengine
cp release-inputs.example.env release-inputs.env
${EDITOR:-vi} release-inputs.env
```

Replace every `REQUIRED` value. Keep `release-inputs.env` and the reviewed
site-values files outside source control. Chart inputs must be relative paths
to regular, non-symlink files inside `INSIGHTENGINE_BUNDLE_DIR`; path traversal
and symlinked chart components are rejected. The three values files and the
local input file must also be regular, non-symlink files. The input file is
trusted shell configuration: keep it operator-owned and limit it to reviewed
variable assignments and comments; never put commands or credentials in it.

## 2. Run the read-only preflight

```bash
./scripts/preflight.sh --env release-inputs.env
```

Proceed only when the command identifies the intended OpenShift API, context,
and identity; verifies the complete extracted VAST delivery; confirms
DataEngine, Knative, CSI, permissions, and chart inputs; and renders all three
charts without an untagged or `latest` image or an inline credential Secret.
The preflight also asserts the required 5.4.3 settings: `v54.enabled=true`,
document-only ingestion, impersonation disabled, the exact PostgreSQL Service
host, and the reviewed policy, model/dimension, pipeline, broker, registry,
image, and Secret references. Model API reachability remains a manual gate
because endpoint authentication is intentionally outside these scripts.

Verify the vendor-supplied checksum for the outer delivery archive before
extracting it. The preflight script then runs the delivery's internal
`tools/verify-checksums.py` against the extracted files.

## 3. Create the application namespace

Preview and apply:

```bash
./scripts/prepare-namespace.sh --env release-inputs.env
./scripts/prepare-namespace.sh --env release-inputs.env --apply
```

The apply operation creates only `INSIGHTENGINE_NAMESPACE`. It does not create
Secrets, change SCCs, or modify VAST.

## 4. Provision the VAST prerequisites

For a new deployment, follow the 5.4.3 runbook and run the
delivered `tools/setup-vast-ie.py` in dry-run mode before applying it. With an
existing DataEngine deployment, the 5.4.3 runbook uses `--local
--skip-dataengine`; this provisions the per-pipeline VAST foundations without
recreating or repointing the tenant-level DataEngine broker, default topic,
compute cluster, or registry.

The scripts deliberately do not accept VAST administrator credentials and
does not wrap this vendor utility. Set `PROVISIONING_MODE=vendor-helper` only
after completing the release-runbook procedure. The validated existing
environment used a pre-provisioned path; do not rerun the helper against such
an environment. Select `preprovisioned` only when VAST Support has confirmed
the same manager, application user/group, VIP pools, CSI policy, S3/VAST
Database foundations, and broker prerequisites already exist.

Whichever path is selected, the app/manager name and password supplied during
provisioning must exactly match runtime `MGMT__USERNAME` and
`MGMT__PASSWORD`. Record the confirmation without recording the password.

## 5. Complete VAST namespace and registry reconciliation

After the namespace exists, use the VAST management interface to:

1. Edit the existing DataEngine compute cluster and add the InsightEngine
   namespace while preserving every existing namespace.
2. Edit the existing DataEngine registry record, associate the same compute
   cluster and namespace, and save the record again.
3. Confirm the existing Event Broker, S3 policy/view, VAST Database view, and
   separate PROTOCOLS and Query Engine VIP services. The operator creates the
   dedicated pipeline topic during reconciliation. Confirm separately
   provisioned auxiliary topics when the site values reference them.
4. Confirm that the application identity is not waiting for a mandatory
   password change and that the registry auth mode is the reviewed `secret`,
   `password`, or `none` mode.
5. Establish the required CA trust without bypassing TLS. Query policy
   assignment occurs after Helm creates the policy CR.

Zot does not grant access by OpenShift namespace. The VAST registry association
authorizes DataEngine placement; Kubernetes image-pull Secrets authenticate
the actual pod pulls. Both controls are required.

Set `VAST_MAPPING_CONFIRMED=true` in the local input file only after the VAST
administrator verifies the two associations.

## 6. Create the credential resources

Create the following resources with the site's approved secret-management
method. Do not place their values in this repository or in Helm command-line
arguments.

| Secret role | Input variable | Used by |
| --- | --- | --- |
| InsightEngine runtime | `RUNTIME_SECRET_NAME` | Backend and release-specific secret shim |
| PostgreSQL authentication | `POSTGRES_SECRET_NAME` | PostgreSQL chart |
| Application image pull | `APP_PULL_SECRET_NAME` | PostgreSQL, operator, and backend |
| DataEngine workload image pull (only for registry `auth_type=secret`) | `INGEST_PULL_SECRET_NAME` | VAST registry record and DataEngine-generated Knative workloads |

The reviewed PostgreSQL values must use the chart's existing-Secret interface.
The backend reads `POSTGRES__PASSWORD` from `RUNTIME_SECRET_NAME`; its value
must match the PostgreSQL password stored under the chart's supported key in
`POSTGRES_SECRET_NAME`.
PostgreSQL, operator, and backend values must reference
`APP_PULL_SECRET_NAME`. The IngestionPipeline carries only the VAST registry
name. DataEngine resolves that registry record and, for `auth_type=secret`,
injects `INGEST_PULL_SECRET_NAME` into the generated Knative Service, Revision,
and Pod. Do not require that Secret for a reviewed `password` or `none` mode.

## 7. Import the delivery images

Authenticate the container client to the approved registry without placing the
password in command history. Then preview and apply:

```bash
./scripts/import-images.sh --env release-inputs.env
./scripts/import-images.sh --env release-inputs.env --apply
```

The apply command first runs the delivery checksum verifier and then calls the
VAST-supplied `tools/load-and-push-images.sh`. Apply the exact image locations
printed by that tool to the reviewed values files before deployment.

## 8. Deploy InsightEngine

Create the exact review set without changing the cluster:

```bash
./scripts/deploy.sh --env release-inputs.env
```

The default review directory is `rendered/`; select another private location
with `--output-dir <path>`. Review mode requires a new or empty private
directory and refuses broad paths, symbolic links, and existing content. It
publishes the three manifests, approved image inventory, deployment intent,
and `review-lock.sha256`, then prints the exact server-dry-run and apply
commands. The lock covers the selected input file, all three chart archives,
all three values files, the rendered manifests, image inventory, and intent.
Review every file before applying. The lock is change detection for the trusted
operator's review directory; it is not a detached approval signature.

After platform and VAST review, install them:

```bash
./scripts/deploy.sh --env release-inputs.env --apply
```

Use the same `--output-dir` on both commands when a non-default directory was
selected. Apply mode never rewrites the reviewed files. Before the first Helm
write, it requires the complete allowlisted set, recomputes every hash, checks
the deployment intent, rerenders into temporary storage, and compares the
current manifests and image inventory byte-for-byte with the reviewed copies.
It checks the lock again immediately before each install. Any changed or
missing input or artifact stops the operation; create and review a new empty
directory instead of editing or replacing the old set.

The sequence is fixed:

1. PostgreSQL in the application namespace, using the selected VAST CSI
   StorageClass and an existing credential Secret.
2. The release-matched, namespace-scoped InsightEngine Operator.
3. The release-matched InsightEngine backend and ingestion pipeline.

The release runbook's provisioning utility creates the tenant and per-pipeline
foundations. The backend chart and operator then create or adopt their
namespace-scoped VAST custom resources and reconcile the dedicated pipeline
topic and trigger. They do not automatically create every auxiliary telemetry
topic declared in backend configuration.

## 9. Assign query policy after reconciliation

Wait for the expected `VmsS3Policy` custom resource to reach `Ready`. Then use
the VAST management interface to attach `EndUserVastDBPolicy-<tenant>` to every
local user that will query InsightEngine. Set
`END_USER_POLICY_ASSIGNMENT_CONFIRMED=true` only after that assignment. This
step cannot occur before Helm because the backend/operator creates the policy.

## 10. Verify the deployment

```bash
./scripts/verify.sh --env release-inputs.env
```

The script fails when a required chart version, rendered/running image,
release-owned controller, PostgreSQL PVC, named pipeline/topic/broker, VAST
custom resource, live Helm manifest contract, or typed Secret/key is absent or
unhealthy. It follows the selected pipeline label from its one KService and
Trigger to the Trigger-selected Broker, the KService's latest Ready Revision,
and that revision's current Pod; unrelated Knative workloads are ignored. For
a secret-backed VAST registry it also proves that those selected objects
reference the expected ingestion pull Secret. It prints Warning events for
troubleshooting; historical events alone do not override the current pod-state
assertions. Final acceptance still requires a **new private
collection**, a new source object, completed ingestion, successful retrieval,
and a grounded response using the AIDP Workloads acceptance procedure.

Use an uncached pinned image when validating registry access. A Ready pod that
reuses a cached layer is not proof that either pull Secret or the VAST registry
association works.

## Release-specific notes

- Keep the S3/Kafka endpoint on a PROTOCOLS VIP service and the vector-query
  endpoint on a Query Engine VIP service.
- The ingest and backend embedding dimensions must match.
- A pipeline trigger is release-defined. In the validated 5.4.3 reference it
  is tag based, not a raw object-created notification.
- The validated 5.4.3 profile requires `v54.enabled=true`, an exact match
  between `v54.postgres.host` and the PostgreSQL Service, matching PostgreSQL
  passwords in the two Secrets, private document collections, text-only
  ingestion, no SyncEngine connectors, and impersonation disabled.
- Do not publish raw backend logs. The validated 5.4.3 backend can expose
  bearer material at INFO level.
- Broad SCC grants are not part of these scripts. Apply only the
  release-supported, security-reviewed SCC/RBAC configuration.
- The backend pre-delete hook does not inherit the backend pull-Secret list.
  Keep its pinned kubectl image directly reachable or bind the approved Secret
  through the release-supported ServiceAccount path before relying on cleanup.
