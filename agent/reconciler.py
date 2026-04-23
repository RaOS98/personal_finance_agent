"""Bank-statement reconciliation via Amazon Bedrock (Claude Haiku 4.5).

Candidates for a single statement line are evaluated in one batched call to
reduce cost (the previous implementation sent one request per candidate).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

import config


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a bank reconciliation assistant. You receive a line from a bank statement and a list of candidate transactions the user has logged. All candidates share the same amount as the statement line, and all have dates close to it. Your job is to decide, for EACH candidate, whether it represents the same real-world transaction.

Evaluate each candidate based on:
- Merchant name similarity: Bank statements often use abbreviated or corporate names (e.g., "CENCOSUD RETAIL SA" for Wong supermarket, "UBER *TRIP" for an Uber ride). Consider whether the statement description plausibly refers to the same merchant.
- Date consistency: Small differences (1-2 days) are normal due to posting delays. Larger gaps are suspicious.
- Context: The candidate's category may help confirm the match (e.g., a "Transportation" transaction matching "UBER *TRIP").

Assign one of three verdicts per candidate:

CONFIDENT - Clearly the same transaction. Merchant names obviously refer to the same business and dates are consistent.

LIKELY - Probably the same transaction. Merchant names are plausibly related but there is some ambiguity (non-obvious abbreviation, date differs by more than 1 day).

UNCERTAIN - No obvious connection between the names, or another reason to doubt the match.

Respond with ONLY a JSON object of the form:
{"matches": [{"index": 0, "verdict": "confident", "reason": "..."}, {"index": 1, "verdict": "uncertain", "reason": "..."}]}
with one entry per candidate, in the same order as the input list. No other text."""


_bedrock = boto3.client(
    "bedrock-runtime",
    region_name=config.BEDROCK_REGION,
    config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
)


def _parse_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


def _null_verdict(reason: str = "Evaluation error") -> dict[str, Any]:
    return {"verdict": "uncertain", "reason": reason}


def evaluate_matches(
    statement_line: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Batched evaluation of every candidate for a single statement line.

    Args:
        statement_line: Dict with keys date, description, amount.
        candidates: List of dicts with keys date, merchant, amount, category.

    Returns:
        A list aligned to *candidates*, each element being a dict with keys
        ``verdict`` ("confident" | "likely" | "uncertain") and ``reason``. On
        failure, each element is a null-verdict placeholder.
    """
    if not candidates:
        return []

    candidate_lines = []
    for i, c in enumerate(candidates):
        candidate_lines.append(
            f"  [{i}] date={c.get('date')}, "
            f"merchant={c.get('merchant') or 'Unknown'}, "
            f"amount={c.get('amount')}, "
            f"category={c.get('category') or 'Unknown'}"
        )

    user_content = (
        "Statement line:\n"
        f"  date={statement_line.get('date')}\n"
        f"  description={statement_line.get('description')}\n"
        f"  amount={statement_line.get('amount')}\n"
        "\n"
        f"Candidates ({len(candidates)} total):\n"
        + "\n".join(candidate_lines)
    )

    messages = [{"role": "user", "content": [{"text": user_content}]}]

    system = [
        {"text": SYSTEM_PROMPT},
        {"cachePoint": {"type": "default"}},
    ]

    for attempt in range(2):
        try:
            response = _bedrock.converse(
                modelId=config.RECONCILER_MODEL_ID,
                system=system,
                messages=messages,
                inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
            )
            output_text = response["output"]["message"]["content"][0]["text"]
            parsed = _parse_json(output_text)
            raw_matches = parsed.get("matches", [])

            # Align response to candidates by index, defaulting to uncertain.
            by_index: dict[int, dict[str, Any]] = {}
            for m in raw_matches:
                try:
                    idx = int(m.get("index"))
                except (TypeError, ValueError):
                    continue
                verdict = str(m.get("verdict") or "uncertain").strip().lower()
                if verdict not in {"confident", "likely", "uncertain"}:
                    verdict = "uncertain"
                by_index[idx] = {
                    "verdict": verdict,
                    "reason": m.get("reason") or "",
                }

            return [by_index.get(i, _null_verdict("Missing verdict")) for i in range(len(candidates))]

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            if attempt == 0:
                logger.warning("Reconciler parse retry: %s", e)
                continue
            return [_null_verdict("Failed to parse LLM response") for _ in candidates]
        except Exception as e:
            logger.exception("Reconciler Bedrock call failed")
            return [_null_verdict(f"Bedrock error: {e}") for _ in candidates]
