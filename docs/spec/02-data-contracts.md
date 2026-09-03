# Tuner Data Contracts

Normative definition of every record schema, manifest, and storage layout in the pipeline. All other documents reference this one and never restate schemas. The executable form of these contracts is `src/tuner/core/schemas.py` (pydantic v2); if code and this document disagree, this document wins and the code is a bug.

General rules:

- All records are JSONL (one JSON object per line, UTF-8, `\n` line endings).
- All timestamps are ISO-8601 UTC with `Z` suffix (`2026-07-20T14:22:01Z`).
- All content hashes are `"sha256:<64 lowercase hex>"`, computed over **canonical JSON**: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`. This one definition (implemented once in `tuner/core/ids.py` as `canonical_hash()`) is used everywhere a hash of a JSON object is taken — Bronze `content_hash`, the Cleaner's dedup key, manifest `records_hash` inputs.
- **Validation is fail-fast:** every stage validates each input record against the upstream schema on read; the first invalid record aborts the stage with exit code 2 and the offending record ID and error in the log.
- Fields marked *(Phase 4)* are reserved now — present in the schema, valid to populate — but MVP producers write only the MVP subset.

---

## 1. Bronze — raw envelope

Bucket `tuner-bronze`, objects `{run_id}/records-{NNNNN}.jsonl` + `{run_id}/manifest.json`. Producer: Ingestor. The raw source data is preserved byte-faithfully inside a metadata envelope; nothing is cleaned or normalized at this tier.

```json
{
  "id": "9f1c2b3a-...-uuid4",
  "run_id": "run-20260720-142201-a3f9c2",
  "source": {
    "uri": "file:///data/support_dialogs.csv",
    "type": "csv",
    "locator": "row:42",
    "ingested_at": "2026-07-20T14:22:05Z",
    "ingestor_version": "0.1.0"
  },
  "content_hash": "sha256:ab12...",
  "raw": {"question": "How do I reset my password?", "answer": "Go to Settings..."}
}
```

| Field | Type | Rules |
| :--- | :--- | :--- |
| `id` | string | UUIDv4, assigned here, **immutable through all tiers** |
| `run_id` | string | run-ID format from [01-architecture.md §4.2](01-architecture.md) |
| `source.uri` | string | source location as given in config |
| `source.type` | string | `csv` \| `jsonl` (MVP) \| `sql` \| `pdf` \| `api` (later) |
| `source.locator` | string | position within the source: `row:{n}` (csv), `line:{n}` (jsonl), `page:{n}` (pdf), query offset (sql) |
| `source.ingested_at` | timestamp | envelope creation time |
| `source.ingestor_version` | string | tuner package version |
| `content_hash` | string | `canonical_hash(raw)` per the canonical-JSON rule above |
| `raw` | object | source record exactly as read; keys are source-defined |

---

## 2. Silver & Gold — the Multimodal Contract

Buckets `tuner-silver` and `tuner-gold`, same object layout as Bronze, **same schema for both tiers** — the difference is data state: Silver has `evaluation: null`; Gold has `evaluation` populated and only contains records at or above the judge threshold. Producers: Cleaner (Silver), Judge (Gold).

```json
{
  "id": "9f1c2b3a-...-same-uuid-as-bronze",
  "run_id": "run-20260720-142201-a3f9c2",
  "lineage": {
    "bronze_content_hash": "sha256:ab12...",
    "cleaner_version": "0.1.0"
  },
  "conversation": [
    {
      "role": "system",
      "content": [{"type": "text", "value": "You are a helpful support agent."}]
    },
    {
      "role": "user",
      "content": [
        {"type": "text", "value": "How do I reset my password?"},
        {"type": "image", "value": "s3://tuner-assets/run-.../img-001.jpg"}
      ]
    },
    {
      "role": "assistant",
      "content": [{"type": "text", "value": "Go to Settings..."}]
    }
  ],
  "evaluation": {
    "score": 0.8,
    "judge_model": "qwen2.5-32b-instruct",
    "reasoning": "Accurate, complete answer; minor verbosity.",
    "evaluated_at": "2026-07-20T14:31:10Z"
  }
}
```

| Field | Type | Rules |
| :--- | :--- | :--- |
| `id` | string | carried over from Bronze unchanged |
| `run_id` | string | as Bronze |
| `lineage.bronze_content_hash` | string | `content_hash` of the originating Bronze envelope |
| `lineage.cleaner_version` | string | tuner version that produced the Silver record |
| `conversation` | array, min 2 items | ordered turns; see turn rules below |
| `conversation[].role` | string | `system` \| `user` \| `assistant`. At most one `system` turn, and only first. Must contain ≥1 `user` and ≥1 `assistant` turn; last turn must be `assistant` |
| `conversation[].content` | array, min 1 item | **always an array**, even for a single text part (SAS §2.2) |
| `content[].type` | string | `text` (MVP) \| `image` \| `audio` *(Phase 4)* |
| `content[].value` | string | for `text`: the text, non-empty after trim; for `image`/`audio`: `s3://tuner-assets/...` URI *(Phase 4)* |
| `evaluation` | object or null | `null` in Silver; required non-null in Gold |
| `evaluation.score` | number | normalized to **[0.0, 1.0]** |
| `evaluation.judge_model` | string | model name that produced the score |
| `evaluation.reasoning` | string | judge's stated rationale |
| `evaluation.evaluated_at` | timestamp | scoring time |

