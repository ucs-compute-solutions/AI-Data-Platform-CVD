# VAST-native VSS workflow

The VAST-native Video Search and Summarization (VSS) lane uses VAST S3 events
and DataEngine functions to segment video, describe visible activity, create
text embeddings, and store searchable rows in VASTDB.

## Stage 1 — Upload and segment

```mermaid
flowchart LR
    A[MP4 upload or<br/>approved S3 batch sync] --> B[VAST VSS UI and API]
    B --> C[VAST S3<br/>source video and metadata]
    C --> D[DataEngine event<br/>and Knative trigger]
    D --> E[Video segmenter<br/>CPU function]
    E --> F[VAST S3<br/>five-second clips]
```

## Stage 2 — Reason, embed, and store

```mermaid
flowchart LR
    A[Five-second clip] --> B[Cosmos Reason2 8B<br/>visible activity description]
    B --> C[Nemotron Embed 1B v2]
    C --> D[2048-D text vector]
    D --> E[VASTDB writer<br/>DataEngine function]
    E --> F[VASTDB<br/>clip, timing, text and vector]
```

## Stage 3 — Search and synthesize

```mermaid
flowchart LR
    A[Video question] --> B[Nemotron Embed 1B v2<br/>query vector]
    B --> C[VASTDB search<br/>metadata and access filters]
    C --> D[Matching descriptions<br/>timestamps and clips]
    D --> E[Nemotron 3.5 Lightning<br/>optional grounded synthesis]
    E --> F[Answer and<br/>playable evidence]
```

| Function | Validated component |
|---|---|
| Video orchestration | VAST DataEngine, Event Broker, Knative functions, and VAST S3 |
| Clip understanding | `nvidia/cosmos-reason2-8b`, NIM 1.7.0 |
| Embedding | `nvidia/llama-nemotron-embed-1b-v2`, NIM 1.13.0, 2048 dimensions |
| Vector search | VASTDB |
| Synthesis | `nvidia/nemotron-3.5-lightning-30b-a3b`, NIM 2.0.9 variant, NVFP4 TP1 |

This lane stores a human-readable activity description with every vector. It
does not use the Document RAG reranker or the NVIDIA Search Critic. Acceptance
requires video segmentation, one indexed row per segment, semantic search,
playback, and a non-empty grounded synthesis. Follow the
[VAST-native VSS deployment companion](../workloads/vast-native-vss/README.md).
