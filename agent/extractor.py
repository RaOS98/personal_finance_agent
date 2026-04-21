import base64
import json

import ollama

import config

SYSTEM_PROMPT = """You are a transaction data extractor. You receive an image of a receipt, transfer screenshot, or payment confirmation, along with an optional user message. Extract the following fields from the image and message.

Fields to extract:
- merchant: The business name or payee. Preserve the original name as shown.
- amount: The total amount charged. Use the final total, not subtotals.
- currency: "PEN" for soles (S/.) or "USD" for dollars ($).
- date: The transaction date in YYYY-MM-DD format. If not visible, use null.
- payment_method_alias: The payment method used. Check both the image and the user message. For app screenshots, identify the platform from branding or UI elements (e.g., Yape logo or "Yapeaste" → "yape", Plin logo → "plin"). From the user message, extract the keyword as-is (e.g., "sapphire/visa", "platinum/amex", "yape", "cash"). If neither source reveals the payment method, use null.
- category_hint: What the spend was for, in a few words. Check both the user message and any description visible in the image (e.g., a receipt line item or Yape memo like "Limpieza departamento"). Exclude payment-method names. Include meal/activity words (e.g., "lunch", "dinner", "coffee", "groceries", "taxi", "gas") even if informal. For "lunch, yape" use category_hint "lunch" and payment_method_alias "yape". If neither the user message nor the image describe the purpose, use null.

Rules:
- If the image is unreadable or missing, extract what you can from the user message alone and set unreadable fields to null.
- Never invent or guess values. Use null for anything you cannot determine.
- If multiple amounts appear (subtotal, tax, tip, total), use the final total.
- For Yape/Plin screenshots, the amount and recipient are the key fields.

Respond with ONLY a JSON object, no other text."""

NULL_RESULT = {
    "merchant": None,
    "amount": None,
    "currency": None,
    "date": None,
    "payment_method_alias": None,
    "category_hint": None,
}


def _build_messages(image_bytes: bytes | None, user_message: str) -> list[dict]:
    """Build the chat messages list for the ollama request."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if image_bytes is not None:
        encoded_image = base64.b64encode(image_bytes).decode("utf-8")
        messages.append(
            {
                "role": "user",
                "content": user_message or "Extract transaction data from this image.",
                "images": [encoded_image],
            }
        )
    else:
        messages.append({"role": "user", "content": user_message})

    return messages


def _parse_response(content: str) -> dict:
    """Parse JSON from the LLM response, stripping markdown fences if present."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (``` markers)
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


def extract_transaction(image_bytes: bytes | None, user_message: str) -> dict:
    """Extract transaction data from an image and/or user message via Ollama.

    Args:
        image_bytes: Raw bytes of the receipt/screenshot image, or None.
        user_message: The user's text message accompanying the image.

    Returns:
        A dict with keys: merchant, amount, currency, date,
        payment_method_alias, category_hint.
        On failure, all values are None and an "error" key is added.
    """
    client = ollama.Client(host=config.OLLAMA_BASE_URL)
    messages = _build_messages(image_bytes, user_message)

    for attempt in range(2):
        try:
            response = client.chat(model=config.OLLAMA_MODEL, messages=messages)
            return _parse_response(response["message"]["content"])
        except (json.JSONDecodeError, KeyError):
            if attempt == 0:
                continue
            return {**NULL_RESULT, "error": "Failed to parse LLM response as JSON"}
        except Exception as e:
            return {**NULL_RESULT, "error": f"Ollama request failed: {e}"}
