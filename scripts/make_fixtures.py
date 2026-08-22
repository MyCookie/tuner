"""Generate the committed fixture dataset (docs/06-testing.md §2).

Test tooling, not a pipeline stage: writes deterministic, synthetic support-
desk chatter used by Ingestor/Cleaner unit+integration tests from T06 on.
Every planted defect is tracked at the point it's generated — `expected_counts.json`
is derived from the same counters that decided what to emit, so its counts
match "by construction" (07-build-plan.md T05 Accept) rather than being
recomputed by a second implementation of cleaner logic that could drift from
the real one built in T07.

Re-run to regenerate; output is byte-deterministic (no randomness, no
wall-clock) and is committed under fixtures/ per 09-git-workflow.md §6.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

# Mirrors the CleanConfig defaults in src/tuner/core/config.py (min_chars=20,
# max_chars=32000) — the fixture's "under-length"/"over-length" rows are
# planted relative to these, not to whatever a future config might set.
MIN_CHARS = 20
MAX_CHARS = 32000

SYSTEM_PROMPT = "You are a helpful support agent."

# 16 support-desk scenarios x 5 product surfaces = 80 unique clean rows.
_TOPICS = [
    (
        "How do I reset my password for {product}?",
        "Go to Settings, select Security, then choose Reset Password. "
        "You'll receive a confirmation email within a few minutes.",
    ),
    (
        "Why was I charged twice for {product} this month?",
        "Duplicate charges are usually a temporary authorization hold that clears "
        "within 3-5 business days. If it persists, contact billing support with "
        "your invoice number.",
    ),
    (
        "Can I change the email address linked to {product}?",
        "Yes, open Account Settings, select Profile, and update the email field. "
        "You'll need to confirm the change from the new address.",
    ),
    (
        "How long does shipping take for orders placed through {product}?",
        "Standard shipping takes 5-7 business days. Expedited shipping is "
        "available at checkout and arrives in 2-3 business days.",
    ),
    (
        "Is it possible to cancel my subscription to {product} at any time?",
        "You can cancel anytime from the Billing tab. Your access continues "
        "until the end of the current billing period, and no further charges "
        "will be made.",
    ),
    (
        "What should I do if {product} won't load on my device?",
        "Try clearing your browser cache and restarting the app. If the issue "
        "continues, check our status page for any ongoing incidents.",
    ),
    (
        "How do I export my data from {product}?",
        "Navigate to Settings, then Data Export, and choose CSV or JSON format. "
        "The export link is emailed to you once it's ready.",
    ),
    (
        "Can I add additional team members to {product}?",
        "Yes, go to the Team page and select Invite Member. Each new seat is "
        "billed on a pro-rated basis for the current cycle.",
    ),
    (
        "Why did I receive a notification that {product} storage is full?",
        "You've reached your plan's storage limit. Free up space by deleting "
        "unused files, or upgrade to a plan with more storage.",
    ),
    (
        "How do I enable two-factor authentication on {product}?",
        "Open Security Settings and toggle on Two-Factor Authentication. You "
        "can use an authenticator app or receive codes by SMS.",
    ),
    (
        "Does {product} support integration with third-party calendars?",
        "Yes, we support Google Calendar and Outlook. Connect them from the "
        "Integrations tab in Settings.",
    ),
    (
        "What happens to my files if I downgrade {product}?",
        "Files beyond your new plan's limit are archived, not deleted. You can "
        "access them again by upgrading or exporting them beforehand.",
    ),
    (
        "How can I update the payment method on file for {product}?",
        "Go to Billing, select Payment Methods, and add a new card. You can "
        "set it as the default before removing the old one.",
    ),
    (
        "Why am I not receiving email notifications from {product}?",
        "Check your notification preferences under Settings, and confirm our "
        "sending domain isn't blocked by your email provider's spam filter.",
    ),
    (
        "Can I restore a file I accidentally deleted from {product}?",
        "Deleted files are kept in the Trash for 30 days. Open Trash, select "
        "the file, and choose Restore.",
    ),
    (
        "How do I transfer ownership of a project in {product}?",
        "Open the project, go to Settings, then Ownership, and select the new "
        "owner. They will receive an email to confirm the transfer.",
    ),
]
_PRODUCTS = [
    "your account",
    "the mobile app",
    "your billing portal",
    "the desktop client",
    "your team workspace",
]


def _clean_rows() -> list[dict[str, str]]:
    """80 unique, mappable, in-bounds rows — no planted defect."""
    rows = []
    for i, (question_tmpl, answer) in enumerate(_TOPICS):
        for product in _PRODUCTS:
            rows.append(
                {
                    "question": question_tmpl.format(product=product),
                    "answer": answer,
                    # Populated on every 4th row so both branches of the
                    # CSV system_column mapping appear in the committed
                    # fixture, even though the default pipeline.yaml config
                    # maps system_column: null (exercised instead by
                    # CLN-U-020's own dedicated temp CSV).
                    "system": SYSTEM_PROMPT if i % 4 == 0 else "",
                }
            )
    assert len(rows) == 80
    for row in rows:
        assert MIN_CHARS <= len(row["question"]) + len(row["answer"]) <= MAX_CHARS
    return rows


def _duplicate_rows(clean: list[dict[str, str]]) -> list[dict[str, str]]:
    """3 exact duplicates of already-clean rows (dedup drops the 2nd occurrence)."""
    return [dict(clean[i]) for i in (0, 27, 55)]


def _short_rows() -> list[dict[str, str]]:
    """5 rows whose combined question+answer text is under MIN_CHARS."""
    rows = [
        {"question": "Help?", "answer": "Sure.", "system": ""},
        {"question": "Hi", "answer": "Hello", "system": ""},
        {"question": "Thanks", "answer": "np", "system": ""},
        {"question": "???", "answer": "!!!", "system": ""},
        {"question": "ok?", "answer": "yes", "system": ""},
    ]
    assert len(rows) == 5
    for row in rows:
        assert len(row["question"]) + len(row["answer"]) < MIN_CHARS
    return rows


def _long_rows() -> list[dict[str, str]]:
    """2 rows whose combined text exceeds MAX_CHARS."""
    padding = "This support article covers the requested topic in extensive detail. " * 500
    rows = [
        {
            "question": "Can you send me the full troubleshooting guide for network errors?",
            "answer": padding,
            "system": "",
        },
        {
            "question": "What is the complete history of changes to my account settings?",
            "answer": padding + " Additional appendix follows below.",
            "system": "",
        },
    ]
    assert len(rows) == 2
    for row in rows:
        assert len(row["question"]) + len(row["answer"]) > MAX_CHARS
    return rows


def _pii_rows() -> list[dict[str, str]]:
    """4 rows carrying PII that the Cleaner must scrub (not drop)."""
    rows = [
        {
            "question": "How do I contact support about a billing issue?",
            "answer": "You can reach us directly at alice@example.com during business hours.",
            "system": "",
        },
        {
            "question": "Is there a direct line I can call about my order?",
            "answer": "Yes, call +1 (555) 123-4567 and an agent will assist you.",
            "system": "",
        },
        {
            "question": "Can I email the enterprise account team directly?",
            "answer": (
                "Sure — reach the enterprise team at jane.doe+billing@corp.example.com "
                "or by phone at +44 20 7946 0958."
            ),
            "system": "",
        },
        {
            "question": "What's the best way to reach the security team urgently?",
            "answer": "Please email SECURITY@EXAMPLE.COM immediately; it's monitored 24/7.",
            "system": "",
        },
    ]
    assert len(rows) == 4
    for row in rows:
        assert MIN_CHARS <= len(row["question"]) + len(row["answer"]) <= MAX_CHARS
    return rows


def _empty_answer_rows() -> list[dict[str, str]]:
    """3 rows with an empty response_column -> unmappable."""
    questions = [
        "Why isn't my refund showing up yet?",
        "Can you check on the status of my open ticket?",
        "Did my last message go through to support?",
    ]
    rows = [{"question": q, "answer": "", "system": ""} for q in questions]
    assert len(rows) == 3
    return rows


def _blank_question_rows() -> list[dict[str, str]]:
    """3 rows with an empty prompt_column -> unmappable."""
    answers = [
        "Thanks for reaching out, we'll follow up shortly.",
        "This has been escalated to the appropriate team.",
        "Your request has been logged under a new ticket.",
    ]
    rows = [{"question": "", "answer": a, "system": ""} for a in answers]
    assert len(rows) == 3
    return rows


def _write_support_dialogs_csv() -> dict:
    clean = _clean_rows()
    duplicates = _duplicate_rows(clean)
    short = _short_rows()
    long_ = _long_rows()
    pii = _pii_rows()
    empty_answer = _empty_answer_rows()
    blank_question = _blank_question_rows()

    all_rows = clean + duplicates + short + long_ + pii + empty_answer + blank_question
    assert len(all_rows) == 100

    path = FIXTURES_DIR / "support_dialogs.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["question", "answer", "system"])
        writer.writeheader()
        writer.writerows(all_rows)

    written = len(clean) + len(pii)
    dropped = len(duplicates) + len(short) + len(long_) + len(empty_answer) + len(blank_question)
    return {
        "read": len(all_rows),
        "written": written,
        "dropped": dropped,
        "drops": {
            "duplicate": len(duplicates),
            "too_short": len(short),
            "too_long": len(long_),
            "unmappable": len(empty_answer) + len(blank_question),
        },
    }


def _contract_shaped_lines() -> list[dict]:
    """10 raw objects already in Multimodal Contract `conversation` shape."""
    scenarios = [
        (
            "How do I merge two duplicate customer records?",
            "Open the customer profile, select Merge, and confirm the primary record.",
        ),
        (
            "What's the retention policy for closed tickets?",
            "Closed tickets are retained for 2 years, then archived to cold storage.",
        ),
        (
            "Can I bulk-export all my open tickets?",
            "Yes, from the Tickets view select all, then choose Export as CSV.",
        ),
        (
            "How do I set an out-of-office auto-reply?",
            "Go to Settings, Automation, and enable the Out of Office responder.",
        ),
        (
            "Why does the dashboard show stale numbers?",
            "Dashboards refresh every 15 minutes; use the manual refresh button for live data.",
        ),
        (
            "Can a ticket be reassigned after it's closed?",
            "Reopen the ticket first, then use the Assignee field to reassign it.",
        ),
        (
            "How do I mute notifications for one project?",
            "Open the project, select Notifications, and toggle Mute for this project.",
        ),
        (
            "What's included in the starter support plan?",
            "The starter plan includes email support and a 48-hour response time.",
        ),
        (
            "How do I merge two support inboxes?",
            "Contact your workspace admin; inbox merges are performed from the Admin console.",
        ),
        (
            "Can I see who last edited a saved reply?",
            "Yes, saved replies show an edit history with the editor's name and timestamp.",
        ),
    ]
    lines = []
    for i, (question, answer) in enumerate(scenarios):
        conversation = []
        if i < 3:
            conversation.append(
                {"role": "system", "content": [{"type": "text", "value": SYSTEM_PROMPT}]}
            )
        conversation.append({"role": "user", "content": [{"type": "text", "value": question}]})
        conversation.append({"role": "assistant", "content": [{"type": "text", "value": answer}]})
        lines.append({"conversation": conversation})
    assert len(lines) == 10
    return lines


def _flat_prompt_response_lines() -> list[dict]:
    """5 raw objects in the flat prompt/response shape (mapped like CSV)."""
    pairs = [
        (
            "How do I archive an old workspace?",
            "Open workspace settings and select Archive Workspace.",
        ),
        (
            "Can I recover an archived workspace?",
            "Yes, archived workspaces can be restored from the Admin console within 90 days.",
        ),
        (
            "How do I change my workspace's timezone?",
            "Go to Workspace Settings, General, and update the Timezone field.",
        ),
        (
            "Why can't I invite new members?",
            "Your plan has reached its seat limit; upgrade or remove an inactive member first.",
        ),
        (
            "How do I download an audit log?",
            "Admins can download the audit log as CSV from the Security tab.",
        ),
    ]
    lines = [{"prompt": p, "response": r} for p, r in pairs]
    assert len(lines) == 5
    return lines


def _unmappable_lines() -> list[dict]:
    """5 raw objects with no recognizable shape -> unmappable."""
    lines = [
        {"note": "internal memo about the CSV importer"},
        {"id": 1001, "payload": {"foo": "bar"}},
        {"log_level": "info", "message": "job completed"},
        {"tags": ["urgent", "billing"], "assignee": "team-b"},
        {"raw_text": "unstructured customer feedback blob"},
    ]
    assert len(lines) == 5
    return lines


def _write_extra_dialogs_jsonl() -> dict:
    contract = _contract_shaped_lines()
    flat = _flat_prompt_response_lines()
    unmappable = _unmappable_lines()
    all_lines = contract + flat + unmappable
    assert len(all_lines) == 20

    path = FIXTURES_DIR / "extra_dialogs.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for obj in all_lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    return {
        "read": len(all_lines),
        "written": len(contract) + len(flat),
        "dropped": len(unmappable),
        "drops": {"unmappable": len(unmappable)},
    }


def _write_bad_lines_jsonl() -> None:
    """3 lines that fail `json.loads` (ING-U-004) — kept out of extra_dialogs.jsonl
    so the happy-path file ingests cleanly."""
    lines = [
        '{"question": "Unterminated string, "answer": "This line is deliberately malformed."}',
        '{"question": "Missing closing brace", "answer": "oops"',
        '["not", "an", "object",]',
    ]
    for line in lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            continue
        raise AssertionError(f"bad_lines.jsonl candidate line unexpectedly parses: {line!r}")

    path = FIXTURES_DIR / "bad_lines.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _merge_counts(csv_counts: dict, jsonl_counts: dict) -> dict:
    drops = dict.fromkeys(("duplicate", "too_short", "too_long", "unmappable"), 0)
    for counts in (csv_counts, jsonl_counts):
        for reason, count in counts["drops"].items():
            drops[reason] += count
    return {
        "read": csv_counts["read"] + jsonl_counts["read"],
        "written": csv_counts["written"] + jsonl_counts["written"],
        "dropped": csv_counts["dropped"] + jsonl_counts["dropped"],
        "drops": drops,
    }


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    csv_counts = _write_support_dialogs_csv()
    jsonl_counts = _write_extra_dialogs_jsonl()
    _write_bad_lines_jsonl()

    expected_counts = {
        "ingest": {
            "support_dialogs.csv": {"read": csv_counts["read"]},
            "extra_dialogs.jsonl": {"read": jsonl_counts["read"]},
            "combined": {"read": csv_counts["read"] + jsonl_counts["read"]},
        },
        "clean": {
            "support_dialogs.csv": csv_counts,
            "extra_dialogs.jsonl": jsonl_counts,
            "combined": _merge_counts(csv_counts, jsonl_counts),
        },
    }
    (FIXTURES_DIR / "expected_counts.json").write_text(
        json.dumps(expected_counts, indent=2, sort_keys=True) + "\n"
    )

    print(json.dumps(expected_counts, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
