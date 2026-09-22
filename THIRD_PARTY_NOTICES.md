# Third-party notices

The repository license applies to original companion documentation, scripts,
templates, and examples contributed to this repository. Third-party software,
container images, models, datasets, and source repositories remain subject to
their own licenses and entitlement terms.

## NVIDIA Video Search and Summarization

The NVIDIA VSS companion targets the following upstream source:

- Repository: <https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization>
- Release: `v3.2.1`
- Commit: `7640d917047cf7b0fd3085eefb8282754b56bc94`
- Upstream license: <https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization/blob/v3.2.1/LICENSE>

This repository contains an OpenShift adaptation patch and configuration
overlays; it does not include the NVIDIA VSS source tree, container images,
models, or sample datasets. Obtain those artifacts from NVIDIA and comply with
the applicable NVIDIA licenses and entitlements.

The Warehouse Operations implementation starter references NVIDIA VSS
Warehouse application data and protected NGC container images. Neither the
dataset nor the images are redistributed. Obtain them directly from NVIDIA
using an appropriately scoped NGC key and the required organization
entitlements.

## VAST VSS Blueprint

The VAST-native VSS companion targets the following upstream source:

- Repository: <https://github.com/vast-data/vss-blueprint>
- Commit: `8b34c2c919edcec6b7bd51cf9ff09722d3dda879`

This repository contains reviewed patch files and dependency lock data used
with a separately obtained checkout; it does not include the complete VAST
source tree. The source lock and patches do not replace the upstream terms.

## Package and model dependencies

Dependency lockfiles identify third-party packages but do not include their
source distributions. NVIDIA NIM containers, model artifacts, NGC resources,
and Hugging Face resources are not redistributed here. Each dependency and
artifact remains governed by its original license and access terms.
