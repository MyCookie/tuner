# Test Suite: Smoke-test (`SMK`)

Spec under test: [smoke-test.md](../03-components/smoke-test.md). File: `tests/integration/test_smoke.py`. CPU-runnable via `tiny-test` artifacts from the shared trainer fixture; real-model generation is covered by the E2E/T15 pass.

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| SMK-I-001 | Happy path, `num_prompts: 4` | Exit 0; `transcript.json` at `tuner-artifacts/{run_id}/smoke/`; validates: run_id, model_version, generation block, exactly 4 samples |
| SMK-I-002 | Sample integrity | Every sample: `record_id` ∈ eval split of `index_map.json` (never train); `prompt_messages` = conversation minus final assistant turn; `reference` = that turn's text; `base_output`/`tuned_output` non-empty |
| SMK-I-003 | MLflow | Transcript attached as artifact `smoke/transcript.json` on the **trainer's** run (matched via `tuner.run_id` + `tuner.stage: trainer` tag pair — with the judge's run for the same run ID present, proving the pair-filter discriminates), not a new run |
| SMK-I-004 | Determinism | Two runs over the same artifacts → identical transcripts (greedy decoding) |
| SMK-I-005 | Missing adapter/model dir | Exit 2 with "trainer has not completed" message |
| SMK-I-006 | `num_prompts: 50` with only 3 eval records | Uses all 3, logs a warning, exit 0 |
| SMK-I-007 | Zero eval records (artifacts from TOK-I-027 scenario) | Exit 3 |
| SMK-I-008 | Re-run same run ID | `smoke/` prefix rebuilt, single transcript (idempotency) |
| SMK-I-009 | `method: qlora` with no CUDA device available (mocked) | Exit 2 naming the host-venv fallback doc — mirrors `TRN-I-012` |
| SMK-I-010 | Induced mid-run failure (mocked MLflow artifact attachment) | Exit 1 with a `smoke: ...` message, not a raw traceback — mirrors `TRN-I-009`'s own generic-exit-1 path |

`SMK-I-009`/`010` were added in review round 1 alongside the fixes they regression-test — see the Notes below.

## Notes — fixes from review round 1

- **`SMK-I-008` never asserted the "`smoke/` prefix rebuilt" half of its own suite row.** It only checked that a single `transcript.json` existed after two runs — true whether or not `delete_prefix` ran at all, since the stage only ever writes one object. Proven live: removing the `delete_prefix` call left the test green. Fixed with the same stale-marker technique `TRN-I-011` already established: plant an object under `{run_id}/smoke/` between the two `smoke()` calls that only a real `delete_prefix` removes, then assert it's gone.
- **`smoke()` had no top-level `try`/`except`.** Unlike `train()` (widened for exactly this reason in PR #11 review round 2), a mid-run failure — storage, MLflow, generation — escaped as a raw traceback instead of the `smoke: ...` message every other failure mode produces. Fixed by wrapping the whole run body in `except Exception as exc: return 1`; `SMK-I-010` regression-tests it.
- **No `method: qlora`-without-CUDA gate**, unlike the Trainer's own `TRN-I-012`. Fixed with the identical check and message (bitsandbytes 4-bit quantization needs CUDA), scoped to `qlora` only ([trainer.md](../03-components/trainer.md) footnote 1's same reasoning, now also in [smoke-test.md](../03-components/smoke-test.md) footnote 2); `SMK-I-009` regression-tests it.
- **`_generate` passed CPU tensors to a model that may be CUDA-resident** (a real `qlora` run always is). Behavior was correct on the box tested (transformers moves inputs internally with a warning) but not guaranteed — fixed to move `apply_chat_template`'s output onto `model.device` explicitly before `generate`.
- **`docs/spec/02-data-contracts.md` §5.3's `prompt_messages` wording was ambiguous** — read as `to_chat_messages(conversation[:-1])`, but the code (correctly) does `to_chat_messages(conversation)[:-1]`; for the two adapters that exist today these agree, but the wording didn't say so. Reworded to state the actual order and why it matters.
- **`SMK-I-003`'s "not a new run" check only compared runs already tagged with this run ID** — a `smoke()` bug that created an entirely untagged new run would be invisible to it. Fixed to also assert the MLflow store's total run count is unchanged before/after.
- **Minor:** [smoke-test.md](../03-components/smoke-test.md) footnote 1 claimed §5.3 documented "core logic 3–4" but §5.3 only ever covered step 4; step 3's own change (`quantized` follows `train.method`, not just the adapter's own config) was undocumented anywhere. Core logic 3–4 now state both changes directly, and a new footnote 2 (mirroring the Trainer's CUDA-scope footnote) documents the `qlora`-only CUDA requirement.