---

## 3. Tier manifest

Written by every tier producer at `{run_id}/manifest.json` in its output bucket, **after** all record shards are written (the manifest is the commit marker: downstream stages read the manifest first and treat its absence as "upstream incomplete", exit 2).

```json
{
  "tier": "silver",
  "run_id": "run-20260720-142201-a3f9c2",
  "created_at": "2026-07-20T14:25:33Z",
  "producer": {"stage": "cleaner", "version": "0.1.0"},
  "input": {"tier": "bronze", "manifest_uri": "s3://tuner-bronze/run-20260720-142201-a3f9c2/manifest.json"},
  "files": ["records-00000.jsonl"],
  "records_hash": "sha256:cd34...",
  "counts": {"read": 100, "written": 91, "dropped": 9},
  "drops": [
    {"reason": "too_short", "count": 5},
    {"reason": "duplicate", "count": 3},
    {"reason": "unmappable", "count": 1}
  ]
}
```

Rules: `counts.read = counts.written + counts.dropped`; `drops[].reason` values are fixed per stage in each component spec; `records_hash` is sha256 over the concatenated bytes of `files` in listed order; `input` is `null` only for Bronze (whose input is external). The chain of `input.manifest_uri` links gives full Bronze→Gold auditability (SAS §2).

---

## 4. Tokenized artifact

Bucket `tuner-artifacts`, prefix `{run_id}/tokens/`. Producer: Tokenizer.

```
{run_id}/tokens/
├── train.safetensors     # input_ids, attention_mask, labels — int64 [n_train, seq_len]
├── eval.safetensors      # same keys                         — int64 [n_eval, seq_len]
└── index_map.json
```

- Tensors are stored as **SafeTensors** (SAS §4.2 — no pickle anywhere in the pipeline).
- `labels` equals `input_ids` with non-assistant tokens and padding set to `-100` (loss masked); details in [03-components/tokenizer.md](03-components/tokenizer.md).

`index_map.json` — traceability from tensor row to Gold record (SAS §3.1):

```json
{
  "run_id": "run-20260720-142201-a3f9c2",
  "adapter": "gemma-e4b",
  "tokenizer_id": "google/gemma-4-E4B-it",
  "max_seq_len": 4096,
  "gold_manifest_uri": "s3://tuner-gold/run-20260720-142201-a3f9c2/manifest.json",
  "splits": {
    "train": [{"row": 0, "record_id": "9f1c2b3a-..."}, ...],
    "eval":  [{"row": 0, "record_id": "77aa41d0-..."}, ...]
  },
  "dropped": [{"record_id": "c0ffee12-...", "reason": "over_max_len"}]
}
```

Allowed `dropped[].reason` values: `over_max_len`, `unsupported_modality`, `masking_mismatch` ([tokenizer.md](03-components/tokenizer.md)).

---

## 5. Training artifacts & registry

### 5.1 Artifact layout (bucket `tuner-artifacts`)

```
{run_id}/
├── tokens/                      # §4
├── adapter/                     # PEFT adapter dir: adapter_model.safetensors,
│   │                            #   adapter_config.json, tokenizer files
│   └── ...
└── smoke/
    └── transcript.json          # smoke-test output (03-components/smoke-test.md)
```

For `train.method: full`, `adapter/` is replaced by `model/` containing full SafeTensors weights; same manifest fields apply.

