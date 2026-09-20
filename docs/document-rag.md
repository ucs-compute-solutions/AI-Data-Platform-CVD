# Document RAG workflow

Document Retrieval-Augmented Generation (RAG) uses VAST InsightEngine and
DataEngine to ingest governed content, search authorized vectors, rerank the
best passages, and generate an answer grounded in the retrieved sources.
The validated document inputs are TXT and text-based PDF.

## Stage 1 — Ingest and index

```mermaid
flowchart LR
    A[Create collection<br/>and upload document] --> B[VAST S3<br/>object and metadata]
    B --> C[DataEngine and Knative<br/>event-driven ingestion]
    C --> D[Extract text<br/>and create chunks]
    D --> E[Nemotron Embed 1B v2<br/>2048-D passage vectors]
    E --> F[VASTDB<br/>chunks, ACLs and vectors]
```

## Stage 2 — Retrieve and rerank

```mermaid
flowchart LR
    A[User question] --> B[Nemotron Embed 1B v2<br/>2048-D query vector]
    B --> C[VASTDB semantic search<br/>collection and ACL filters]
    C --> D[Nemotron RerankQA 1B v2]
    D --> E[Ranked authorized<br/>source chunks]
```

## Stage 3 — Generate a grounded answer

```mermaid
flowchart LR
    A[Question] --> C[InsightEngine<br/>grounded prompt]
    B[Ranked source chunks] --> C
    C --> D[Nemotron 3.5 Lightning<br/>30B-A3B, NVFP4 TP1]
    D --> E[Answer with<br/>source chunks]
```

| Function | Validated component |
|---|---|
| Ingestion orchestration | VAST DataEngine and Knative |
| Document and vector storage | VAST S3 and VASTDB |
| Embedding | `nvidia/llama-nemotron-embed-1b-v2`, NIM 1.13.0, 2048 dimensions |
| Reranking | `nvidia/llama-3.2-nv-rerankqa-1b-v2`, NIM 1.8.0 |
| Generation | `nvidia/nemotron-3.5-lightning-30b-a3b`, NIM 2.0.9 variant, NVFP4 TP1 |

Acceptance requires fresh document ingestion, retrieval of the expected
content, reranking, a grounded answer, and returned source chunks. Follow the
[Document RAG deployment and validation companion](../workloads/document-rag/README.md).
Changing the embedding model or vector dimension requires re-ingestion and a
new acceptance run.
