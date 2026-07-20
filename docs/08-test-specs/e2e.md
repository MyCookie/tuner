# Test Suite: End-to-End Steel Thread (`E2E`)

Spec under test: the whole MVP ([00 §2 success criteria](../00-product-scope.md), [06 §5](../06-testing.md)). File: `tests/e2e/test_steel_thread.py`, config `configs/pipeline.e2e.yaml` (tiny-test adapter, mock judge as compose sidecar, real MinIO + MLflow server). One pipeline execution, then one assertion function per case below (single `eftp run`, many asserts — budget ≤10 min GPU / nightly CPU).

| ID | Assertion over the completed run | Expected |
| :--- | :--- | :--- |
| E2E-E-001 | Exit status | `eftp run` returned 0 |
| E2E-E-002 | Lineage chain | Gold manifest `input.manifest_uri` → Silver manifest → Bronze manifest all resolve; every `counts.read` equals upstream `counts.written` |
| E2E-E-003 | Count conservation | Bronze written = fixture ingestable count; each tier `read = written + dropped`; drop reasons match `expected_counts.json` + marker-derived judge expectations |
| E2E-E-004 | Record conservation | Every Gold id appears in `index_map` splits or `index_map.dropped` — nothing silently lost between Gold and tensors |
| E2E-E-005 | Artifacts | Adapter/model dir present and PEFT/HF-loadable; zero `.bin`/pickle objects under the run prefix |
| E2E-E-006 | Registry | Exactly one manifest, `status: candidate`, all URIs resolve, visible in `eftp registry list` |
| E2E-E-007 | MLflow | Trainer run with params/metrics/tags per TRN-I-005; judge run per JDG-I-026; smoke transcript artifact attached |
| E2E-E-008 | Transcript | Sample count = config `smoke.num_prompts` (or all eval records if fewer); all sampled ids from eval split |
| E2E-E-009 | Idempotent stage re-run | Re-running `eftp tokenize` + `eftp train` for the same run ID leaves one coherent set of artifacts and an unchanged-or-replaced single registry manifest |
| E2E-E-010 | IAM spot-check | Cleaner creds denied writing `eftp-gold`; trainer creds denied reading `eftp-bronze` |

The E2E suite is the **release gate**: it runs before any tag ([09-git-workflow.md §5](../09-git-workflow.md)) and its run ID is recorded in the tag annotation. It is excluded from coverage measurement ([README §Coverage policy](README.md)).
