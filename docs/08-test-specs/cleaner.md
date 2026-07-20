# Test Suite: Cleaner (`CLN`)

Spec under test: [cleaner.md](../03-components/cleaner.md). Files: `tests/unit/test_cleaner_rules.py`, `tests/integration/test_cleaner.py`. Coverage target: **100 %** of `rules.py`/`patterns.py`.

## Scrubbers & filters (unit, table-driven)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLN-U-001 | Email scrubber: plain, plus-tag, subdomain, uppercase (parametrized) | Each replaced with `[EMAIL]`; surrounding text intact |
| CLN-U-002 | Email negatives: `user@localhost` policy case, `@handle` twitter-style | Fixed expected outputs pinned in the table (decide and pin at implementation; the point is the behavior is *chosen*, not accidental) |
| CLN-U-003 | Phone scrubber: `+1 (555) 123-4567`, `555-123-4567`, `+44 20 7946 0958` | `[PHONE]` |
| CLN-U-004 | Phone negatives: `v2.10.3`, ISO dates, 5-digit zips, `port 8080` | Unchanged |
| CLN-U-005 | Unicode NFC: decomposed é vs composed é | Identical output; hash-equal for dedup |
| CLN-U-006 | Control chars stripped; `\n`/`\t` preserved; ≥3 blank lines → 2; trim | Per [cleaner.md step 4](../03-components/cleaner.md) |
| CLN-U-007 | Filters: 19-char convo (`too_short` @ min 20), convo over `max_chars` (`too_long`), empty-after-scrub turn (`empty_turn`) | Correct drop reason each |
| CLN-U-008 | `bad_structure`: no assistant turn, last turn user, double system (parametrized) | Drop `bad_structure` — cleaner drops, not crashes, since Bronze `raw` is untrusted |
| CLN-U-009 | Dedup: identical scrubbed conversations, different Bronze ids | First kept, second dropped `duplicate`; near-identical (one char diff) both kept |
| CLN-U-010 | Scrub-then-dedup ordering: two records identical only **after** PII scrub | Second is dropped (proves dedup runs on scrubbed text) |

## Structure mapping (unit)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLN-U-020 | CSV mapping with `system_column` present / null (parametrized) | 3-turn / 2-turn conversation; content values array-wrapped |
| CLN-U-021 | CSV row with empty `response_column` | Drop `unmappable` |
| CLN-U-022 | JSONL raw already contract-shaped | Adopted as-is (then scrubbed) |
| CLN-U-023 | JSONL flat shapes `prompt`/`response` and `question`/`answer` (+ optional `system`) | Mapped like CSV |
| CLN-U-024 | JSONL arbitrary object | Drop `unmappable` |

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
