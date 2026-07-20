# Tuner Product Scope & Phased Delivery

Entry point to the Tuner engineering document set. The product is defined by [SAS.md](../SAS.md); this doc set turns the SAS into buildable specifications and fixes the delivery phasing. Start here, then read in order: [01-architecture](01-architecture.md) → [02-data-contracts](02-data-contracts.md) → the [component specs](03-components/) → [04-model-adapters](04-model-adapters.md) → [05-infrastructure](05-infrastructure.md) → [06-testing](06-testing.md) → [07-build-plan](07-build-plan.md).

## 1. Product definition

A modular, cloud-native pipeline that turns raw enterprise data into fine-tuned LLMs with full auditability: every model version traces to the exact scored dataset, cleaning rules, and experiment that produced it. Text-first, multimodal-ready. The fine-tune target model is **pluggable** ([04](04-model-adapters.md)); the initial default is Google **Gemma E4B**.

## 2. Delivery phases & milestone gates

| Phase | Content | Exit gate |
| :--- | :--- | :--- |
| **1+2 — MVP slice** (current) | Full local pipeline: Ingest(CSV/JSONL) → Clean → Judge → Tokenize → Train(QLoRA) → Smoke-test, on Docker Compose + MinIO, with MLflow tracking, SafeTensors everywhere, IAM-scoped buckets, candidate registry manifests | `tuner run` takes fixture data to a trained adapter + before/after transcript, run visible in MLflow, E2E test green ([07 T14–T15](07-build-plan.md)) |
| **2′ — Enterprise completion** | Registry promote/rollback, smoke regression scoring, checkpoint resume, SQL/PDF sources | promote→rollback cycle demonstrated; regression score produced per run |
| **3 — Cloud-native** | K8s + Kubeflow Pipelines, cloud object store, shared MLflow, Inference Engine with canary + base-model fallback | one canary release executed end-to-end in the cluster |
| **4 — Multimodal** | Asset ingestion (`tuner-assets`), multimodal contract parts live, modality evaluators (CLIP), multimodal adapter + fine-tune | a multimodal fine-tune passes its smoke gate |

The MVP is a **slice, not a fork**: every spec covers the full feature with an explicit "MVP scope" section, so later phases extend documents rather than replace them.

### MVP success criteria (verbatim commitment)

One command (`tuner run`) ingests fixture data and ends with a trained adapter plus a smoke-test transcript, with the run logged in MLflow — reproducibly, under the IAM matrix, with zero pickle artifacts.

## 3. Out of MVP scope (explicit)

Serving/canary/fallback (specified in [inference.md](03-components/inference.md)), K8s/Kubeflow ([05 §4](05-infrastructure.md)), registry lifecycle beyond `candidate` ([registry.md](03-components/registry.md)), SQL/PDF/API ingestion, near-dedup and semantic cleaning, multi-GPU training, full-parameter fine-tuning exercised in anger, all multimodal.

## 4. SAS traceability

| SAS ref | Requirement | Where specified | MVP? |
| :--- | :--- | :--- | :-: |
| §1 | Decoupled stateless micro-service pipeline, orchestrated, shared object storage | [01 §1–§2](01-architecture.md) | ✅ |
| §1.1 | Logical flow incl. Registry & Inference | [01 §1.1–1.2](01-architecture.md) | partial (no serving) |
| §2 | Medallion Bronze→Silver→Gold + Artifact | [02](02-data-contracts.md) | ✅ |
| §2.2 | Multimodal Contract (content arrays) | [02 §2](02-data-contracts.md) | ✅ shape; text-only data |
| §3.1 Ingestor | SQL/CSV/PDF/API → Bronze | [ingestor.md](03-components/ingestor.md) | CSV/JSONL only |
| §3.1 Cleaner | Deterministic scrub, PII, → Silver | [cleaner.md](03-components/cleaner.md) | ✅ |
| §3.1 Judge | LLM scoring, threshold → Gold | [judge.md](03-components/judge.md) | ✅ |
| §3.1 Tokenizer | Vocabulary mapping, tensors + index_map | [tokenizer.md](03-components/tokenizer.md), [02 §4](02-data-contracts.md) | ✅ |
| §3.1 Trainer | QLoRA primary; full FT for small models | [trainer.md](03-components/trainer.md), [04](04-model-adapters.md) | QLoRA ✅; full-FT gated, unexercised |
| §3.2 | MLflow tracking (params, loss, dataset versions) | [01 §7](01-architecture.md), [trainer.md](03-components/trainer.md), [judge.md](03-components/judge.md) | ✅ |
| §3.2 | Registry: versioned weights + dataset-link manifest | [02 §5.2](02-data-contracts.md), [registry.md](03-components/registry.md) | manifests ✅; lifecycle Phase 2′ |
| §4.1 | Kubeflow-ready components; MinIO↔S3 portability | [01 §2, §5.1](01-architecture.md), [05 §4](05-infrastructure.md) | design ✅; KFP Phase 3 |
| §4.2 | K8s Secrets / env-only credentials | [05 §1, §4, §6](05-infrastructure.md) | env-only ✅; K8s Phase 3 |
| §4.2 | Bucket-level IAM (Ingestor∤Gold, Trainer∤Bronze) | [05 §5](05-infrastructure.md) | ✅ (MinIO policies) |
| §4.2 | Non-root hardened containers | [05 §2](05-infrastructure.md) | ✅ |
| §4.2 | SafeTensors, no pickle | [02 §4–5](02-data-contracts.md), [05 §6](05-infrastructure.md) | ✅ |
| §5 | vLLM/Triton serving | [inference.md](03-components/inference.md) | Phase 3 |
| §5 | Canary deployments | [inference.md](03-components/inference.md) | Phase 3 |
| §5 | Base-model fallback | [inference.md](03-components/inference.md), [registry.md rollback](03-components/registry.md) | Phase 3 |
| §6 P1 | Steel thread local MVP | [07-build-plan](07-build-plan.md) | ✅ |
| §6 P2 | Judge, MLflow, SafeTensors, Registry | as rows above | ✅ (lifecycle Phase 2′) |
| §6 P3 | K8s, KFP, cloud storage | [05 §4](05-infrastructure.md) | Phase 3 |
| §6 P4 | Multimodal ingestion/eval/fine-tune | Phase-4 sections of [02](02-data-contracts.md), [04 §5](04-model-adapters.md), component specs | Phase 4 |

Deviation from the SAS worth noting: the SAS names no fine-tune target; this doc set adds the **model-adapter layer** ([04](04-model-adapters.md)) as a first-class abstraction, and adds the **Smoke-test** component (not in the SAS component list) as the MVP's validation gate — it grows into the canary regression metric the SAS's §5 comparison requires.
