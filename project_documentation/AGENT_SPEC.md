# Agent Specification

## Overview

This document defines how the AI agent behaves: what prompts it receives, how it processes inputs, what outputs it produces, and the rules it follows. The agent is powered by Amazon Bedrock — Claude Sonnet 4.6 for vision extraction, Claude Haiku 4.5 for everything else (intent classification, categorization, edit parsing, reconciliation) — called via the Bedrock Converse API. The Telegram bot handles all user communication and passes structured requests to the agent.

All LLM calls use the Converse API with `cachePoint` system prompt blocks for prompt caching. Retries on JSON parse failure are handled internally.

## General Principles

- **The agent is a function, not a conversationalist.** It receives a structured input, performs a task, and returns a structured output. It does not maintain conversation history or memory between calls.
- **Never fabricate data.** If the agent cannot extract a field from an image or text, it must say so explicitly. A missing field is always better than an invented one.
- **Prefer asking over guessing.** When confidence is low on any extracted field, the agent should flag it as uncertain so the bot can ask the user.
- **Respond in structured format.** All agent outputs are JSON. No prose, no explanations, no markdown — just the requested data structure.
- **Language handling.** Inputs (receipts, messages) are primarily in Spanish. The agent must handle Spanish-language receipts and merchant names. User messages may mix Spanish and English. All structured output field values are in English (category names, status values) except merchant names which are preserved as-is.

---

## Agent Tasks

The agent performs six distinct tasks. Each task has its own system prompt, input format, and output format.

### Task 0: Intent Classification

**Purpose:** Route a free-form Telegram text message to one of two downstream flows.

**Model:** Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)

**File:** `agent/intent_classifier.py`

**Function signature:** `classify_intent(text: str) -> dict`

**Bypass conditions** (handled before the classifier is called):
- The message contains a photo (always treated as `new_transaction`).
- The user is mid-flow (`state.state` is non-empty) — the existing state-machine handler runs instead.
- The message starts with `!` (forces `edit`); the prefix is stripped before downstream processing.

**System prompt:**
```
You are an intent classifier for a personal finance Telegram bot.
Messages are bilingual (English and Spanish). Classify each user
message into EXACTLY ONE of two intents:

1. new_transaction — User is logging a new spend, or the message is
   not clearly an edit of a prior transaction (including questions).
2. edit — User wants to modify a previously logged transaction.
   Verbs like change, update, edit, fix, cambia, cambiar, actualiza.

Rules:
- When a message contains both an amount AND a merchant/purpose,
  default to new_transaction unless the message explicitly says
  edit/change/update or clearly refers to a prior transaction.
- Set confident=false only if genuinely ambiguous.

Respond with ONLY a JSON object: {intent, confident}.
```

**Expected output:**
```json
{"intent": "new_transaction", "confident": true}
```

**Error handling:** Fails open. On JSON parse failure, unrecognized intent, or any Bedrock error, the function returns `{"intent": "new_transaction", "confident": false, "error": "..."}`. The bot then falls through to the new-transaction flow. Rationale: missing a new transaction silently loses user data; misclassifying an edit only prompts a rephrase.

---

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
4. For each matching row, parse every non-date cell with `_parse_amount` and split them into description text vs. numeric values
5. Build a signed amount:
   - If the row has one numeric value, keep its sign as `_parse_amount` returned it (including `( … )`, leading `-`, trailing `CR`, leading `ABONO` markers)
   - If the row has two numeric columns (BCP "Cargos/Consumos" + "Abonos/Pagos"), the rightmost is treated as a credit and negated when populated
6. Skip rows whose final amount is exactly zero (header padding); negative amounts (refunds, credits) are kept

**Output format (per line):**
```json
{
  "date": "2026-04-13",
  "description": "CENCOSUD RETAIL SA",
  "amount": 145.30
}
```

`amount` is signed: positive for charges/debits, negative for refunds/credits/reversals. Downstream code (DynamoDB writers, GSI1 candidate lookup, the reconciler prompt) all assume the same convention so refunds are first-class citizens.

**Notes:**
- BCP statements contain selectable text, so OCR is not needed.
- If parsing fails or no lines are found, `ValueError` is raised and the bot/dashboard surface the error to the user.
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

By the time the agent sees this input, Python has already pre-filtered candidates by exact signed-amount match and date proximity (±5 days via GSI1 range query) within the same settlement account.

**System prompt** (kept in sync with `agent/reconciler.py`):
```
You are a bank reconciliation assistant. You receive a line from a bank
statement and a list of candidate transactions the user has logged. All
candidates share the same signed amount as the statement line, and all
have dates close to it. Your job is to decide, for EACH candidate, whether
it represents the same real-world transaction.

Amounts are signed: positive values are charges/debits, negative values
are credits/refunds/reversals. The pre-filter guarantees the statement
line and every candidate share the same sign and absolute amount, so you
do not need to second-guess the figure itself; focus on whether the
merchant and date are consistent. For credit lines, look for matching
refund/return transactions or original purchases that the user later
reversed.

Evaluate each candidate based on:
- Merchant name similarity: Bank statements often use abbreviated or
  corporate names (e.g., "CENCOSUD RETAIL SA" for Wong supermarket,
  "UBER *TRIP" for an Uber ride). For credits, an "AMAZON REFUND" line
  plausibly reverses an earlier "Amazon" purchase.
- Date consistency: Small differences (1-2 days) are normal due to posting
  delays. Refunds may post several days after the original charge.
- Context: The candidate's category may help confirm the match.

Assign one of three verdicts per candidate:

CONFIDENT - Clearly the same transaction.
LIKELY - Probably the same transaction, with some ambiguity.
UNCERTAIN - No obvious connection, or another reason to doubt the match.

Respond with ONLY a JSON object:
{"matches": [{"index": 0, "verdict": "confident", "reason": "..."}, ...]}
with one entry per candidate, in the same order as the input list.
```

