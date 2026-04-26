"""Receipt / screenshot extraction via Amazon Bedrock (Claude Sonnet 4.6)."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

import config


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a transaction data extractor. You receive an image of a receipt, transfer screenshot, or payment confirmation, along with an optional user message. Extract the following fields from the image and message.

Fields to extract:
- merchant: The business name or payee. Preserve the original name as shown.
- amount: The total amount charged. Use the final total, not subtotals.
- currency: "PEN" for soles (S/.) or "USD" for dollars ($).
- date: The transaction date in YYYY-MM-DD format. If not visible, use null.
- payment_method_alias: The payment method used. Check both the image and the user message. For app screenshots, identify the platform from branding or UI elements (e.g., Yape logo or "Yapeaste" -> "yape", Plin logo -> "plin"). From the user message, extract the keyword as-is (e.g., "sapphire/visa", "platinum/amex", "yape", "cash"). If neither source reveals the payment method, use null.
- category_hint: What the spend was for, in a few words. Check both the user message and any description visible in the image (e.g., a receipt line item or Yape memo like "Limpieza departamento"). Exclude payment-method names. Include meal/activity words (e.g., "lunch", "dinner", "coffee", "groceries", "taxi", "gas") even if informal. For "lunch, yape" use category_hint "lunch" and payment_method_alias "yape". If neither the user message nor the image describe the purpose, use null.

Rules:
- If the image is unreadable or missing, extract what you can from the user message alone and set unreadable fields to null.
- Never invent or guess values. Use null for anything you cannot determine.
- If multiple amounts appear (subtotal, tax, tip, total), use the final total.
- For Yape/Plin screenshots, the amount and recipient are the key fields.

Respond with ONLY a JSON object, no other text."""


NULL_RESULT: dict[str, Any] = {
    "merchant": None,
    "amount": None,
    "currency": None,
    "date": None,
    "payment_method_alias": None,
    "category_hint": None,
}


_bedrock = boto3.client(
    "bedrock-runtime",
    region_name=config.BEDROCK_REGION,
    config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
)


def _detect_image_format(image_bytes: bytes) -> str:
    """Infer the image format from magic bytes. Falls back to jpeg."""
    if image_bytes.startswith(b"\x89PNG"):
        return "png"
    if image_bytes.startswith(b"GIF8"):
        return "gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "webp"
    return "jpeg"


def _build_user_content(image_bytes: bytes | None, user_message: str) -> list[dict]:
    parts: list[dict] = []
    if image_bytes is not None:
        parts.append(
            {
                "image": {
                    "format": _detect_image_format(image_bytes),
                    "source": {"bytes": image_bytes},
                }
            }
        )
    parts.append(
        {
            "text": user_message
            or "Extract transaction data from this image."
        }
    )
    return parts


def _parse_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


def extract_transaction(image_bytes: bytes | None, user_message: str) -> dict:
    """Extract transaction data from an image and/or user message via Bedrock.

    Args:
        image_bytes: Raw bytes of the receipt/screenshot image, or None.
        user_message: The user's text message accompanying the image.

    Returns:
        A dict with keys: merchant, amount, currency, date,
        payment_method_alias, category_hint.
        On failure, all values are None and an "error" key is added.
    """
    messages = [
        {
            "role": "user",
            "content": _build_user_content(image_bytes, user_message),
        }
    ]

    system = [
        {"text": SYSTEM_PROMPT},
        {"cachePoint": {"type": "default"}},
        {"text": f"Today's date is {date.today().isoformat()}. When a receipt shows a partial date (e.g., MM/DD without a year), prefer the most recent past or current year — receipts are typically photographed within days of the purchase."},
    ]

    for attempt in range(2):
        try:
            response = _bedrock.converse(
                modelId=config.EXTRACTOR_MODEL_ID,
                system=system,
                messages=messages,
                inferenceConfig={"maxTokens": 512, "temperature": 0.0},
            )
            output_text = response["output"]["message"]["content"][0]["text"]
            return _parse_json(output_text)
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            if attempt == 0:
                logger.warning("Extractor parse retry: %s", e)
                continue
            return {**NULL_RESULT, "error": "Failed to parse LLM response as JSON"}
        except Exception as e:
            logger.exception("Extractor Bedrock call failed")
            return {**NULL_RESULT, "error": f"Bedrock request failed: {e}"}
