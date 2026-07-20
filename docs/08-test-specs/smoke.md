# Test Suite: Smoke-test (`SMK`)

Spec under test: [smoke-test.md](../03-components/smoke-test.md). File: `tests/integration/test_smoke.py`. CPU-runnable via `tiny-test` artifacts from the shared trainer fixture; real-model generation is covered by the E2E/T15 pass.

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| SMK-I-001 | Happy path, `num_prompts: 4` | Exit 0; `transcript.json` at `eftp-artifacts/{run_id}/smoke/`; validates: run_id, model_version, generation block, exactly 4 samples |
| SMK-I-002 | Sample integrity | Every sample: `record_id` ∈ eval split of `index_map.json` (never train); `prompt_messages` = conversation minus final assistant turn; `reference` = that turn's text; `base_output`/`tuned_output` non-empty |
| SMK-I-003 | MLflow | Transcript attached as artifact `smoke/transcript.json` on the **trainer's** run (matched via `eftp.run_id` tag), not a new run |
| SMK-I-004 | Determinism | Two runs over the same artifacts → identical transcripts (greedy decoding) |
| SMK-I-005 | Missing adapter/model dir | Exit 2 with "trainer has not completed" message |
| SMK-I-006 | `num_prompts: 50` with only 3 eval records | Uses all 3, logs a warning, exit 0 |
| SMK-I-007 | Zero eval records (artifacts from TOK-I-027 scenario) | Exit 3 |
| SMK-I-008 | Re-run same run ID | `smoke/` prefix rebuilt, single transcript (idempotency) |
