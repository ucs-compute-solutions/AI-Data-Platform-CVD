# AI Data Platform workload companions

These directories contain customer-facing deployment and acceptance companions
for the validated AI Data Platform workloads. Complete the platform foundation
and the VAST InsightEngine deployment before starting a workload procedure.

| Workload | Companion | Primary data plane |
| --- | --- | --- |
| Document RAG | [`document-rag/`](document-rag/) | VAST S3, DataEngine ingestion, VAST Database, local NVIDIA NIMs |
| VAST-native VSS | [`vast-native-vss/`](vast-native-vss/) | VAST S3, DataEngine functions and triggers, VAST Database |
| NVIDIA VSS 3.2.1 Search | [`nvidia-vss-3.2.1-search/`](nvidia-vss-3.2.1-search/) | NVIDIA VIOS, Kafka, Elasticsearch, VAST CSI persistence |

Use the same operating sequence for each companion:

1. Check out the CVD repository release selected for the deployment.
2. Copy the example input file to the ignored local input filename.
3. Run the read-only preflight and review every target it reports.
4. Preview any deployment or rollback command before applying it.
5. Run the workload acceptance procedure and retain the pass evidence.

Credentials, API keys, tokens, kubeconfigs, and private certificates do not
belong in these directories. Create them through the site's approved secret
management process and reference only the Kubernetes Secret names required by
the applicable platform companion.
