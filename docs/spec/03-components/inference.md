# Component Spec: Inference Engine

**Status: fully specified, entirely post-MVP (Phase 3).** Nothing in this document is built in the MVP slice; it exists so the MVP's registry manifests and artifact layout are provably sufficient to serve from later.

**Purpose:** High-throughput serving of fine-tuned models with safe rollout: canary traffic-splitting against the base model and automatic fallback on regression (SAS §5).

## Engine choice

- **Primary: vLLM** — PagedAttention/continuous batching, native OpenAI-compatible server, first-class **LoRA adapter serving** (`--enable-lora`), which matches Tuner's QLoRA output: serve the base model once and hot-load adapters, rather than merging weights per fine-tune.
- **Alternative: NVIDIA Triton** (with TensorRT-LLM backend) where the platform team standardizes on it; the deployment contract below is engine-agnostic.

## Deployment contract

- Serves only registry versions with `status: "promoted"` ([registry.md](registry.md)); resolves `weights_uri` from the manifest and downloads via `StorageClient` at pod start.
- Exposes the OpenAI-compatible API; the served model name is the Tuner `model_version` string.
- Stateless pods; K8s Deployment + HPA; readiness gate = model fully loaded and one warm-up generation completed.

## Release strategy (SAS §5)

1. **Base line:** a "base model" deployment (no adapter) is always running — the fail-over target.
2. **Canary:** promoting a new version deploys it alongside the current one; the gateway (Istio/Gateway API weighted routing) sends a configured small percentage (default 5 %) of traffic to the canary.
3. **Comparison:** a monitor job compares canary vs. incumbent over the canary window on: error rate, p95 latency, and the smoke-regression score (smoke-test Phase 2 metric, [smoke-test.md](smoke-test.md)) — thresholds in deployment config.
4. **Outcome:** pass ⇒ weights shift to 100 % and the incumbent scales down (registry already reflects promotion); fail ⇒ traffic returns to incumbent and the registry operator runs `rollback`. Hard failures (crash-looping, readiness loss) fall back to the **base model** deployment immediately.

## MVP scope

None. The MVP's model-consumption path is the [smoke-test](smoke-test.md). What the MVP **must** guarantee for this component's sake (and does, via the contracts): adapter dirs loadable by vLLM's LoRA loader (standard PEFT layout, SafeTensors), registry manifests carrying `weights_uri` + `base_model`, and one-promoted-version-per-adapter semantics.

## Future phases

Phase 3 delivers everything above alongside the K8s/Kubeflow migration ([05-infrastructure.md §4](../05-infrastructure.md)). Phase 4 revisits engine choice for multimodal serving.
