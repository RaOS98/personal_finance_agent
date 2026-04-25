# Features

## Overview

This document describes the features of the Personal Finance Agent, organized by user flow. Each feature includes what it does, how the user interacts with it, and the expected behavior.

---

## 0. Intent-Based Routing

Free-form text messages are classified into one of three intents at the top of the message handler. The classifier (Claude Haiku 4.5) reads the message and returns one of:

- `new_transaction` — log a new spend (default)
- `edit` — modify a previously logged transaction
- `query` — answer a question about historical transactions

**Bypass conditions** (the classifier is skipped):
- The message contains a photo — always treated as a new transaction.
- The user is mid-flow (e.g., the bot is awaiting a missing field or category selection) — the existing state-machine handler runs instead.

**Escape hatches** (override the classifier):
- Prefix the message with `!` to force the edit flow (e.g., `!change merchant to Pedro`).
- Prefix the message with `?` to force the query flow (e.g., `?balance this month`).

**Failure mode:** The classifier fails open. Any error or unrecognized response defaults to `new_transaction`. The reasoning: missing a new transaction silently loses user data; misclassifying an edit/query just prompts a rephrase.

---

## 1. Transaction Capture

The primary feature of the system. The user logs transactions by sending messages to the Telegram bot.

### 1.1 Photo + Message

The most common input method. The user sends a photo (receipt, transfer screenshot, Yape/Plin confirmation) with a short message providing context.

**User sends:**
- A photo of a receipt
- A message like "Sapphire, groceries"

**Agent does:**
1. Extracts from the image: merchant name, amount, date, currency (Claude Sonnet 4.6 via Bedrock)
2. Parses the message: payment method (Sapphire) and category hint (groceries)
3. Maps payment method to settlement account (Sapphire → Sapphire credit card statement)
4. Maps category hint to standard category (groceries → Groceries) (Claude Haiku 4.5 via Bedrock)
5. Uploads the receipt image to S3 at a temporary key

**Bot replies with confirmation:**
```
📝 New transaction:
  Merchant:  Wong Supermercados
  Amount:    S/. 145.30
  Date:      2026-04-13
  Currency:  PEN
  Category:  Groceries
  Paid with: BCP Visa Infinite Sapphire

  ✅ Confirm    ✏️ Edit    ❌ Cancel
```

**User confirms:** Transaction is saved to DynamoDB; receipt image is moved to its permanent S3 key.

**User edits:** Bot asks which field to correct. User provides the correction, bot shows updated summary, user confirms.

**User cancels:** Transaction is discarded; temporary S3 image is deleted.

### 1.2 Text-Only Message

For cash transactions or cases where the user forgot to take a photo. The user sends a text message describing the transaction.

**User sends:**
```
Taxi to the airport, S/. 35, cash
```

**Agent does:**
1. Parses the text: merchant/description (taxi to the airport), amount (S/. 35), payment method (cash)
2. Infers category: Transportation
3. Sets date to today (unless specified otherwise)

**Bot replies with the same confirmation flow as 1.1.**

### 1.3 Handling Ambiguity

When the agent cannot confidently extract a field, it asks rather than guesses.

**Examples:**
- Image is blurry or unreadable → "I couldn't read this receipt clearly. Can you tell me the merchant, amount, and date?"
- No payment method provided → "Which card or account did you use for this?"
- Category is unclear → "How would you categorize this?" (presents the 12 categories as buttons)
- Amount found but currency ambiguous → "Is this S/. 45.00 or $45.00?"

The agent never stores a transaction with fabricated data. Missing fields are always asked for.

### 1.4 Supported Input Formats

| Input Type | Example | Notes |
|-----------|---------|-------|
| Receipt photo | Paper receipt from a store | Most common format |
| Transfer screenshot | BCP app transfer confirmation | Includes destination account info |
| Yape/Plin confirmation | Screenshot of Yape payment | Settlement routes to BCP soles account |
| Text message | "Lunch S/. 25, Amex" | No image required |

---

## 2. Transaction Storage

All confirmed transactions are stored in DynamoDB with the full set of structured fields defined in [DATA_MODEL.md](DATA_MODEL.md). Receipt images are stored in S3.

### 2.1 Required Fields

Every transaction must have these fields before it can be saved:
- Amount
- Currency (PEN or USD)
- Date
- Payment method
- Category

### 2.2 Optional Fields

These are captured when available but not required:
- Merchant name
- Description / notes
- Receipt image (stored in S3; key saved in `image_path`)

### 2.3 Duplicate Prevention

Before saving, the system checks for potential duplicates: same amount, same date, same payment method. If a likely duplicate is found, the bot asks the user to confirm it is a distinct transaction before saving.

