"""Intent classification via Amazon Bedrock (Claude Haiku 4.5).

Routes free-form Telegram text to one of two intents:
- new_transaction: log a new spend (existing flow)
- edit: edit a previously logged transaction
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

import config


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an intent classifier for a personal finance Telegram bot. Messages are bilingual (English and Spanish). Classify each user message into EXACTLY ONE of two intents:

1. new_transaction - The user is logging a new spend. Typically includes an amount plus a merchant or purpose, or is a short phrase describing a purchase. Also use this for questions, chit-chat, or anything that is NOT clearly an edit of a prior logged transaction.
   Examples:
   - "starbucks 25 soles yape"
   - "lunch 40 amex"
   - "almuerzo 30 soles"
   - "uber 15 yape"
   - "taxi al aeropuerto 50"
   - "gas sapphire 120"
   - "pago de luz 180 yape"
   - "how much did I spend on food?" (no edit — treat as new_transaction)

2. edit - The user wants to modify a previously logged transaction. Typically contains verbs like change, update, edit, fix, cambia, cambiar, actualiza, modificar, or refers to "the last one", "my last transaction", "la ultima", "el anterior".
   Examples:
   - "change the last one to 30 soles"
   - "cambia el monto a 25"
   - "update merchant to Juan"
   - "fix the category, it was groceries"
   - "actualiza la fecha a ayer"

Rules:
- When a message contains both an amount AND a merchant/purpose, default to new_transaction unless the message explicitly says edit/change/update or clearly refers to a prior transaction.
- Short standalone amounts ("25 soles yape") are new_transaction.
- Commands to change/update/fix a logged entry are edit.
- Set confident=false only if the intent is genuinely ambiguous.

Respond with ONLY a JSON object with exactly these keys: intent, confident. The value of intent MUST be one of "new_transaction" or "edit". No other text."""


NULL_RESULT: dict[str, Any] = {
    "intent": "new_transaction",
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


def classify_intent(text: str) -> dict:
    """Classify a Telegram text message into one of two intents.

    Returns a dict with keys: intent, confident. On failure, returns the
    fail-open default (new_transaction, unconfident) plus an "error" key.
    """
    messages = [{"role": "user", "content": [{"text": text}]}]

    system = [
        {"text": SYSTEM_PROMPT},
        {"cachePoint": {"type": "default"}},
    ]

    for attempt in range(2):
        try:
            response = _bedrock.converse(
                modelId=config.CLASSIFIER_MODEL_ID,
                system=system,
                messages=messages,
                inferenceConfig={"maxTokens": 64, "temperature": 0.0},
            )
            output_text = response["output"]["message"]["content"][0]["text"]
            parsed = _parse_json(output_text)
            intent = parsed.get("intent")
            if intent not in {"new_transaction", "edit"}:
                if attempt == 0:
                    logger.warning("Classifier returned unexpected intent: %r", intent)
                    continue
                return {**NULL_RESULT, "error": f"Unexpected intent: {intent!r}"}
            return {
                "intent": intent,
                "confident": bool(parsed.get("confident", False)),
            }
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            if attempt == 0:
                logger.warning("Classifier parse retry: %s", e)
                continue
            return {**NULL_RESULT, "error": "Failed to parse LLM response as JSON"}
        except Exception as e:
            logger.exception("Classifier Bedrock call failed")
            return {**NULL_RESULT, "error": f"Bedrock request failed: {e}"}
