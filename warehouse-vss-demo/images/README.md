# Preserve and restore Warehouse images

```mermaid
flowchart LR
    A["Back up pinned images"] --> B["Verify every local blob"]
    B --> C["Review restore targets"] --> D["Publish and map verified digests"]
```

Keep image backups outside Git on storage large enough for the complete layer
set and transfer scratch space. [required-images.json](required-images.json)
lists 25 package pins resolving to 24 distinct `linux/amd64` images; both
BusyBox pins resolve to the same platform image. Two Docker-save archives
containing seven custom images have passed local checksum verification. A full
file mirror must pass the all-inventory check below before being called complete.
See [preservation status](preservation-status.json) for the recorded outcomes.
The complete private transport archive is `warehouse-images-linux-amd64.tar`
(48,964,782,080 bytes); its SHA256 is in that status file. Keep it outside Git
and move it to retained storage before removing the client VM.

`sourceIndexDigest` identifies the multi-architecture source index, whereas
`platformDigest` identifies the preserved `linux/amd64` manifest and can differ
from `packagePin`'s digest.

## 1. Back up the pinned set

Use an authorized source registry, trusted certificates, `oc`, Python 3.10+
and an existing protected registry-auth file. Do not store credentials with
the mirror or in this repository. The custom images have no public download
URL; obtain the preserved artifacts through the approved handoff or use the
[locked build recipes](../deployment/build/README.md).

Run these commands from `warehouse-vss-demo`. This example reads one pinned image and writes a
native `oc image mirror --dir` store; it does not deploy a workload:

```bash
oc image mirror \
  'docker.io/library/busybox@sha256:7a3ebe5bfd1a4a19797d20b0c0bb39d44393e9a03fd852c0865b0f540d868df0=file://warehouse-preserved/busybox:preserved-20261116' \
  --dir /mnt/warehouse-images --registry-config /secure/auth.json
```

Repeat for every distinct inventory image using its authorized source,
`platformDigest` and `fileRepository`; normalize short source names to fully
qualified registry/repository names. Retain the whole `v2/` tree, inventory
and verification receipt together; copying only manifests is not a backup.
The [OpenShift 4.20 image command reference](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/cli_tools/openshift-cli-oc)
describes the native file transport.

## 2. Verify without registry access

```bash
python3 -B images/verify_file_mirror.py \
  --directory /mnt/warehouse-images --inventory images/required-images.json
python3 -B images/verify_docker_archive.py \
  --archive /mnt/warehouse-archives/custom-images.tar \
  --expected-images /mnt/warehouse-archives/custom-images.json
```

The file-store verifier checks all required manifests, configs and layer
blobs. The Docker-save verifier requires an independent sidecar containing
`images` records with digest-pinned `source` and config `imageId`; optional
`archiveSha256`/`archiveBytes` are also checked. It checks every Docker image,
config and layer without extraction or engine import. Docker content checks
do not prove the original registry manifest digest or engine-load success.

Pass: every required image verifies, no layer is missing or corrupt, and a
second preserved copy passes the same check. Keep archives uncompressed for
the Docker verifier. Model weights, recordings and database volumes require
their own backups; image preservation does not include them.

## 3. Review, restore and map digests

Prepare the complete mapping offline; this writes a new local plan directory,
not a registry or cluster:

```bash
python3 -B images/make_restore_plan.py \
  --registry-prefix registry.customer.example/warehouse --output image-restore-plan
```

Review all 24 publish mappings and 25 original-pin overrides. After approving
the exact destination repositories and digest-specific tags, the operator runs:

```bash
# Publishes the reviewed images to the destination registry; no cluster write.
oc image mirror --filename=image-restore-plan/mirror-inputs.txt \
  --from-dir=/mnt/warehouse-images --registry-config=/secure/auth.json
# Read-only registry checks; writes verified overrides locally only if all pass.
python3 -B images/make_restore_plan.py --verify-plan image-restore-plan \
  --registry-config /secure/auth.json
```

Verification checks all 24 target digests and `linux/amd64` platforms. Merge
`image-restore-plan/verified-image-overrides.json` into the site's
`images.reviewedImageOverrides` before rendering. Repository-only mirrors apply
only when the original digest is retained unchanged. Re-render and review the
installation plan before any separately approved cluster change. Successful
image restoration alone is not fresh-install or runtime acceptance.
