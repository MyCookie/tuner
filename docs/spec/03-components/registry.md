# Component Spec: Model Registry

**Purpose:** Versioned catalog of trained models linking weights ↔ exact Gold dataset ↔ MLflow experiment (SAS §3.2). The registry **is** the `tuner-registry` bucket — one manifest object per model version ([02-data-contracts.md §5.2](../02-data-contracts.md)); there is no database. This component spec covers the operations layered on top of those manifests.

## CLI

```
tuner registry list
tuner registry show <model_version>
tuner registry promote <model_version>
tuner registry rollback
```

Env: `TUNER_S3_*` (registry-ops credentials: read/write `tuner-registry`, read `tuner-artifacts`).

## Operations

- **`list`** — table of all versions: model_version, adapter, created_at, status, final_eval_loss.
- **`show`** — pretty-print one manifest; verify `weights_uri` objects exist; print the MLflow run URL.
- **`promote`** — transition `candidate → promoted`. Exactly **one** version per `adapter_name` may be `promoted` at a time: promoting version B demotes the currently promoted version A to `retired` in the same operation, recording `superseded_by: B` in A's manifest. Refuses (exit 2) if the manifest's `weights_uri` is missing objects.
- **`rollback`** — the inverse safety valve: re-promote the most recently `retired` version for the adapter and retire the current `promoted` one. This is the operational realization of the SAS §5 fallback requirement.

All transitions rewrite the manifest object with an appended `history` array entry: `{"at": ts, "from": "candidate", "to": "promoted", "by": "<operator or system>"}`.

## Concurrency note

Object stores have no transactions. Registry writes are last-writer-wins; the promote/rollback CLI is a human-in-the-loop tool, and simultaneous operators are out of scope until Phase 3, where promotion moves behind a single Kubeflow exit-handler step.

## Acceptance criteria

- Train two runs → `list` shows two candidates; `promote` the second → statuses are `retired`-none/`candidate`/`promoted` as expected; `promote` the first → second becomes `retired` with `superseded_by`; `rollback` restores it.
- `show` on a manifest with deleted weights exits 2.

## MVP scope

**Trainer-written manifests only** (`status: "candidate"`, [trainer.md](trainer.md) step 8) plus `tuner registry list` — one afternoon of work that makes runs discoverable. `show`/`promote`/`rollback` are Phase 2.

## Future phases

Phase 2: full CLI as above. Phase 3: promotion emits a K8s event consumed by the Inference Engine's deployment controller ([inference.md](inference.md)); manifests mirrored into MLflow Model Registry if the team adopts it (decision deferred — the bucket remains the source of truth either way).
