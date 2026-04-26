"""Transaction edit-request parsing via Amazon Bedrock (Claude Haiku 4.5).

Takes a natural-language edit instruction plus the target transaction and
returns a structured description of what field to change to what value.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

import config


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a transaction-edit parser for a personal finance Telegram bot. You receive a summary of the user's most recent logged transaction plus a natural-language instruction describing what to change. Output a single structured edit.

Editable fields:
- merchant: the business name or payee (string)
- description: a short note describing the spend (string)
- amount: the total amount charged (number)
- date: the transaction date, ISO YYYY-MM-DD (string)
- category_slug: one of food_dining, groceries, transportation, housing, utilities, health, personal_care, entertainment, shopping, education, work, other
- payment_method_id: when the user names a payment method, emit the alias verbatim (e.g. "sapphire", "amex", "yape", "cash") as a STRING — the handler resolves it to an id.

Rules:
- Pick exactly ONE field to change per request. If the user asked for multiple changes, pick the first one and set confident=false.
- For amount, return a plain number (no currency symbols).
- For date, if the user says "yesterday", "today", "last Tuesday", etc., emit the ISO date — BUT if you cannot compute it confidently, leave new_value null and set confident=false.
- For category_slug, emit the closest-matching slug from the list above. Map "food" → food_dining, "groceries" → groceries, "uber/taxi/gas" → transportation, "rent" → housing, etc.
- For payment_method_id, emit the alias string the user said (e.g. "yape", "amex") — do not try to resolve to an id.
- Set confident=false when the intent is genuinely ambiguous, the target field is unclear, or you cannot parse a valid new value.

Respond with ONLY a JSON object with exactly these keys: field, new_value, confident. If you cannot determine a valid field, set field=null and confident=false. No other text."""


NULL_RESULT: dict[str, Any] = {
    "field": None,
    "new_value": None,
    "confident": False,
}


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


def _format_target(target_txn: dict[str, Any]) -> str:
    return (
        f"Target transaction (id={target_txn.get('id')}):\n"
        f"  merchant:     {target_txn.get('merchant') or '—'}\n"
        f"  description:  {target_txn.get('description') or '—'}\n"
        f"  amount:       {target_txn.get('amount', '—')}\n"
        f"  currency:     {target_txn.get('currency', '—')}\n"
        f"  date:         {target_txn.get('date') or '—'}\n"
        f"  category:     {target_txn.get('category_slug') or '—'}\n"
        f"  paid with:    {target_txn.get('payment_method_name') or '—'}"
    )


def parse_edit_request(text: str, target_txn: dict[str, Any]) -> dict:
    """Parse an edit instruction against a target transaction.

    Returns a dict with keys: field, new_value, confident. On failure or
    ambiguity, returns the null result with confident=false.
    """
    user_content = (
        f"{_format_target(target_txn)}\n\n"
        f"User's edit instruction: {text.strip()}"
    )

    messages = [{"role": "user", "content": [{"text": user_content}]}]

    system = [
        {"text": SYSTEM_PROMPT},
        {"cachePoint": {"type": "default"}},
        {"text": f"Today's date is {date.today().isoformat()}. Use this to resolve relative expressions like 'yesterday', 'today', 'last Tuesday', or 'next Friday'."},
    ]

    for attempt in range(2):
        try:
            response = _bedrock.converse(
                modelId=config.CLASSIFIER_MODEL_ID,
                system=system,
                messages=messages,
                inferenceConfig={"maxTokens": 128, "temperature": 0.0},
            )
            output_text = response["output"]["message"]["content"][0]["text"]
            parsed = _parse_json(output_text)
            return {
                "field": parsed.get("field"),
                "new_value": parsed.get("new_value"),
                "confident": bool(parsed.get("confident", False)),
            }
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            if attempt == 0:
                logger.warning("tx_editor parse retry: %s", e)
                continue
            return {**NULL_RESULT, "error": "Failed to parse LLM response as JSON"}
        except Exception as e:
            logger.exception("tx_editor Bedrock call failed")
            return {**NULL_RESULT, "error": f"Bedrock request failed: {e}"}