---

## 3. Edit a Previously Logged Transaction

Send a natural-language correction to update the most recent transaction. The bot always shows a yes/no diff before any database write — destructive operations never skip the confirmation step.

### 3.1 Trigger

The intent classifier routes messages like "change the last one to 30 soles" or "cambia el monto a 25" to the edit flow. Alternatively, prefix any message with `!` to force the edit flow regardless of phrasing (e.g., `!change merchant to Pedro`).

### 3.2 Flow

**User sends:**
```
change amount to 25
```

**Agent does:**
1. Fetches the user's most recent transaction from DynamoDB.
2. Parses the edit instruction with Claude Haiku 4.5 → `{field: "amount", new_value: 25.0, confident: true}`.
3. Validates the new value (numeric for amount, ISO date for date, known slug for category, resolvable alias for payment method).

**Bot replies:**
```
Update transaction #42: change amount from 30.0 to 25.0?
  ✅ Yes    ❌ No
```

**User presses ✅ Yes:**
- The transaction is updated in DynamoDB.
- For `amount` and `date` edits, the underlying primary key (`SK = {date}#{txn_id:08d}`) and the GSI1 partition key (`AMT#{account_id}#{cents}`) are atomically rewritten via a `TransactWriteItems` (delete old + put new) call. `id`, `created_at`, `reconciliation_status`, and any image references are preserved verbatim.
- For other fields, a plain `UpdateItem` rewrites the affected attributes.
- The bot replies with the updated summary plus "✅ Updated."

**User presses ❌ No:** The state is cleared, the bot replies "❌ Edit cancelled," and DynamoDB is untouched.

### 3.3 Editable Fields

| Field | Notes |
|-------|-------|
| `merchant` | Free string |
| `description` | Free string |
| `amount` | Coerced to float; key rewrite required |
| `date` | ISO YYYY-MM-DD; key rewrite required |
| `category_slug` | Must be one of the 12 standard slugs. If the model emits something unknown, the bot presents the category keyboard for the user to pick. |
| `payment_method_id` | The model emits the alias verbatim (`yape`, `amex`, etc.); the handler resolves it via `db.resolve_payment_method`. |

### 3.4 Out of Scope (v1)

- Reference resolution to non-most-recent transactions (e.g., "my last Starbucks", "the 25 soles one from Tuesday").
- Multi-field edits in one turn.

These are deferred until real usage patterns are known.

---

## 4. Natural-Language Queries

Ask questions about historical spending in plain English or Spanish. The query agent runs a tool-use loop over DynamoDB and replies with cited transaction ids so the user can verify any number it cites.

### 4.1 Trigger

The intent classifier routes messages like "how much did I spend on food this month?" or "cuanto gaste en uber esta semana" to the query flow. Prefix any message with `?` to force it (e.g., `?balance`).

### 4.2 Flow

**User sends:**
```
how much did I spend on food this month?
```

**Bot replies:** `Thinking…` (covers the 2–4 second latency).

**Agent does:**
1. Calls `get_today` to resolve "this month" → date range.
2. Calls `aggregate_by_category(date_from, date_to)` (or `query_transactions(...)` filtered by `category_slug=food_dining`) to fetch the figures.
3. Returns a final JSON envelope `{answer, source_txn_ids}`.

**Bot replies:**
```
You spent S/. 312.50 on food this month across 8 transactions.

Based on: #42, #47, #51, #55, #60, #63, #68, #71
```

### 4.3 Tool Surface

The query agent has access to four tools:

| Tool | Purpose |
|------|---------|
| `get_today()` | Returns today's ISO date so the model can resolve relative time phrases. |
| `list_recent_transactions(limit)` | Fetches the N most recent transactions (capped at 50). |
| `query_transactions(date_from, date_to, category_slug?, payment_method_alias?)` | Date-range query with optional filters; capped at 50 rows. |
| `aggregate_by_category(date_from, date_to)` | Groups by category and sums per currency. |

### 4.4 Limitations

- Hard cap of 4 tool-use iterations per query — bounds worst-case latency.
- Tool results capped at 50 rows. If the full result exceeded the cap, the model sees `truncated: true` and is instructed to communicate the limitation in its answer.
- Stateless: the agent sees only the current question, not the prior conversation. Follow-up questions need to restate context.
- Currency assumed to be PEN unless the user explicitly says USD or dollars.

---

## 5. Monthly Reconciliation

At the end of each billing cycle, the user uploads bank statement PDFs for each active account/card. The system matches logged transactions against statement lines.

### 5.1 Statement Upload

**User sends:** A PDF file to the Telegram bot with a message identifying the account (e.g., "Sapphire statement April").