### 5.2 Registry manifest (bucket `tuner-registry`)

One object per trained model version: `{model_version}/manifest.json`, written by the Trainer on success. This is the SAS §3.2 registry link between model, data, and experiment.

```json
{
  "model_version": "gemma-e4b-run-20260720-142201-a3f9c2",
  "run_id": "run-20260720-142201-a3f9c2",
  "adapter_name": "gemma-e4b",
  "base_model": "google/gemma-4-E4B-it",
  "method": "qlora",
  "created_at": "2026-07-20T16:05:00Z",
  "gold_manifest_uri": "s3://tuner-gold/run-20260720-142201-a3f9c2/manifest.json",
  "index_map_uri": "s3://tuner-artifacts/run-20260720-142201-a3f9c2/tokens/index_map.json",
  "weights_uri": "s3://tuner-artifacts/run-20260720-142201-a3f9c2/adapter/",
  "mlflow_run_id": "abcdef123456",
  "hyperparameters": {"learning_rate": 2e-4, "epochs": 3, "lora_r": 16},
  "eval": {"final_train_loss": 1.23, "final_eval_loss": 1.31},
  "status": "candidate"
}
```

`status` lifecycle: `candidate` → `promoted` → `retired` (promote/rollback operations: [03-components/registry.md](03-components/registry.md); MVP only ever writes `candidate`). The Inference Engine ([03-components/inference.md](03-components/inference.md)) serves only `promoted` versions.

### 5.3 Smoke transcript (bucket `tuner-artifacts`)

One object per run: `{run_id}/smoke/transcript.json`, written by the Smoke-test on success ([03-components/smoke-test.md](03-components/smoke-test.md) core logic 5) and attached as an MLflow artifact to the Trainer's run ([01-architecture.md §7](01-architecture.md)).

```json
{
  "run_id": "run-20260720-142201-a3f9c2",
  "model_version": "gemma-e4b-run-20260720-142201-a3f9c2",
  "generation": {"max_new_tokens": 256, "strategy": "greedy"},
  "samples": [
    {
      "record_id": "9f1c2b3a-...",
      "prompt_messages": [{"role": "user", "content": "..."}],
      "reference": "...",
      "base_output": "...",
      "tuned_output": "..."
    }
  ]
}
```

`prompt_messages` is `to_chat_messages(conversation)`'s own output ([04-model-adapters.md §1](04-model-adapters.md)) **minus its final message** — not `to_chat_messages` applied to the conversation already truncated, which need not produce the same result for every adapter. `reference` is that same call's final message's `content`. Both come from one `to_chat_messages` call, so `prompt_messages` is exactly a prefix of what the model is actually prompted with, not a re-derivation from the raw conversation.

**`train.method: full` clarification (T12, mirrors §5.1's "`adapter/` is replaced by `model/`" note):** the Smoke-test resolves its input weights directory the same way the Trainer decided where to write them — `{run_id}/adapter/` for `method: qlora`, `{run_id}/model/` for `method: full` — since [03-components/smoke-test.md](03-components/smoke-test.md)'s own Input section predates that distinction being spelled out in prose (the `08-test-specs/smoke.md` suite table's `SMK-I-005` row already says "adapter/model dir"). For `method: full` there is no PEFT adapter to attach: the "tuned" model for step 4 is the full fine-tuned weights loaded directly, in place of attaching a PEFT adapter to the base model.

---

## 6. Bucket & prefix summary

| Bucket | Layout | Written by |
| :--- | :--- | :--- |
| `tuner-bronze` | `{run_id}/records-*.jsonl`, `{run_id}/manifest.json` | Ingestor |
| `tuner-silver` | same | Cleaner |
| `tuner-gold` | same | Judge |
| `tuner-artifacts` | `{run_id}/tokens/`, `{run_id}/adapter/`, `{run_id}/smoke/` | Tokenizer, Trainer, Smoke-test |
| `tuner-registry` | `{model_version}/manifest.json` | Trainer, Registry ops |
| `tuner-assets` *(Phase 4)* | `{run_id}/media/{asset_id}.{ext}` | Ingestor |

**MVP scope:** all of §1–§5 except: `content[].type` of `image`/`audio` is never produced, `tuner-assets` does not exist, registry `status` transitions beyond `candidate` are not implemented, and `train.method: full` is specified but only `qlora` is exercised.
