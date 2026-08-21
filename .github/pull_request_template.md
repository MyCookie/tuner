<!-- docs/10-code-review.md §3. Everything here is a hint for the reviewer,
     never evidence — the reviewer re-derives all of it independently. -->

## Task

**TNN — <task title>** · `docs/07-build-plan.md`

<one paragraph: what this branch makes true that was not true before>

## Suite

Cases implemented, per the task's **Suite** line in `docs/08-test-specs/`:

- `XXX-U-001..00N` — <what they cover>
- `XXX-I-010..01N` — <what they cover>

Every case is docstring-tagged with its ID. None skipped, none invented.

## Gate

Output of `./scripts/gate.sh`:

| check | result |
| :--- | :--- |
| | |

## Spec decisions

<Any ambiguity found in the specs and how it was resolved, with the doc change
that records it. "None" if none — but a task that hit no ambiguity at all is
worth a second look. See the INF-I-005 footnote in 08-test-specs/infra.md for
the precedent.>

## Pre-existing tests touched

<`git diff origin/main...HEAD -- tests/` — list every modified pre-existing test
and justify it, or state "none". A test that looks wrong is a spec question,
not an edit.>

## Reviewer focus

<Where the implementer most wants scrutiny: the part that was hardest to get
right, the assumption least backed by a spec line, the code with the thinnest
test coverage.>

---

Refs: TNN