**Return value:** List of dicts, one per candidate, in input order:
```json
[
  {"verdict": "confident", "reason": "CENCOSUD RETAIL SA is the corporate name for Wong supermarkets."}
]
```

**Auto-match logic (applied by `agent/reconciliation.pick_auto_match` after receiving results):**

| Situation | Action |
|-----------|--------|
| Exactly one CONFIDENT candidate | Auto-match |
| Multiple CONFIDENT candidates | Pick the one with the smallest `|Δdate|`; ties leave the line pending |
| No CONFIDENT but one or more LIKELY/UNCERTAIN | Leave line pending → manual review in the dashboard |
| No candidates found | Leave line pending |

Unknown verdict values from the model are coerced to `uncertain`. The dashboard "Manual Reconciliation" page is the canonical surface for everything that isn't auto-matched; the bot only sends a one-shot summary message after `auto_reconcile` finishes.

---

### Task 5: Edit-Request Parsing

**Purpose:** Convert a natural-language edit instruction into a structured `{field, new_value}` against the user's most recent transaction.

**Model:** Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)

**File:** `agent/tx_editor.py`

**Function signature:** `parse_edit_request(text: str, target_txn: dict) -> dict`

**Input:** The user's edit instruction plus a formatted summary of the target transaction (id, merchant, amount, currency, date, category, payment method).

**Editable fields:**
- `merchant` (string)
- `description` (string)
- `amount` (number)
- `date` (ISO YYYY-MM-DD)
- `category_slug` (one of the 12 standard slugs)
- `payment_method_id` (model emits the alias verbatim — `yape`, `amex`, etc. — and the handler resolves it)

**Rules:**
- Pick exactly ONE field per request. If the user asked for multiple changes, pick the first and set `confident=false`.
- For relative dates ("yesterday", "last Tuesday"), emit the ISO date if it can be computed confidently; otherwise leave `new_value=null` and set `confident=false`.
- For categories, map natural-language hints (e.g., "food" → `food_dining`, "uber/taxi" → `transportation`).

**Expected output:**
```json
{"field": "amount", "new_value": 25.0, "confident": true}
```

**v1 scope:** edits the most recent transaction only. Reference resolution ("my last Starbucks", "the 25 soles one from Tuesday") is out of scope.

**Confirmation gate:** Every edit goes through a mandatory `awaiting_edit_target_confirm` state with a yes/no keyboard before any DynamoDB write. The bot displays the old → new diff and the txn id so the user can abort.

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

The auto-reconciliation loop lives in `agent/reconciliation.py` (`auto_reconcile`) and is called identically by the Telegram bot and by the Streamlit "Upload Statement" page. The dashboard passes a `progress_callback` to drive its `st.progress` bar; the bot passes `None`.

1. User uploads a statement PDF (Telegram or dashboard) and identifies the account + billing period
2. The PDF is uploaded to S3 (`statements/{account_id}/{period}/{uuid}.pdf`) *before* parsing — a parse failure still leaves the original document recoverable
3. `statement_parser.parse_statement_pdf()` extracts signed-amount lines
4. `db.save_statement_lines(...)` persists them with the shared `pdf_s3_key` (idempotent via content-hash IDs and `attribute_not_exists(PK)` condition)
5. `auto_reconcile(billing_period, account_id)` iterates pending lines:
   - Query via GSI1: same `account_id` + signed `amount_cents`, date within ±5 days
   - If no candidates → leave `pending`
   - If candidates found → call `reconciler.evaluate_matches(line, candidates)` once
   - Apply `pick_auto_match` (see table in Task 4): write a reconciliation record only when there is a single confident pick or a clean smallest-`|Δdate|` winner
6. Send a summary back to the entry-point (bot reply or dashboard toast): X auto-matched, Y still pending
7. Anything still pending is reviewed in the dashboard's "Manual Reconciliation" page

### Image Storage

1. Photo arrives → upload to `receipts/tmp/{user_id}.jpg` via `s3_store.upload_tmp_image()`
2. Transaction confirmed → `s3_store.finalize_image()` copies to `receipts/{yyyy}/{mm}/txn_{id}.jpg`, deletes tmp
3. Transaction cancelled → `s3_store.delete_tmp_image()` removes tmp object

### Statement PDF Storage

1. PDF arrives (Telegram document or dashboard upload) → `s3_store.upload_statement_pdf(account_id, billing_period, pdf_bytes)` returns the final key
2. `db.save_statement_lines` stamps `pdf_s3_key` on every line that came out of the parser
3. The dashboard renders a presigned `s3_store.statement_pdf_url(...)` link on each line so the user can open the original PDF for context

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
