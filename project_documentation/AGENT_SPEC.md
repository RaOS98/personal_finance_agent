# Agent Specification

## Overview

This document defines how the AI agent behaves: what prompts it receives, how it processes inputs, what outputs it produces, and the rules it follows. The agent is powered by Amazon Bedrock — Claude Sonnet 4.6 for vision extraction, Claude Haiku 4.5 for categorization and reconciliation — called via the Bedrock Converse API. The Telegram bot handles all user communication and passes structured requests to the agent.

All LLM calls use the Converse API with `cachePoint` system prompt blocks for prompt caching. Retries on JSON parse failure are handled internally.

## General Principles

- **The agent is a function, not a conversationalist.** It receives a structured input, performs a task, and returns a structured output. It does not maintain conversation history or memory between calls.
- **Never fabricate data.** If the agent cannot extract a field from an image or text, it must say so explicitly. A missing field is always better than an invented one.
- **Prefer asking over guessing.** When confidence is low on any extracted field, the agent should flag it as uncertain so the bot can ask the user.
- **Respond in structured format.** All agent outputs are JSON. No prose, no explanations, no markdown — just the requested data structure.
- **Language handling.** Inputs (receipts, messages) are primarily in Spanish. The agent must handle Spanish-language receipts and merchant names. User messages may mix Spanish and English. All structured output field values are in English (category names, status values) except merchant names which are preserved as-is.

---

## Agent Tasks

The agent performs four distinct tasks. Each task has its own system prompt, input format, and output format.

### Task 1: Transaction Extraction

**Purpose:** Extract structured transaction data from an image and/or user message.

**Model:** Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`)

**File:** `agent/extractor.py`

**Function signature:** `extract_transaction(image_bytes: bytes | None, user_message: str) -> dict`

**Input:** Image bytes (JPEG/PNG/WEBP/GIF) passed as a Bedrock image content block, plus a text message string.

**System prompt:**
```
You are a transaction data extractor. You receive an image of a receipt,
transfer screenshot, or payment confirmation, along with an optional user
message. Extract the following fields from the image and message.

Fields to extract:
- merchant: The business name or payee. Preserve the original name as shown.
- amount: The total amount charged. Use the final total, not subtotals.
- currency: "PEN" for soles (S/.) or "USD" for dollars ($).
- date: The transaction date in YYYY-MM-DD format. If not visible, use null.
- payment_method_alias: The payment method mentioned in the user message.
  Extract the keyword as-is (e.g., "sapphire", "amex", "yape", "cash").
  If not mentioned, use null.
- category_hint: Any category-related keyword from the user message
  (e.g., "groceries", "food", "taxi"). If not mentioned, use null.

Rules:
- If the image is unreadable or missing, extract what you can from the
  user message alone and set unreadable fields to null.
- Never invent or guess values. Use null for anything you cannot determine.
- If multiple amounts appear (subtotal, tax, tip, total), use the final total.
- For Yape/Plin screenshots, the amount and recipient are the key fields.

Respond with ONLY a JSON object, no other text.
```

**Expected output:**
```json
{
  "merchant": "Wong Supermercados",
  "amount": 145.30,
  "currency": "PEN",
  "date": "2026-04-13",
  "payment_method_alias": "sapphire",
  "category_hint": "groceries"
}
```

**Error handling:** On JSON parse failure, the call is retried once. If it fails twice, the raw response is logged and the bot asks the user to re-send or provide the transaction manually.

---

### Task 2: Transaction Categorization

**Purpose:** Assign a category to a transaction based on extracted data and user hint.

**Model:** Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)

**File:** `agent/categorizer.py`

**Function signature:** `categorize_transaction(merchant, amount, category_hint, user_note=None) -> dict`

**System prompt:**
```
You are a transaction categorizer. Given a merchant name, amount, and an
optional category hint from the user, assign the most appropriate category.

Available categories:
1. food_dining — Restaurants, delivery apps, cafés
2. groceries — Supermarkets, markets, bodegas
3. transportation — Fuel, taxis, Uber, parking, tolls
4. housing — Rent, maintenance, repairs, home services
5. utilities — Electricity, water, gas, internet, phone
6. health — Pharmacy, doctors, clinics, insurance, medical
7. personal_care — Haircuts, salons, barbers, grooming, cosmetics, spa, gym
8. entertainment — Streaming, outings, events, hobbies
9. shopping — Clothing, electronics, household items
10. education — Courses, books, learning subscriptions
11. work — Work-related expenses (business meals, coworking, office supplies, work travel)
12. other — Only when none of the above fit

Rules:
- If the user provided a category hint, respect it unless it is clearly wrong.
- Use the merchant name to validate or infer the category when the hint is
  absent or ambiguous.
- When assigning "other", set needs_description to true.
- If you are not confident in the categorization, set confident to false.

