# Protected NVIDIA Image Resolution

Status: complete for the four protected NVIDIA source images. On
September 22, 2026, the existing NGC API key authenticated to `nvcr.io` and
resolved every pinned image. A separate NGC Service Key was not required for
these repositories in the validated organization. No image was pulled and no
OpenShift resource was changed.

## Verified immutable references

The source tags come from NVIDIA VSS `v3.2.1`, commit
`7640d917047cf7b0fd3085eefb8282754b56bc94`. The manifest digest is used by the
Helm chart; the platform digest records the selected `linux/amd64` image.

| Component | Source tag | Manifest digest | `linux/amd64` platform digest |
|---|---|---|---|
| Alert Bridge | `nvcr.io/nvidia/vss-core/vss-alert-verification:3.2.0` | `sha256:a36745d216ca2396acb2491c75f3af05884e207e782c445e76b06df4976aa275` | `sha256:22090dc1c6741e1829a79fc04e2bb151fead6f8ec8a238ada6eec5a24a1dccf4` |
| Blueprint Configurator | `nvcr.io/nvidia/vss-core/vss-configurator:3.2.1` | `sha256:35e3e31e7d9e62b298d6dbcb91244d54b0686845227f26e46f886493e9fe4504` | `sha256:6421bfb4c3a94ecb0c19142f28ae2d0dba9a3292862db87bf6b6cd40cc5fc9a6` |
| RTVI-VLM | `nvcr.io/nvidia/vss-core/vss-rt-vlm:3.2.1` | `sha256:5403e0c8fa8b149e7ad15ab1b063b78d610e7a50297dba6ca550ac5cc5ef9504` | `sha256:156bc152242ce38d7913ebb75806b3101e815d93755e371507b1dc5c988746cc` |
| Nemotron Nano NIM | `nvcr.io/nim/nvidia/nvidia-nemotron-nano-9b-v2:1` | `sha256:a2f4a5aefe7dd0ff29bfd8d7081ce4977337d1b12081361af7b6283ff9a406b2` | `sha256:bdd975848d5d4e2ae1f701a9b78ff7f6ed7de56498f9c2f250d7d98484b0d40f` |

Two earlier login attempts were rejected before image resolution. A later run
with the NGC API key authenticated successfully and resolved all four images,
so those earlier results are not evidence of a Service Key or entitlement
requirement.

## Credential-safe verification procedure

Run the wrapper from the deployment client. It stores registry authentication
in a temporary directory, never prints the key, limits resolution to
the four protected source references, writes a digest-only JSON result under
`/tmp`, and removes the temporary login on exit.

```bash
# Run from the repository root.
./demos/warehouse-operations/scripts/resolve-protected-ngc-images.sh
```

Enter an NGC API key that can read the pinned repositories. A successful run
reports `failed: 0`, `resolved: 4`, and a manifest and platform digest for each
image. HTTP 401 or a rejected registry login means the key did not authenticate.
HTTP 403 means the authenticated identity cannot read that repository. Do not
substitute `latest` or another tag.

The verified result is recorded in
[`source-lock.yaml`](../openshift/nvidia-warehouse/source-lock.yaml) and the
matching Helm chart [`values.yaml`](../openshift/nvidia-warehouse/chart/values.yaml).
The temporary JSON evidence contains no credential and must not be treated as a
registry login file.

NVIDIA documents NGC key types and registry authentication in the
[NGC User Guide](https://docs.nvidia.com/ngc/latest/ngc-user-guide.html).
