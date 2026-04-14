import json

import ollama

import config

SYSTEM_PROMPT = """You are a bank reconciliation assistant. You receive a line from a bank statement and a candidate transaction logged by the user. Both have the same amount and similar dates. Your job is to determine whether they represent the same real-world transaction.

Evaluate based on:
- Merchant name similarity: Bank statements often use abbreviated or corporate names (e.g., "CENCOSUD RETAIL SA" for Wong supermarket, "UBER *TRIP" for an Uber ride). Consider whether the statement description plausibly refers to the same merchant.
- Date consistency: Small differences (1-2 days) are normal due to posting delays. Larger gaps are suspicious.
- Context: The transaction category may help confirm the match (e.g., a "Transportation" transaction matching "UBER *TRIP").

Assign one of three verdicts:

CONFIDENT — Clearly the same transaction. The merchant names obviously refer to the same business, and the dates are consistent.

LIKELY — Probably the same transaction. The amount matches and the merchant names are plausibly related, but there is some ambiguity (e.g., abbreviation is not obvious, or date differs by more than 1 day).

UNCERTAIN — Not clear. The merchant names do not have an obvious connection, or there is another reason to doubt the match.

Respond with ONLY a JSON object, no other text."""

NULL_RESULT = {
    "verdict": None,
    "reason": None,
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


def evaluate_match(statement_line: dict, candidate_transaction: dict) -> dict:
    """Evaluate whether a bank statement line matches a user-logged transaction.

    Args:
        statement_line: Dict with keys: date, description, amount.
        candidate_transaction: Dict with keys: date, merchant, amount, category.

    Returns:
        A dict with keys: verdict ("confident", "likely", or "uncertain"), reason.
        On failure, returns nulls with an "error" key.
    """
    client = ollama.Client(host=config.OLLAMA_BASE_URL)

    user_content = (
        f"Bank statement line:\n"
        f"  Date: {statement_line.get('date')}\n"
        f"  Description: {statement_line.get('description')}\n"
        f"  Amount: {statement_line.get('amount')}\n"
        f"\n"
        f"Candidate transaction:\n"
        f"  Date: {candidate_transaction.get('date')}\n"
        f"  Merchant: {candidate_transaction.get('merchant')}\n"
        f"  Amount: {candidate_transaction.get('amount')}\n"
        f"  Category: {candidate_transaction.get('category')}"
    )

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