Respond with ONLY a JSON object, no other text.
```

**Expected output:**
```json
{
  "category_slug": "groceries",
  "confident": true,
  "needs_description": false
}
```

When `confident` is false, the bot presents the category options to the user for manual selection instead of using the agent's suggestion.

---

### Task 3: Statement Line Parsing

**Purpose:** Extract structured line items from a bank statement PDF.

**Model:** None — handled entirely in Python with `pdfplumber`.

**File:** `agent/statement_parser.py`

**Function signature:** `parse_statement_pdf(pdf_source: str | bytes) -> list[dict]`

**Process:**
1. Open the PDF with `pdfplumber`
2. Iterate all pages and extract tables
3. Match rows where the first cell is a `DD/MM/YYYY` or `DD/MM` date
4. Parse each matching row into `{date, description, amount}`
5. Skip rows with zero or negative amounts (credits)

**Output format (per line):**
```json
{
  "date": "2026-04-13",
  "description": "CENCOSUD RETAIL SA",
  "amount": 145.30
}
```

**Notes:**
- BCP statements contain selectable text, so OCR is not needed.
- If parsing fails or no lines are found, `ValueError` is raised and the bot surfaces the error to the user.
- Statement parsing logic will need to be adapted per bank/card when additional accounts are added. For now, only BCP statement formats are handled.

---

### Task 4: Reconciliation Matching (Batched)

**Purpose:** Evaluate whether a bank statement line matches one or more logged transactions.

**Model:** Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)

**File:** `agent/reconciler.py`

**Function signature:** `evaluate_matches(statement_line: dict, candidates: list[dict]) -> list[dict]`

All candidates for a given statement line are evaluated in a **single Bedrock call**. The model returns a verdict for each candidate, aligned to the input order.

**Input:**
```json
{
  "statement_line": {
    "date": "2026-04-13",
    "description": "CENCOSUD RETAIL SA",
    "amount": 145.30
  },
  "candidates": [
    {
      "index": 0,
      "date": "2026-04-13",
      "merchant": "Wong Supermercados",
      "amount": 145.30,
      "category": "Groceries"
    }
  ]
}
```

By the time the agent sees this input, Python has already pre-filtered candidates by exact amount match and date proximity (±5 days via GSI1 range query) within the same settlement account.

**System prompt:**
```
You are a bank reconciliation assistant. You receive a line from a bank
statement and a list of candidate transactions logged by the user. All
candidates have the same amount and similar dates. Your job is to
determine whether each candidate represents the same real-world
transaction as the statement line.

Evaluate based on:
- Merchant name similarity: Bank statements often use abbreviated or
  corporate names (e.g., "CENCOSUD RETAIL SA" for Wong supermarket,
  "UBER *TRIP" for an Uber ride). Consider whether the statement
  description plausibly refers to the same merchant.
- Date consistency: Small differences (1-2 days) are normal due to
  posting delays. Larger gaps are suspicious.
- Context: The transaction category may help confirm the match.

Assign one of three verdicts per candidate:

CONFIDENT — Clearly the same transaction.
LIKELY — Probably the same transaction, but some ambiguity exists.
UNCERTAIN — Not clear. The merchant names do not have an obvious connection.

Respond with ONLY a JSON object in this exact format, no other text:
{"matches": [{"index": 0, "verdict": "confident", "reason": "..."}]}
```

**Return value:** List of dicts, one per candidate, in input order:
```json
[
  {"verdict": "confident", "reason": "CENCOSUD RETAIL SA is the corporate name for Wong supermarkets."}
]
```

**Verdict logic (applied by the bot after receiving results):**

| Situation | Action |
|-----------|--------|
| Exactly one CONFIDENT candidate | Auto-confirm match |
| No CONFIDENT but one or more LIKELY | Present options to user |
| All UNCERTAIN | Present all to user with full context |
| Multiple CONFIDENT | Present to user (indicates a potential duplicate) |
| No candidates found | Prompt user to add or skip |

Unknown verdict values from the model are coerced to `uncertain`.

---

## Application-Level Logic

The following logic is handled in Python, not by the agent.

### Payment Method Resolution

When the agent returns a `payment_method_alias` (e.g., "sapphire"), the application:
1. Searches the in-memory payment method cache for a match in the `aliases` list (case-insensitive)
2. If found, uses that payment method and its linked settlement account
3. If not found, the bot asks the user to clarify

### Duplicate Detection

Before saving a confirmed transaction, the application checks:
1. Query DynamoDB for transactions with the same `amount`, `date`, and `payment_method_id`
2. If matches are found, the bot asks the user to confirm this is a distinct transaction

### Reconciliation Orchestration

1. User uploads a statement PDF and identifies the account
2. `statement_parser.parse_statement_pdf()` extracts lines
3. Lines are stored in DynamoDB (idempotent via content-hash IDs)
4. For each pending statement line:
   - Query via GSI1: same `account_id` + `amount_cents`, date within ±5 days
   - If no candidates → mark `pending`, notify user
   - If candidates found → call `reconciler.evaluate_matches(line, candidates)` once
   - Apply verdict logic (see table above)
   - Update statement line and transaction statuses in DynamoDB
5. Send summary to user: X auto-matched, Y need review, Z unmatched

### Image Storage

1. Photo arrives → upload to `receipts/tmp/{user_id}.jpg` via `s3_store.upload_tmp_image()`
2. Transaction confirmed → `s3_store.finalize_image()` copies to `receipts/{yyyy}/{mm}/txn_{id}.jpg`, deletes tmp
3. Transaction cancelled → `s3_store.delete_tmp_image()` removes tmp object

---

## Error Handling

**Agent returns invalid JSON:** Retry once. If it fails again, log the raw response and ask the user to re-send or provide the transaction manually via text.

**Agent returns null for critical fields (amount, currency):** Do not attempt to save. The bot asks the user to provide the missing information.

**PDF parsing fails:** Notify the user that the statement could not be processed and suggest re-uploading or checking the file format.

**Bedrock service error:** The bot informs the user that processing is temporarily unavailable and to try again shortly.

---

## Prompt Evolution

The prompts defined in this document are starting points. They should be refined based on real-world usage:

- **Extraction accuracy:** If the agent consistently misreads a particular receipt format, the extraction prompt may need format-specific hints.
- **Categorization accuracy:** If certain merchants are frequently miscategorized, the categorization prompt can include a merchant-to-category mapping as context.
- **Reconciliation accuracy:** As the user processes more statements, common BCP merchant name patterns (corporate names vs. common names) can be added to the reconciliation prompt.

All prompt changes should be tracked in version control alongside the application code.
