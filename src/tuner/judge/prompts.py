"""Judge rubric prompt (docs/03-components/judge.md scoring protocol).

RUBRIC_V1 is the versioned rubric identifier logged to MLflow (JDG-I-026) and embedded
here as a module constant (JDG-U-017) — bump it (RUBRIC_V2, ...) if the instructions or
dimensions below ever change, so Gold tiers scored under different rubrics stay
distinguishable (determinism caveat, docs/03-components/judge.md error handling).
"""

from __future__ import annotations

from typing import Any

RUBRIC_V1 = "judge-rubric-v1"

_INSTRUCTIONS = """You are an expert evaluator of AI assistant responses.

Score the conversation below on these dimensions, holistically, as a single integer
from 1 (worst) to 10 (best):
- Instruction adherence: does the assistant follow what was asked?
- Factual plausibility: is the answer plausible, not self-contradictory?
- Completeness: does it fully address the request?
- Tone and formatting: is it appropriately toned and well formatted?

Respond with ONLY a JSON object of this exact shape, and nothing else:
{"score": <integer 1-10>, "reasoning": "<one or two sentences>"}"""


def render_rubric_prompt(conversation: list[dict[str, Any]]) -> str:
    """Render one Silver `conversation` into the full rubric prompt text
    (docs/03-components/judge.md scoring protocol). Every turn's text is included,
    verbatim and role-labeled, in order (JDG-U-017)."""
    lines = [_INSTRUCTIONS, "", "Conversation:"]
    for turn in conversation:
        text = " ".join(part["value"] for part in turn["content"] if part.get("type") == "text")
        lines.append(f"{turn['role']}: {text}")
    return "\n".join(lines)