**Agent does:**
1. Parses the PDF with `pdfplumber` to extract individual statement lines (date, description, amount)
2. Identifies the account from the user's message
3. Stores the statement lines in DynamoDB (idempotent — re-uploading the same statement is safe)

### 5.2 Matching Process

Reconciliation runs in two stages:

**Stage 1 — Python pre-filter:**
- For each statement line, query DynamoDB via GSI1 for transactions with the same exact amount and settlement account within ±5 days of the statement date
- If no candidates are found, flag the statement line as pending
- If candidates are found, pass all of them to the agent in a single batched call

**Stage 2 — Agent evaluation (batched):**
- The agent receives the statement line and all candidates at once
- It assigns a verdict to each candidate: Confident, Likely, or Uncertain
- One Bedrock call per statement line, regardless of how many candidates exist

| Verdict | Meaning | Action |
|---------|---------|--------|
| **Confident** | Clearly the same transaction | Auto-confirmed |
| **Likely** | Probably the same | Sent to user for quick yes/no confirmation |
| **Uncertain** | Unclear match | Sent to user with context for manual review |

### 5.3 Reconciliation Review via Telegram

For Likely and Uncertain matches, the bot sends review requests:

**Likely match:**
```
🔄 Match found:
  Statement:   CENCOSUD RETAIL SA — S/. 145.30 — Apr 13
  Your record:  Wong Supermercados — S/. 145.30 — Apr 13 — Groceries

  Is this the same transaction?
  ✅ Yes    ❌ No
```

**Uncertain match (multiple candidates):**
```
❓ Unclear match:
  Statement: UBER *TRIP — S/. 18.50 — Apr 10

  Possible matches:
  1. Uber to office — S/. 18.50 — Apr 9 — Transportation
  2. Uber to dinner — S/. 18.50 — Apr 10 — Transportation

  Which one? (1, 2, or None)
```

**No match found:**
```
⚠️ No match:
  Statement: SPOTIFY — S/. 22.90 — Apr 5

  No logged transaction matches this charge.
  Would you like to add it now?
  ✅ Add    ⏭️ Skip
```

### 5.4 Reconciliation Outcomes

After reconciliation, every statement line has one of these statuses:
- **Matched** — linked to a logged transaction (auto or user-confirmed)
- **Added** — no prior record existed; user added it during reconciliation
- **Skipped** — user chose to skip (e.g., bank fees they don't want to track)
- **Pending** — not yet reviewed

Every logged transaction has one of these statuses:
- **Reconciled** — matched to a statement line
- **Unreconciled** — not yet matched (either no statement uploaded yet or no matching line found)

---

## 6. Dashboard

A local Streamlit application providing real-time visibility into personal finances. Reads from DynamoDB — requires AWS credentials in the environment.

```bash
export AWS_PROFILE=your-profile AWS_REGION=us-east-1
streamlit run dashboard/app.py
```

### 6.1 Monthly Summary

The default view. Shows spending for a selected month:
- Total spent (PEN and USD shown separately)
- Comparison to the previous month
- Number of transactions
- Spending by category (horizontal bar chart)
- Spending by payment method (horizontal bar chart)

### 6.2 Category Breakdown

Drill down into any category to see individual transactions. Filterable by date range and payment method.

### 6.3 Trends

Monthly spending over time:
- Total spending per month (line chart, by currency)
- Per-category trends (line chart)
- Selectable date range

### 6.4 Reconciliation Status

Overview of reconciliation health:
- Reconciled vs. unreconciled transaction counts for the selected month
- Statement line counts by status (matched, added, skipped, pending)
- List of pending statement lines (date, description, amount, account)
- List of unreconciled transactions (date, merchant, amount, payment method)

---

## 7. Payment Method Shortcuts

To minimize typing, the bot recognizes short aliases for payment methods:

| Alias | Payment Method |
|-------|---------------|
| `sapphire`, `sap`, `visa` | BCP Visa Infinite Sapphire |
| `amex`, `platinum` | BCP Amex Platinum |
| `yape` | Yape (settles to BCP soles current account) |
| `transfer`, `bcp` | BCP direct transfer (soles) |
| `usd`, `dollars` | BCP direct transfer (dollars) |
| `cash`, `efectivo` | Cash (no reconciliation) |

The agent recognizes these in any position within the message and is case-insensitive.

---

## Features NOT Included

The following are explicitly out of scope:
- Income and credit tracking (salary, reimbursements — statement credits are skipped during reconciliation)
- Budget setting and tracking
- Automated spending alerts or notifications
- Multi-user support
- FX conversion between PEN and USD
- Recurring transaction detection
- Bank API integration
- Additional banks (Interbank, BBVA, Cencosud Scotiabank) — BCP only for now
