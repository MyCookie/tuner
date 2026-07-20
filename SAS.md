# System Architecture Specification: Enterprise Fine-Tuning Pipeline (Tuner)

**Project Goal:** To build a modular, cloud-native pipeline for the ingestion, refinement, and fine-tuning of Large Language Models (LLMs), starting with text and expanding to multimodal (image/audio) capabilities.

**Target Environment:** 
*   **Development:** Local CUDA Environment (128GB Coherent Memory), Docker, MinIO.
*   **Production:** Kubernetes (K8s), Kubeflow, AWS S3 / Azure Blob Storage / GCP Storage.

---

## 1. High-Level System Architecture
The system is designed as a **Decoupled Micro-Service Pipeline**. Each stage of the pipeline exists as a stateless Docker container. Communication between stages is handled via an orchestration layer (Kubeflow) and a shared object storage layer (MinIO/S3).

### 1.1 Logical Flow
`Data Source` $\rightarrow$ `Ingestor` $\rightarrow$ `Cleaner` $\rightarrow$ `Judge (Eval)` $\rightarrow$ `Tokenizer` $\rightarrow$ `Trainer` $\rightarrow$ `Model Registry` $\rightarrow$ `Inference Engine`

---

## 2. Data Engineering & Contract
To ensure auditability and scalability, the pipeline implements a **Medallion Architecture** (Bronze $\rightarrow$ Silver $\rightarrow$ Gold).

### 2.1 Storage Tiers
| Tier | Format | Purpose | Data State |
| :--- | :--- | :--- | :--- |
| **Bronze** | JSONL | Preservation | Raw, untouched source data wrapped in a metadata envelope. |
| **Silver** | JSONL | Standardization | Cleaned, deduplicated, and converted to a Multimodal Conversation schema. |
| **Gold** | JSONL | Quality Assurance | Scored and filtered data ready for tensorization. |
| **Artifact** | SafeTensors | Model Consumption | Tokenized binary tensors and final model weights. |

### 2.2 The Multimodal Contract (Silver/Gold)
To support future image and audio integration, the system uses a **Content Array** rather than flat strings:
```json
{
  "id": "uuid",
  "conversation": [
    {
      "role": "user",
      "content": [
        {"type": "text", "value": "..."},
        {"type": "image", "value": "s3://bucket/path/img.jpg"}
      ]
    },
    { "role": "assistant", "content": [{"type": "text", "value": "..."}] }
  ],
  "evaluation": { "score": 0.0, "judge_model": "model_name", "reasoning": "..." }
}
```

---

## 3. Pipeline Component Specifications

### 3.1 Component Breakdown
*   **Ingestor:** Abstraction layer that converts SQL, CSV, PDF, or API streams into Bronze JSONL.
*   **Cleaner:** Performs deterministic scrubbing (regex, length filters, PII removal) and converts data to the Silver schema.
*   **Judge:** A separate LLM-based component that scores Silver records. Only records meeting a specific threshold are promoted to Gold.
*   **Tokenizer:** Maps Gold records to the specific model vocabulary, producing binary tensors and an `index_map` for traceability.
*   **Trainer:** Executes fine-tuning. 
    *   **Primary Method:** QLoRA (Quantized Low-Rank Adaptation) for portability.
    *   **Secondary Method:** Full Parameter Fine-Tuning (for models < 3B parameters).

### 3.2 Experiment Tracking & Registry
*   **Tracking:** **MLflow** is used to log hyperparameters, loss curves, and dataset versions.
*   **Registry:** Final model weights are stored as versioned artifacts in S3/MinIO, including a manifest file linking the model to the exact Gold dataset version used.

---

## 4. Infrastructure & Security

### 4.1 Cloud-Native Strategy
*   **Orchestration:** The pipeline is built as **Kubeflow-ready components**. Each step is a standalone image that reads/writes to S3, ensuring a seamless transition from local Docker Compose to a Kubernetes cluster.
*   **Storage:** MinIO provides S3-API compatibility locally, allowing the same `boto3` code to function in the cloud.

### 4.2 Security Posture
*   **Secret Management:** No hardcoded keys. All credentials are passed via **K8s Secrets** as environment variables.
*   **IAM:** Strict bucket-level permissions (e.g., Ingestor cannot write to Gold; Trainer cannot read Bronze).
*   **Hardening:** All containers run as **non-root users** using slim base images.
*   **Integrity:** Model weights are stored in **SafeTensors** format to prevent arbitrary code execution (Pickle attacks).

---

## 5. Deployment & Inference
To move from a `.bin` file to a production tool, the following deployment stack is specified:

*   **Inference Engine:** **vLLM** or **NVIDIA Triton** for high-throughput serving (PagedAttention/Continuous Batching).
*   **Release Strategy:** **Canary Deployments** (routing a small % of traffic to new fine-tunes) to compare performance against the base model in real-time.
*   **Fallback:** A "Base Model" instance remains active to handle fail-overs if a fine-tuned version exhibits regression.

---

## 6. Execution Roadmap (Phased Implementation)

### Phase 1: Local MVP (The "Steel Thread")
*   Set up MinIO and Docker.
*   Build a basic text-only pipeline: `Ingest` $\rightarrow$ `Clean` $\rightarrow$ `Train`.
*   Implement a simple JSONL contract.

### Phase 2: Enterprise Hardening
*   Implement the `Judge` component and Gold-tier filtering.
*   Integrate **MLflow** for experiment tracking.
*   Migrate to **SafeTensors** and implement the Model Registry.

### Phase 3: Cloud-Native Migration
*   Deploy the pipeline onto a **Kubernetes** cluster.
*   Migrate orchestration from scripts to **Kubeflow Pipelines (KFP)**.
*   Move storage from local MinIO to **Cloud S3**.

### Phase 4: Multimodal Expansion
*   Update the `Ingestor` and `Cleaner` to handle image/audio binary assets.
*   Implement modality-specific evaluation (e.g., CLIP for image quality).
*   Fine-tune a multimodal model (e.g., LLaVA or similar).
