# NVIDIA VSS 3.2.1 Search — CVD companion files

This directory contains the site-neutral inputs used by the **Deploy NVIDIA VSS 3.2.1 Search** CVD section. It adapts the NVIDIA VSS 3.2.1 developer Search profile to Red Hat OpenShift while retaining NVIDIA Kafka, Logstash, and Elasticsearch as the Search data plane and using VAST CSI-backed persistent volumes.

The files do not contain credentials, private endpoints, node names, or a complete copy of the NVIDIA source tree. Obtain the pinned NVIDIA source and model entitlements separately.

The critic overlay deliberately pins the validated RTX PRO 6000 Blackwell GPU type, Cosmos3 image, and model profile. Revalidate those fields before using different GPU hardware or a different Cosmos3 release.

Review the [visual workflow guide](../../docs/nvidia-vss-3.2.1-search.md)
before using this deployment companion.

## Directory contents

```text
nvidia-vss-3.2.1-search/
├── README.md
├── release-inputs.example.env
├── source-lock.yaml
├── manifests/
│   ├── namespace-rbac.yaml.tpl
│   └── routes.yaml.tpl
├── scripts/
│   ├── lib.sh
│   ├── check-entitlements.sh
│   ├── preflight.sh
│   ├── render.sh
│   ├── deploy.sh
│   └── verify.sh
└── values/
    ├── bootstrap.yaml
    ├── site-values.example.yaml
    ├── steady-state.yaml
    └── critic.yaml
```

The reviewed OpenShift patch is maintained at `patches/nvidia-vss-3.2.1-search/openshift-v321.patch` in the CVD repository.

## Prepare the inputs

```bash
git clone https://github.com/ucs-compute-solutions/AI-Data-Platform-CVD.git \
  AI-Data-Platform-CVD
cd AI-Data-Platform-CVD/workloads/nvidia-vss-3.2.1-search

cp release-inputs.example.env release-inputs.env
${EDITOR:-vi} release-inputs.env
```

`release-inputs.env` is the single authority for site-specific hosts, StorageClass, node selectors, Secret names, model endpoint, Kubernetes version, and render location. The wrapper substitutes those values into the reviewed `site-values.example.yaml`; do not create a second edited values file. Use different DNS names for `CVD_ROUTE_HOST` and `CVD_STREAMER_ROUTE_HOST` because both Routes are pathless. Set `CVD_LLM_BASE_URL` to only the OpenAI-compatible service origin, including its explicit port—for example, `http://llm.example.svc:8000`—with no `/v1`, trailing slash, query, or fragment. The NVIDIA Agent appends `/v1` itself.

After render review and change approval, apply the rendered namespace/RBAC manifest, then create the two required Secrets through the approved secret-management workflow. Do not put an NGC API key in an environment file, values file, shell history, or Git repository.

## Prepare the pinned NVIDIA source

The CVD keeps this checkout clean. Git LFS payload download is disabled because
only the Helm source and its pointer files are required; the render wrapper
creates a private snapshot and applies the reviewed OpenShift patch there.

```bash
git clone --no-checkout --branch v3.2.1 --depth 1 \
  https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization.git \
  <nvidia-vss-source-directory>

git -C <nvidia-vss-source-directory> config filter.lfs.process ""
git -C <nvidia-vss-source-directory> config filter.lfs.smudge cat
git -C <nvidia-vss-source-directory> config filter.lfs.clean cat
git -C <nvidia-vss-source-directory> config filter.lfs.required false
git -C <nvidia-vss-source-directory> checkout --detach \
  7640d917047cf7b0fd3085eefb8282754b56bc94
git -C <nvidia-vss-source-directory> rev-parse HEAD
git -C <nvidia-vss-source-directory> status --porcelain --untracked-files=all
CVD_REPOSITORY_ROOT=$(git rev-parse --show-toplevel)
git -C <nvidia-vss-source-directory> apply --check \
  "${CVD_REPOSITORY_ROOT}/patches/nvidia-vss-3.2.1-search/openshift-v321.patch"
unset CVD_REPOSITORY_ROOT
```

The commit must match `source-lock.yaml`, the status output must be empty, and
the patch must apply without offsets or rejects. Do not apply the patch to this
checkout; `render.sh` applies it to a private source snapshot.

## Verify registry and model entitlement

Install the NGC CLI version recorded in `source-lock.yaml`. If `ngc` is not in
`PATH`, point `CVD_NGC_CLI` to its absolute path. The key is accepted only at a
hidden prompt and is removed from the temporary Docker configuration when the
script exits.

```bash
export CVD_NGC_CLI=<absolute-path-to-ngc>
./scripts/check-entitlements.sh --mode steady
unset CVD_NGC_CLI
```

