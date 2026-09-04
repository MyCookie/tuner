# Inference

**This component does not exist yet.** There is no `tuner infer` command, no
serving code, no `src/tuner/inference/` package — nothing in this repository
implements it. Everything on this page describes *design intent* from
[spec/03-components/inference.md](../spec/03-components/inference.md), not
running behavior. The spec document itself says so in its own opening line:
"fully specified, entirely post-MVP (Phase 3)." Nothing below was run,
because there is nothing to run; treat every claim on this page as
spec-verified, not code- or run-verified, unlike every other page in this
guide.

## Why it's documented anyway

Per [Product scope §3](../spec/00-product-scope.md), serving is explicitly
out of MVP scope. The spec exists regardless so that the MVP's own design
choices — the registry manifest shape, the SafeTensors/PEFT artifact
layout — are provably sufficient to serve from later, without the MVP having
had to guess at a serving contract it never implements. If you're operating
Tuner today, the pipeline's actual endpoint is the
[Smoke-test](smoke-test.md) transcript, not a served model — see
[Architecture overview](../01-architecture-overview.md) for the full stage
list as it actually exists.

## What it's meant to do, per the spec

A high-throughput serving layer with safe rollout: canary traffic-splitting
between a newly-promoted model version and the current one, with automatic
fallback to a permanently-running base-model deployment if anything goes
wrong.

- **Engine:** vLLM primarily, chosen specifically because it serves LoRA
  adapters natively (`--enable-lora`) — matching Tuner's QLoRA output
  directly, so a fine-tune is served by hot-loading an adapter onto an
  already-running base model rather than merging and redeploying full
  weights per fine-tune. NVIDIA Triton (TensorRT-LLM backend) is named as an
  alternative if a platform team standardizes on it; the deployment contract
  is meant to be engine-agnostic either way.
- **What it would serve:** only registry versions with `status: "promoted"`
  ([registry.md](registry.md)) — resolving `weights_uri` from that version's
  manifest and downloading it via `StorageClient` at pod start. Since the
  MVP's registry only ever writes `status: "candidate"` (there is no
  `promote` operation implemented — see [registry.md](registry.md)), there
  is currently no version this component's own deployment contract could
  even select.
- **Release strategy:** a base-model deployment always runs as the
  fail-over target; promoting a new version deploys it alongside the
  current one at a small traffic percentage (5% by default); a monitor job
  compares error rate, p95 latency, and a smoke-regression score (a
  Phase 2+ extension of the Smoke-test's transcript, not implemented in the
  MVP's Smoke-test either — see [smoke-test.md](smoke-test.md)) over a
  canary window; passing shifts traffic to 100%, failing reverts and the
  operator runs `registry rollback` (also unimplemented today).

## What the MVP guarantees for this component's future sake

The spec is explicit that the MVP doesn't build any of the above, but its
own contracts are shaped so this component is buildable without revisiting
earlier stages: for `train.method: full`, the Trainer's saved weights were
verified (while writing [trainer.md](trainer.md)) to load correctly via
plain `AutoModelForCausalLM.from_pretrained` — a standard `transformers`
loader, the same family a serving engine would use. The `method: qlora`
adapter path (standard PEFT layout, which is what vLLM's `--enable-lora`
loader specifically expects) is GPU-only and wasn't exercised by anything run
while writing this guide — see the Trainer/Smoke-test pages' own GPU caveats.
Registry manifests already carry `weights_uri` and `base_model`, and the
one-promoted-version-per-adapter semantics are specified (if not
implemented) in [registry.md](registry.md). None of that is a substitute for
actually building this component — it just means the MVP didn't back itself
into a corner that would make building it harder later.

## If you're looking for how to actually serve a trained model today

You can't, through Tuner itself. What exists is: a registry manifest naming
where the trained weights live
([registry.md](registry.md)/[Data contracts §5.2](../spec/02-data-contracts.md)),
and a Smoke-test transcript showing base-vs-tuned outputs on held-out
prompts ([smoke-test.md](smoke-test.md)). Loading those weights into your
own serving stack (vLLM, Triton, or a plain `transformers` `generate()` call)
is possible today using nothing but the manifest's `weights_uri` and
`base_model` fields and a standard PEFT/`transformers` loader — the same way
the Smoke-test itself loads them — but that's you building it, not Tuner
serving it.
