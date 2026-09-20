# Deploy VAST DataEngine

This directory is the executable companion to the **Deploy VAST DataEngine**
section of the AI Data Platform CVD. It prepares an authenticated Zot registry,
initializes the Zarf runtime supplied with the selected VAST release, deploys
the release-matched DataEngine package, and verifies the OpenShift resources.

The VAST release notes, compatibility matrix, package README, checksums, and
included binaries remain authoritative. Do not mix Zarf binaries, init
packages, DataEngine packages, charts, or images from different releases.

## Files

| Path | Purpose |
| --- | --- |
| `release-inputs.example.env` | Site and release inputs. Copy it locally; do not commit the completed file. |
| `registry/zot-values.yaml.tpl` | Pinned authenticated Zot configuration with persistent storage, a NodePort service, an edge-terminated Route, and no embedded password. |
| `permissions/vast-permissions-values.yaml` | Namespace list for the release-supplied VAST permissions chart. Review the resulting SCC bindings before use. |
| `scripts/preflight.sh` | Read-only OpenShift readiness, authorization, release-integrity, storage, ingress, and tooling checks. |
| `scripts/deploy-zot.sh` | Plans or reconciles Zot without dropping existing users, configures Route trust, and verifies MachineConfigPool convergence. |
| `scripts/init-zarf.sh` | Initializes the explicit release package and architecture with deny-by-default, labeled mutation. |
| `scripts/deploy-dataengine.sh` | Verifies artifacts before change, labels only the approved namespaces, and deploys the VAST package. |
| `scripts/verify-dataengine.sh` | Asserts Zot, namespace labels, workload availability, pod health, and image mutation without making changes. |

## Required inputs

- A supported VAST and DataEngine release combination.
- A healthy OpenShift cluster and cluster-administrator access.
- Working DNS and HTTPS ingress for the Zot route.
- A tested StorageClass for the Zot persistent volume.
- The VAST-supplied DataEngine bundle, including its `zarf` binary, init
  package, DataEngine package, README, and checksums.
- The VAST-supplied permissions chart that matches the selected release.
- An approved registry username. The scripts prompt for its password and never
  place the clear-text value in command arguments, the repository, or terminal
  output.
- A Linux OpenShift administration host with Bash 4 or later, GNU
  `sha256sum`/`base64`, `oc`, Helm, `jq`, `curl`, `envsubst`, and `htpasswd`.

## 1. Prepare site inputs

Run from the repository root:

```bash
cd AI-Data-Platform-CVD/dataengine
cp release-inputs.example.env release-inputs.env
${EDITOR:-vi} release-inputs.env
```

Set every value marked `REQUIRED`. Keep `release-inputs.env` outside source
control. Confirm that `ZOT_CHART_VERSION` remains approved for the selected
VAST release; `0.1.113` records the version used by the validated design.
Record the release-provided SHA-256 values for the Zarf binary, init package,
and DataEngine package. Review `ZARF_MUTATION_NAMESPACES`; platform namespaces
such as `openshift-*`, `kube-*`, and `default` are deliberately rejected.
For a custom ingress certificate, set `ZOT_CA_BUNDLE` to its PEM issuer bundle;
otherwise the scripts use the OpenShift default ingress CA. Zarf receives this
bundle through `SSL_CERT_FILE`, so no TLS-bypass option is required.

## 2. Run the read-only preflight

```bash
./scripts/preflight.sh --env release-inputs.env
```

Proceed only when the command identifies the intended OpenShift API and
context, the selected StorageClass exists, the VAST bundle files are present,
and the required client tools are available.

## 3. Deploy Zot and establish OpenShift registry trust

Review the planned changes first:

```bash
./scripts/deploy-zot.sh --env release-inputs.env
```

Apply them after approval:

```bash
./scripts/deploy-zot.sh --env release-inputs.env --apply
```

For a new registry installation, the script prompts twice for the Zot password. On a rerun it
extracts and reuses the existing `htpasswd` Secret, preserving every registry
user without requesting a password. It stops if the configured `ZOT_USER` is
not present. To deliberately add or rotate `ZOT_USER`,
review the dry run and use:

