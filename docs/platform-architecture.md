# AI Data Platform architecture

The design combines Cisco compute and networking, VAST data services, NVIDIA
accelerated AI, and Red Hat OpenShift. The infrastructure is shared, while
each workload keeps its own namespace, data path, and index.

## Solution view

```mermaid
flowchart TB
    subgraph W[Validated AI workloads]
        direction LR
        RAG[Document RAG]
        VVSS[VAST-native VSS]
        NVSS[NVIDIA VSS 3.2.1 Search]
    end

    subgraph P[OpenShift platform and accelerated compute]
        direction LR
        OCP[Cisco UCS C225 M8<br/>Red Hat OpenShift]
        GPU[Cisco UCS C845A M8<br/>NVIDIA RTX PRO 6000 Blackwell]
        OPS[GPU and NIM Operators<br/>DataEngine and InsightEngine services]
    end

    subgraph D[VAST AI data services]
        direction LR
        EBOX[Cisco E-Box<br/>VAST Data Platform]
        DATA[VAST S3, VASTDB, Event Broker<br/>DataEngine and InsightEngine]
    end

    subgraph N[Cisco Nexus network fabrics]
        direction LR
        AIF[AI and OpenShift fabric]
        STF[VAST storage fabric]
    end

    W --> P
    OCP --- OPS --- GPU
    P --> D
    EBOX --- DATA
    GPU --> AIF
    OCP --> AIF
    EBOX --> STF
    AIF <--> STF
```

| Layer | Primary role |
|---|---|
| Cisco UCS and Nexus | OpenShift control, GPU inference capacity, and high-speed workload and storage connectivity |
| Red Hat OpenShift | Container scheduling, namespace isolation, operators, Routes, and persistent-volume consumption |
| VAST Data Platform | Unified file and object storage, VASTDB, event services, DataEngine, and InsightEngine |
| NVIDIA software | GPU enablement, NIM model services, and the NVIDIA VSS Search application stack |

## Deployment order

```mermaid
flowchart LR
    A[1. Cable and configure<br/>Cisco infrastructure] --> B[2. Deploy or verify<br/>VAST and OpenShift]
    B --> C[3. Enable VAST CSI<br/>and GPU software]
    C --> D[4. Deploy Zot, Zarf<br/>and DataEngine]
    D --> E[5. Deploy InsightEngine<br/>and local NIMs]
    E --> F[6. Deploy and validate<br/>selected workloads]
```

Start with the [Nexus companion](../nexus/README.md), then use the
[DataEngine](../dataengine/README.md) and
[InsightEngine](../insightengine/README.md) companions before deploying a
workload.
