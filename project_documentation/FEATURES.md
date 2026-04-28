# Features

## Overview

This document describes the features of the Personal Finance Agent, organized by user flow. Each feature includes what it does, how the user interacts with it, and the expected behavior.

---

## 0. Intent-Based Routing

Free-form text messages are classified into one of two intents at the top of the message handler. The classifier (Claude Haiku 4.5) reads the message and returns one of:

- `new_transaction` — log a new spend (default), or any message that is not clearly an edit of a prior transaction
- `edit` — modify a previously logged transaction

**Bypass conditions** (the classifier is skipped):
- The message contains a photo — always treated as a new transaction.
- The user is mid-flow (e.g., the bot is awaiting a missing field or category selection) — the existing state-machine handler runs instead.

**Escape hatch** (override the classifier):
- Prefix the message with `!` to force the edit flow (e.g., `!change merchant to Pedro`).

**Failure mode:** The classifier fails open. Any error or unrecognized response defaults to `new_transaction`. The reasoning: missing a new transaction silently loses user data; misclassifying an edit just prompts a rephrase.

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

## 4. Periodic spending insights (Telegram)

The stack can run a **scheduled Lambda** (EventBridge) that pushes a short, **deterministic** digest to the same Telegram chat as the bot. There is **no LLM** on this path: numbers come straight from DynamoDB aggregation (same logic as the widget summary plus a seven-day category rollup).

### 4.1 What the user sees

A typical weekly message includes:

- **Week of** (Monday date in the configured timezone)
- **Month-to-date** PEN (and USD if non-zero) totals, **today** PEN
- **Top spending categories** (PEN) for the month and for the last seven days
- **Unreconciled** transaction count (nudge to reconcile card spend)
- **Transaction count** logged in the current month

If there are no transactions in the current month, the Lambda skips sending (nothing to report).

### 4.2 Deduping

After a successful send, the function stores a week key on `STATE#<telegram_user_id>` / `INSIGHT_LAST` in DynamoDB so EventBridge retries do not duplicate the same digest.

### 4.3 Configuration

- `ALLOWED_USER_ID` — chat id (same as the bot gate)
- `INSIGHTS_TIMEZONE` — IANA zone for "today" (default `America/Lima`)
- Schedule — see `template.yaml` (default: weekly Monday 12:00 UTC; adjust to taste)

---

## 5. Monthly Reconciliation

At the end of each billing cycle, the user uploads bank statement PDFs for each active account/card. The system auto-matches what it can confidently and surfaces the rest for manual review in the dashboard.

### 5.1 Statement Upload

There are two equivalent entry points; both upload the PDF to S3, parse it, and persist the lines.

**Telegram bot.** Send a PDF document to the bot. The bot extracts the account/period from the caption (or asks for them), uploads the PDF, parses it, runs auto-reconcile, and posts a summary message. The PDF is uploaded to S3 *before* parsing, so even a parse failure leaves a debug artifact behind.

**Streamlit dashboard → Upload Statement page.** Pick the account, billing period, and PDF file. The page shows an editable preview of every parsed line (`st.data_editor`), so you can fix the description or amount before committing. After "Save statement," an optional "Auto-reconcile now" button runs the same loop the bot uses, with a progress bar.

Both paths call the same `agent/reconciliation.auto_reconcile` so the matching outcome is identical regardless of where the upload originated.

### 5.2 Matching Process

Reconciliation runs in two stages:

**Stage 1 — Python pre-filter:**
- For each pending statement line, query DynamoDB via GSI1 for transactions with the same signed `amount_cents` and same settlement account within ±5 days of the statement date
- If no candidates are found, the line stays pending for manual review
- If candidates are found, all of them are passed to the agent in a single batched Bedrock call

**Stage 2 — Agent evaluation (batched):**
- `agent/reconciler.evaluate_matches` receives the statement line and all candidates at once
- It assigns a verdict to each candidate: Confident, Likely, or Uncertain (one Bedrock call per line, regardless of candidate count)

| Verdict | Meaning | Action |
|---------|---------|--------|
| **Confident** | Clearly the same transaction | Auto-matched if there is exactly one confident candidate (or one that wins the smallest-`|Δdate|` tiebreaker) |
| **Likely** | Probably the same | Left pending for the dashboard manual-review page |
| **Uncertain** | Unclear match | Left pending for the dashboard manual-review page |

Negative-amount statement lines (refunds, credits, ABONOs) are kept and matched against transactions of the same sign — the parser, the database, and the LLM prompt all use the same signed-amount convention.

### 5.3 Manual Reconciliation Review (Streamlit dashboard)

Anything not auto-matched is reviewed in the **Manual Reconciliation** page of the dashboard. The page has two columns:

**Left column — pending statement lines:**
- One expandable card per line, with date, description, signed amount, and a presigned link back to the original PDF
- "Widen search" controls per line: extend the date window, allow ±tolerance on amount, or include other accounts
- "Ask the agent" button: re-runs `evaluate_matches` against the candidates currently on screen; verdicts are rendered next to each candidate
- Per-candidate actions: **Match**, **Skip**, **Add as new transaction** (creates a transaction pre-populated from the statement line and immediately matches it)

**Right column — reconciled lines this period:**
- Every line that was matched in the selected billing period
- One-click **Unmatch** to undo a bad pairing

A footer **"Start from a transaction"** helper inverts the lookup: pick an unreconciled transaction first, then see candidate statement lines.

### 5.4 Reconciliation Outcomes

After reconciliation, every statement line has one of these statuses:
- **Matched** — linked to a logged transaction (auto or user-confirmed)
- **Added** — no prior record existed; user added it from the dashboard during review
- **Skipped** — user chose to skip (e.g., bank fees they don't want to track)
- **Pending** — not yet reviewed

Every logged transaction has one of these statuses:
- **Reconciled** — matched to a statement line
- **Unreconciled** — not yet matched (either no statement uploaded yet or no matching line found)

---

## 6. Dashboard

A local Streamlit application providing real-time visibility *and* edit access to personal finances. Reads from DynamoDB via a cached read layer (`dashboard/dynamo_reader.py`); writes go through `dashboard/dynamo_writer.py`, which automatically clears the read cache after every successful mutation. Requires AWS credentials in the environment.

```bash
export AWS_PROFILE=your-profile AWS_REGION=us-east-1
# Optional: configure the password gate
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then edit the password
streamlit run dashboard/app.py
```

### 6.0 Authentication

If `dashboard_password` is set in `.streamlit/secrets.toml`, the app shows a sign-in form before any page renders. An incorrect password keeps the user on the form; the correct password unlocks the session via `st.session_state`. If no password is configured (the default for a fresh checkout), the gate is bypassed — the dashboard is intended for a single trusted machine.

The `secrets.toml` file is gitignored; only `.streamlit/secrets.toml.example` is checked in.

### 6.1 Monthly Summary

The default view. Shows spending for a selected month:
- Total spent (PEN and USD shown separately)
- Comparison to the previous month
- Number of transactions
- Spending by category (horizontal bar chart)
- Spending by payment method (horizontal bar chart)

### 6.2 Transactions

A full transaction-list view with inline editing.

- Filters: date range, category, payment method, reconciliation status, free-text search over merchant/description
- Signed-amount column (negative for refunds and credits) with currency
- **Inline edit** via `st.data_editor` for `merchant`, `description`, `amount`, `date`, `category`, and `payment_method`. Read-only columns are `id`, `currency`, `reconciliation_status`, and `image_path`. Amount/date edits trigger the same atomic key-rewrite path used by the bot
- Receipt thumbnail expander pulls the image from S3 via a presigned URL when `image_path` is set
- Each saved edit clears the dashboard read cache automatically

### 6.3 Category Breakdown

Drill down into any category to see individual transactions. Filterable by date range and payment method.

### 6.4 Trends

Monthly spending over time:
- Total spending per month (line chart, by currency)
- Per-category trends (line chart)
- Selectable date range

### 6.5 Upload Statement

End-to-end statement ingest from the dashboard.

- File uploader for the bank statement PDF
- Account picker (from the `REF#ACCOUNT` items) and billing-period picker
- Editable parse preview (`st.data_editor`) with the parsed lines — adjust descriptions or amounts before committing
- "Save statement" persists every line via `dynamo_writer.commit_statement` (idempotent: re-uploading the same statement is safe)
- "Auto-reconcile now" button runs `agent/reconciliation.auto_reconcile` with a progress bar, then displays a summary

### 6.6 Manual Reconciliation

The interactive review surface for everything `auto_reconcile` couldn't decide on its own. See [Section 5.3](#53-manual-reconciliation-review-streamlit-dashboard) for the full behaviour.

### 6.7 Reconciliation Status

Overview of reconciliation health:
- Reconciled vs. unreconciled transaction counts for the selected month
- Statement line counts by status (matched, added, skipped, pending)
- List of pending statement lines (date, description, amount, account, link to PDF)
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
- Income tracking (salary)
- Budget setting and tracking
- Automated spending alerts or notifications
- Multi-user support (a single shared `dashboard_password` and a single Telegram user are assumed)
- FX conversion between PEN and USD
- Recurring transaction detection
- Bank API integration
- Additional banks (Interbank, BBVA, Cencosud Scotiabank) — BCP only for now

Note: refunds and statement credits *are* tracked end-to-end. Negative amounts flow through the parser, the database, and the matcher unchanged.
