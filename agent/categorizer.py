import json

import ollama

import config

SYSTEM_PROMPT = """You are a transaction categorizer. You receive a merchant name, amount, an optional structured category_hint from an upstream extractor, and sometimes the user's full Telegram caption or message verbatim.

Available categories:
1. food_dining — Restaurants, delivery apps, cafés
2. groceries — Supermarkets, markets, bodegas
3. transportation — Fuel, taxis, Uber, parking, tolls
4. housing — Rent, maintenance, repairs, home services
5. utilities — Electricity, water, gas, internet, phone
6. health — Pharmacy, doctors, gym, insurance
7. entertainment — Streaming, outings, events, hobbies
8. shopping — Clothing, electronics, household items
9. education — Courses, books, learning subscriptions
10. other — Only when none of the above fit

Rules:
- When a "User's own caption or message" line is present, treat the user's natural-language description of the spend as the strongest signal for category. Casual words (lunch, dinner, coffee, uber, rent, etc.) often map clearly to one category.
- For P2P app transfers (Yape, Plin, etc.), the merchant may be a person's name; do not require a business-like merchant to be confident if the user's words already describe the purchase.
- The structured category_hint complements the caption; if they disagree, prefer a clear, specific reading from the user's own words.
- Use the merchant name to validate or refine when it adds real signal.
- When assigning "other", set needs_description to true.
- Set confident to false only when the category is genuinely ambiguous after considering the user's words and merchant.

Respond with ONLY a JSON object, no other text."""

NULL_RESULT = {
    "category_slug": None,
    "confident": False,
    "needs_description": False,
}


def _parse_response(content: str) -> dict:
    """Parse JSON from the LLM response, stripping markdown fences if present."""
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
    """Categorize a transaction via Ollama.

    Args:
        merchant: The merchant/business name, or None.
        amount: The transaction amount, or None if not yet known.
        category_hint: Optional short hint from the extractor JSON.
        user_note: Verbatim user message or photo caption; primary signal when present.

    Returns:
        A dict with keys: category_slug, confident, needs_description.
        On failure, returns defaults with an "error" key.
    """
    client = ollama.Client(host=config.OLLAMA_BASE_URL)

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

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

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