```bash
./scripts/deploy-zot.sh --env release-inputs.env --rotate-credentials
./scripts/deploy-zot.sh --env release-inputs.env --rotate-credentials --apply
```

After rotation, update the registry credential stored in VAST before deploying
new workloads. With `--apply`, the script renders both charts before the first
Helm write, uses the validated NodePort service behind an edge-terminated
OpenShift Route, preserves existing trust and allowlist entries, and waits for
registry-trust changes to converge.

Pass when the Zot pod is Ready, its PVC is Bound to `ZOT_STORAGE_CLASS`, the
Route uses `ZOT_HOST`, authenticated `/v2/` access succeeds, and the
MachineConfigPools report `Updated=True`.

## 4. Initialize Zarf

If the cluster already contains the Zarf runtime installed for this release,
skip this step. Otherwise review and apply:

```bash
./scripts/init-zarf.sh --env release-inputs.env
./scripts/init-zarf.sh --env release-inputs.env --apply
```

The apply command verifies the Zarf binary and explicit init-package checksums,
passes the selected package and `ZARF_ARCHITECTURE` directly, and selects
`--agent-mutation-policy labeled`. The password is provided through Zarf's
configuration environment rather than its command line. If `namespace/zarf`
already exists, the script stops; use the release-specific Zarf upgrade
procedure instead of reinitializing it.

The VAST-supplied Zarf must support a positional init package,
`--architecture`, `--agent-mutation-policy labeled`, and the
`ZARF_INIT_REGISTRY_*` configuration environment variables. The preflight
fails closed when the mutation-policy flag is unavailable. Confirm the other
interfaces against the README shipped with the selected VAST release.

## 5. Deploy the DataEngine package

Review the target namespaces and package:

```bash
./scripts/deploy-dataengine.sh --env release-inputs.env
```

Apply after approval:

```bash
./scripts/deploy-dataengine.sh --env release-inputs.env --apply
```

Before changing the cluster, the script verifies the Zarf binary and
DataEngine package. It creates only the namespaces in
`ZARF_MUTATION_NAMESPACES` and applies both `zarf.dev/agent=mutate` and
`zarf.dev/vast=mutate`. With the labeled Zarf policy, resources outside that
reviewed allowlist are not redirected to Zot.

## 6. Enable DataEngine in the VAST management interface

Complete these four stages with site-specific values:

1. Assign the approved Event Broker, default topic, dead-letter topic, and CA.
2. Link the OpenShift API by mutual TLS using the cluster CA, client
   certificate, and private key.
3. Select only the namespaces approved for DataEngine workloads.
4. Link the Zot registry to the same Kubernetes cluster and use the approved
   registry identity or Kubernetes Secret.

Record the Event Broker VIP, bucket/view owner, policy, topic names and
partition counts, certificate source, selected namespace list, and registry
record as site inputs. OpenShift node trust does not by itself establish trust
from VAST to the Route; use a certificate chain accepted by VAST or import the
approved issuer by the method supported for the selected VAST release.

If an application namespace is added later, update both the Kubernetes-cluster
namespace selection and the registry assignment, then save the registry record
again so VAST reconciles the mapping.

## 7. Verify DataEngine

```bash
./scripts/verify-dataengine.sh --env release-inputs.env
```

If a compatible and configured `vastde` client is installed, also run:

```bash
vastde version
vastde functions list
vastde triggers list
vastde pipelines list
```

The script fails if the Zot PVC/Route, scoped labels, pods, Deployments,
StatefulSets, or image checks are unhealthy. Final acceptance additionally
requires a successful VAST UI enablement state and one new pinned-image
function or pipeline invocation. Use an uncached image so the test proves the
registry path and credentials.

## Security notes

- Do not commit `release-inputs.env`, registry passwords, `htpasswd` files,
  S3 keys, client private keys, kubeconfigs, or Kubernetes Secret contents.
- Do not add TLS-bypass options. Correct the certificate chain or trust bundle.
- The release permissions chart can grant broad SCC access. Review its rendered
  objects with the platform security owner and use only the namespaces required
  by the selected release.
- Zot provides the validated compact registry pattern. Use the organization’s
  supported enterprise registry when production policy requires HA, backup,
  scanning, or external identity integration.
