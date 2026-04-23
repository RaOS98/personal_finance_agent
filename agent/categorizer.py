"""Transaction categorization via Amazon Bedrock (Claude Haiku 4.5)."""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

import config


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a transaction categorizer. You receive a merchant name, amount, an optional structured category_hint from an upstream extractor, and sometimes the user's full Telegram caption or message verbatim.

Available categories:
1. food_dining - Restaurants, delivery apps, cafes
2. groceries - Supermarkets, markets, bodegas
3. transportation - Fuel, taxis, Uber, parking, tolls
4. housing - Rent, maintenance, repairs, home services
5. utilities - Electricity, water, gas, internet, phone
6. health - Pharmacy, doctors, clinics, insurance, medical
7. personal_care - Haircuts, salons, barbers, grooming, cosmetics, spa, gym
8. entertainment - Streaming, outings, events, hobbies
9. shopping - Clothing, electronics, household items
10. education - Courses, books, learning subscriptions
11. work - Work-related expenses (business meals, coworking, office supplies, work travel)
12. other - Only when none of the above fit

Rules:
- When a "User's own caption or message" line is present, treat the user's natural-language description of the spend as the strongest signal for category. Casual words (lunch, dinner, coffee, uber, rent, etc.) often map clearly to one category.
- For P2P app transfers (Yape, Plin, etc.), the merchant may be a person's name; do not require a business-like merchant to be confident if the user's words already describe the purchase.
- The structured category_hint complements the caption; if they disagree, prefer a clear, specific reading from the user's own words.
- Use the merchant name to validate or refine when it adds real signal.
- When assigning "other", set needs_description to true.
- Set confident to false only when the category is genuinely ambiguous after considering the user's words and merchant.

Respond with ONLY a JSON object with exactly these keys: category_slug, confident, needs_description. No other text."""


NULL_RESULT: dict[str, Any] = {
    "category_slug": None,
    "confident": False,
    "needs_description": False,
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


def categorize_transaction(
    merchant: str | None,
    amount: float | None,
    category_hint: str | None,
    user_note: str | None = None,
) -> dict:
    """Categorize a transaction via Bedrock.

    Returns a dict with keys: category_slug, confident, needs_description.
    On failure, returns defaults with an "error" key.
    """
    lines = [
        f"Merchant: {merchant or 'Unknown'}",
        f"Amount: {amount if amount is not None else 'Unknown'}",
        f"Category hint (from structured extraction, may be empty): "
        f"{category_hint or 'None'}",
    ]
    note = (user_note or "").strip()
    if note:
        lines.append(f"User's own caption or message (verbatim): {note}")
    user_content = "\n".join(lines)

    messages = [{"role": "user", "content": [{"text": user_content}]}]

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
                inferenceConfig={"maxTokens": 256, "temperature": 0.0},
            )
            output_text = response["output"]["message"]["content"][0]["text"]
            return _parse_json(output_text)
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            if attempt == 0:
                logger.warning("Categorizer parse retry: %s", e)
                continue
            return {**NULL_RESULT, "error": "Failed to parse LLM response as JSON"}
        except Exception as e:
            logger.exception("Categorizer Bedrock call failed")
            return {**NULL_RESULT, "error": f"Bedrock request failed: {e}"}