This verifies the exact container images, RT-DETR and SigLIP2 artifacts, and
the public Cosmos Embed repository. The upstream VSS chart retrieves the
Hugging Face repository without a revision argument, so the script is a
deployment gate: it stops if repository `HEAD` no longer matches the validated
revision in `source-lock.yaml`.

Steady mode checks only the base Search images and models. It does not require
the optional Cosmos3 Critic entitlement.

## Review, render, deploy, and verify

```bash
./scripts/preflight.sh --env release-inputs.env
./scripts/render.sh --env release-inputs.env --mode initial
```

Review the rendered manifest and the exact values before requesting a change approval. The deployment wrapper is review-only unless `--apply` is supplied.
The preview records a SHA-256 digest over the environment file, reviewed
overlays, patch, rendered resources, and private chart snapshot. The apply
command stops if any of those artifacts changed after review.

On a new deployment, the first render performs client-side validation because
a dry-run Namespace is not persisted for its namespaced objects. Apply only the
reviewed Namespace/RBAC manifest, create the approved Secrets, and rerun the
preview command; the second render performs server-side validation of the full
release.

```bash
oc apply -f <private-render-directory>/namespace-rbac.yaml
# Create the two pre-approved Secrets in the new namespace.
./scripts/deploy.sh --env release-inputs.env --mode initial
./scripts/deploy.sh --env release-inputs.env --mode initial --apply
./scripts/verify.sh --env release-inputs.env --mode initial
```

Initial mode holds RT-CV at zero replicas, waits for the model-download Job,
verifies the Job-to-PVC contract, and marks the retained RWO model PVC only
after successful completion. Review the downloader log while its 3600-second
TTL permits it:

```bash
oc -n <nvidia-vss-namespace> logs job/vss-rtvi-cv-download-models
```

Then preview and apply steady mode. It disables the downloader before starting
the RT-CV replica. Every later baseline upgrade must retain this overlay.

```bash
./scripts/deploy.sh --env release-inputs.env --mode steady
./scripts/deploy.sh --env release-inputs.env --mode steady --apply
./scripts/verify.sh --env release-inputs.env --mode steady
```

Enable the optional Cosmos3 critic only after its GPU capacity, model
entitlement, cache plan, and the NVIDIA NIM Operator version recorded in
`source-lock.yaml` are approved. The critic overlay creates the NIMCache and
NIMService; verification waits for the generated endpoint:

```bash
./scripts/deploy.sh --env release-inputs.env --mode critic
./scripts/deploy.sh --env release-inputs.env --mode critic --apply
./scripts/verify.sh --env release-inputs.env --mode critic
```

The critic preview automatically runs the mode-specific entitlement check and
prompts for the NGC key. If the locked NGC CLI is not in `PATH`, export
`CVD_NGC_CLI` before the preview. The later apply is bound to that reviewed
preview digest.

The verification wrapper checks the exact Deployment, StatefulSet, Service,
PVC, endpoint, Route, and optional Cosmos3 NIM contracts. It is a platform
gate, not the functional workload test. Complete acceptance with a fresh MP4:
upload it in the VSS UI, confirm VIOS registration, search for a visible event,
play a returned clip, run grounded synthesis, and record the pass evidence
listed in the CVD section.

If the first Helm release is `failed`, inspect the downloader and warning
events first. After explicit recovery approval, the following command deletes
only the failed transient downloader Job, retains its model PVC, and retries
the reviewed initial release. If Helm reports another pending operation,
resolve that operation before retrying.

```bash
./scripts/deploy.sh --env release-inputs.env --mode initial --apply --retry-initial
```

For rollback, select only a recorded steady or critic revision; the initial
revision intentionally leaves RT-CV at zero replicas. Use Helm `--wait` with
the approved timeout, reapply the reviewed Routes, and run `verify.sh` with the
matching steady or critic mode. Retain all model and stateful-service PVCs.

## Publication and support boundary

- Pin the NVIDIA tag, source commit, chart version, and image versions. Where
  the chart cannot pass a model revision, enforce the recorded revision as a
  pre-deployment repository-HEAD gate.
- Keep the pinned source checkout clean; deploy only from the patched private render snapshot.
- Reconcile OpenShift, Kubernetes, GPU Operator, driver, Container Toolkit, and model support before deployment.
- Treat the files as a validated reference adaptation, not a statement that every OpenShift combination is NVIDIA certified.
- MP4 upload is the accepted input path. RTSP, direct VAST S3 import, arbitrary image upload, OCR/ALPR, and a unified VAST/NVIDIA UI are outside this procedure.
- Retain persistent claims during rollback. Never delete model caches or stateful-service PVCs as part of a routine Helm rollback.
