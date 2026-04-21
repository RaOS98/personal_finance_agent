# Agent Specification

## Overview

This document defines how the AI agent behaves: what prompts it receives, how it processes inputs, what outputs it produces, and the rules it follows. The agent is powered by Gemma4 31B running locally via Ollama and is called by the Python application — it does not interact with the user directly. The Telegram bot handles all user communication and passes structured requests to the agent.

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

**Input:**
```json
{
  "image": "<base64 encoded image or null>",
  "user_message": "Sapphire, groceries"
}
```

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

**Null field example (blurry receipt, no date visible):**
```json
{
  "merchant": null,
  "amount": 45.00,
  "currency": "PEN",
  "date": null,
  "payment_method_alias": "amex",
  "category_hint": null
}
```

---

### Task 2: Transaction Categorization

**Purpose:** Assign a category to a transaction based on extracted data and user hint.

**Input:**
```json
{
  "merchant": "Wong Supermercados",
  "amount": 145.30,
  "category_hint": "groceries"
}
```

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
6. health — Pharmacy, doctors, gym, insurance
7. entertainment — Streaming, outings, events, hobbies
8. shopping — Clothing, electronics, household items
9. education — Courses, books, learning subscriptions
10. other — Only when none of the above fit

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

**Low confidence example:**
```json
{
  "category_slug": "shopping",
  "confident": false,
  "needs_description": false
}
```

When `confident` is false, the bot presents the category options to the user for manual selection instead of using the agent's suggestion.

---

### Task 3: Statement Line Parsing

**Purpose:** Extract structured line items from a bank statement PDF. This task does not use the LLM — it is handled entirely in Python using `pdfplumber`.

**Process:**
1. Extract all text from the PDF using `pdfplumber`
2. Identify the tabular transaction section (date, description, amount columns)
3. Parse each row into a structured line item
4. Return the list of line items

**Output format (per line):**
```json
{
  "date": "2026-04-13",
  "description": "CENCOSUD RETAIL SA",
  "amount": 145.30
}
```

**Notes:**
- This is Python code, not an LLM call. Statement PDFs from BCP have selectable text and a consistent tabular format, making programmatic parsing more reliable than LLM extraction.
- If the PDF format changes or parsing fails, the system should surface the error to the user rather than guessing.
- Statement parsing logic will need to be adapted per bank/card when additional accounts are added post-MVP. For the MVP, only BCP statement formats need to be handled.

---

### Task 4: Reconciliation Matching

**Purpose:** Evaluate whether a logged transaction matches a bank statement line.

**Input:**
```json
{
  "statement_line": {
    "date": "2026-04-13",
    "description": "CENCOSUD RETAIL SA",
    "amount": 145.30
  },
  "candidate_transaction": {
    "date": "2026-04-13",
    "merchant": "Wong Supermercados",
    "amount": 145.30,
    "category": "Groceries"
  }
}
```

Note: By the time the agent sees this input, Python has already pre-filtered candidates by exact amount match and date proximity (±5 days) within the same settlement account. The agent only evaluates semantic similarity.

**System prompt:**
```
You are a bank reconciliation assistant. You receive a line from a bank 
statement and a candidate transaction logged by the user. Both have the 
same amount and similar dates. Your job is to determine whether they 
represent the same real-world transaction.

Evaluate based on:
- Merchant name similarity: Bank statements often use abbreviated or 
  corporate names (e.g., "CENCOSUD RETAIL SA" for Wong supermarket, 
  "UBER *TRIP" for an Uber ride). Consider whether the statement 
  description plausibly refers to the same merchant.
- Date consistency: Small differences (1-2 days) are normal due to 
  posting delays. Larger gaps are suspicious.
- Context: The transaction category may help confirm the match 
  (e.g., a "Transportation" transaction matching "UBER *TRIP").

Assign one of three verdicts:

CONFIDENT — Clearly the same transaction. The merchant names obviously 
refer to the same business, and the dates are consistent.

LIKELY — Probably the same transaction. The amount matches and the 
merchant names are plausibly related, but there is some ambiguity 
(e.g., abbreviation is not obvious, or date differs by more than 1 day).

UNCERTAIN — Not clear. The merchant names do not have an obvious 
connection, or there is another reason to doubt the match.

Respond with ONLY a JSON object, no other text.
```

**Expected output:**
```json
{
  "verdict": "confident",
  "reason": "CENCOSUD RETAIL SA is the corporate name for Wong supermarkets. Same amount, same date."
}
```

**Multiple candidates:** When Python finds multiple logged transactions matching the same statement line amount and date range, the agent is called once per candidate. The bot then uses the verdicts to decide:
- If exactly one candidate is Confident → auto-match
- If no candidate is Confident but one or more are Likely → present options to user
- If all are Uncertain → present all to user with context
- If multiple are Confident → present to user (this should be rare and indicates a potential duplicate)

---

## Application-Level Logic

The following logic is handled in Python, not by the agent.

### Payment Method Resolution

When the agent returns a `payment_method_alias` (e.g., "sapphire"), the application:
1. Searches the `payment_methods` table for a row where the alias appears in the `aliases` array (case-insensitive)
2. If found, uses that payment method and its linked settlement account
3. If not found, the bot asks the user to clarify

### Duplicate Detection

Before saving a confirmed transaction, the application checks:
1. Query for existing transactions with the same `amount`, `date`, and `payment_method_id`
2. If matches are found, the bot asks the user to confirm this is a distinct transaction

### Reconciliation Orchestration

The reconciliation flow is coordinated by the application:
1. User uploads a statement PDF and identifies the account
2. Application calls the statement parser (Task 3) to extract lines
3. Lines are stored in `statement_lines`
4. For each statement line with a positive amount (debits only in MVP):
   a. Query `transactions` for candidates: same amount, date within ±5 days, payment method settles to the same account
   b. If no candidates → mark as unmatched, notify user
   c. If candidates found → call agent (Task 4) for each candidate
   d. Apply verdict logic (see Task 4 multiple candidates section)
   e. Update `reconciliation_matches` and status fields
5. Send summary to user via Telegram: X auto-matched, Y need review, Z unmatched

### Image Storage

When the bot receives a photo:
1. Download the image from Telegram using the file ID
2. Save temporarily until the transaction is confirmed
3. On confirmation, move to `storage/images/txn_{id}.jpg`
4. Store the path in `transactions.image_path` and the Telegram file ID in `telegram_image_id`
5. On cancellation, delete the temporary file

---

## Error Handling

**Agent returns invalid JSON:** Retry once. If it fails again, log the raw response and ask the user to re-send or provide the transaction manually via text.

**Agent returns null for critical fields (amount, currency):** Do not attempt to save. The bot asks the user to provide the missing information.

**PDF parsing fails:** Notify the user that the statement could not be processed and suggest re-uploading or checking the file format.

**Ollama is unreachable:** The bot informs the user that processing is temporarily unavailable and to try again shortly.

---

## Prompt Evolution

The prompts defined in this document are starting points. They should be refined based on real-world usage. Specifically:

- **Extraction accuracy:** If the agent consistently misreads a particular receipt format, the extraction prompt may need format-specific hints.
- **Categorization accuracy:** If certain merchants are frequently miscategorized, the categorization prompt can include a merchant-to-category mapping as context.
- **Reconciliation accuracy:** As the user processes more statements, common BCP merchant name patterns (corporate names vs. common names) can be added to the reconciliation prompt to improve matching.

All prompt changes should be tracked in version control alongside the application code.
