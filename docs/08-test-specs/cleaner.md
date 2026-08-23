# Test Suite: Cleaner (`CLN`)

Spec under test: [cleaner.md](../03-components/cleaner.md). Files: `tests/unit/test_cleaner_rules.py`, `tests/integration/test_cleaner.py`. Coverage target: **100 %** of `rules.py`/`patterns.py`.

## Scrubbers & filters (unit, table-driven)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLN-U-001 | Email scrubber: plain, plus-tag, subdomain, uppercase (parametrized) | Each replaced with `[EMAIL]`; surrounding text intact |
| CLN-U-002 | Email negatives: `user@localhost` policy case, `@handle` twitter-style | Fixed expected outputs pinned in the table (decide and pin at implementation; the point is the behavior is *chosen*, not accidental) |
| CLN-U-003 | Phone scrubber: `+1 (555) 123-4567`, `(555) 123-4567`, `555-123-4567`, `+44 20 7946 0958` | `[PHONE]` |
| CLN-U-004 | Phone negatives: `v2.10.3`, ISO dates, 5-digit zips, `port 8080` | Unchanged |
| CLN-U-005 | Unicode NFC: decomposed é vs composed é | Identical output; hash-equal for dedup |
| CLN-U-006 | Control chars stripped; `\n`/`\t` preserved; ≥3 blank lines → 2; trim | Per [cleaner.md step 4](../03-components/cleaner.md) |
| CLN-U-007 | Filters: 19-char convo (`too_short` @ min 20), convo over `max_chars` (`too_long`), empty-after-scrub turn (`empty_turn`, incl. one blank text part among non-blank ones in the same turn¹) | Correct drop reason each |
| CLN-U-008 | `bad_structure`: no assistant turn, last turn user, double system, system not first, a single turn, and every other way a contract-shaped `raw.conversation` can be malformed while still being a JSON value — non-dict turn, an extra turn key, non-array content, non-dict content part, a part missing `value`, an invalid `type`, non-string `value`, an extra content-part key, an invalid role (parametrized)² | Drop `bad_structure` — cleaner drops, not crashes, since Bronze `raw` is untrusted |
| CLN-U-009 | Dedup: identical scrubbed conversations, different Bronze ids | First kept, second dropped `duplicate`; near-identical (one char diff) both kept |
| CLN-U-010 | Scrub-then-dedup ordering: two records identical only **after** PII scrub | Second is dropped (proves dedup runs on scrubbed text) |
| CLN-U-030 | **Property (hypothesis):** arbitrary unicode text through the full scrub chain | Idempotent (`scrub(scrub(x)) == scrub(x)`); output never matches the PII patterns; output length ≤ input length + placeholder slack |

¹ **Design decision (T07 review round 1):** `empty_turn` is `any` blank text part, not `all`. The Multimodal Contract requires *every* `ContentPart`'s text be non-empty after trim ([02 §2](../02-data-contracts.md)), so a turn with one blank part alongside a non-blank one is exactly as unwritable as a turn that's blank all over — keeping it would produce Silver output that fails its own schema. An earlier `all`-based version let this slip through uncaught until review round 1 reproduced it end to end.

² **Design decision (T07 review round 1):** `_is_well_structured` ([rules.py](../../src/tuner/cleaner/rules.py)) re-derives the Multimodal Contract's per-part shape rules (valid `type` enum, `value` is a string, no keys beyond `type`/`value`) rather than delegating to `ContentPart`'s own pydantic validator, because that validator's non-empty-after-trim check would fire on the *pre-scrub* value — collapsing the deliberate `unmappable` (empty at the source) vs. `empty_turn` (blank only after scrub) distinction CLN-U-007 requires. The trade-off is that `_is_well_structured` must independently track every shape rule the contract enforces, checked defensively since a JSONL `raw.conversation` is untrusted, attacker- or typo-controlled input — round 1 found it missing three of them (type enum, value type, extra keys).

## Structure mapping (unit)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLN-U-020 | CSV mapping with `system_column` present / null (parametrized) | 3-turn / 2-turn conversation; content values array-wrapped |
| CLN-U-021 | CSV row with empty `response_column`, or a CSV source with no mapping at all (parametrized) | Drop `unmappable` |
| CLN-U-022 | JSONL raw already contract-shaped, including a non-text (image) content part | Adopted as-is (then scrubbed); a non-text part isn't judged "empty" — there's no text to be empty |
| CLN-U-023 | JSONL flat shapes `prompt`/`response` and `question`/`answer` (+ optional `system`) | Mapped like CSV |
| CLN-U-024 | JSONL arbitrary object, a recognized flat shape with an empty value, or a Bronze `source.type` the Cleaner doesn't map (parametrized) | Drop `unmappable` |

## Pipeline behavior (integration)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLN-I-030 | Full fixture Bronze → Silver | Per-reason drop counts equal `expected_counts.json`; `read = written + dropped` |
| CLN-I-031 | Output records | All valid Silver: `evaluation` null, `lineage.bronze_content_hash` matches source envelope, ids ⊂ Bronze ids |
| CLN-I-032 | Run twice | `records-*.jsonl` byte-identical (determinism) |
| CLN-I-033 | PII sweep: regex scan of all output text | Zero email/phone matches |
| CLN-I-034 | Seeded invalid Bronze record (schema-breaking) | Exit 2 naming record id — invalid input is an abort, not a drop |
| CLN-I-035 | Missing Bronze manifest | Exit 2 (upstream incomplete) |
| CLN-I-036 | All records dropped (seed 3 too-short records only) | Exit 3, no manifest |

## Notes — decisions not tied to a single case

- **Every Bronze record is read and schema-validated before Silver's own prefix is deleted** ([cli.py](../../src/tuner/cleaner/cli.py)), not interleaved with mapping/scrubbing/writing. [cleaner.md](../03-components/cleaner.md) core logic already orders it this way ("1. Read... validate... 2. Delete `tuner-silver/{run_id}/`"); the point of calling it out is that CLN-I-034's invalid-Bronze-record abort happens *before* any destructive action against a prior run's Silver output, not mid-write into a state a re-run then has to recover from.
- **`scrub()` runs PII placeholder substitution after control-char stripping**, not before or interleaved — load-bearing, not arbitrary: a control char sitting between two non-matching fragments could otherwise knit them into something PII-shaped only after stripping, and this ordering guarantees the same pass still catches it (confirmed in review round 1 with `"call 555-123\x01-4567 now"`, which scrubs to `"call [PHONE] now"` only in this order).
- **`clean.pii` is validated at config-load time** ([core/config.py](../../src/tuner/core/config.py) `CleanConfig.pii: list[Literal["email", "phone"]]`), not by `tuner.cleaner.rules.scrub()` at scrub time. An unknown scrubber name (`clean.pii: [ssn]`) fails with `ConfigError` (exit 2) before any Bronze record is touched, per [01-architecture.md §4.4](../01-architecture.md)'s exit-code taxonomy — round 1 found it reaching `scrub()`'s lookup dict instead and raising an uncaught `KeyError` (exit 1) mid-run.
