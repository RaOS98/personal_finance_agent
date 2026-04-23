# Features

## Overview

This document describes the features of the Personal Finance Agent, organized by user flow. Each feature includes what it does, how the user interacts with it, and the expected behavior.

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

## 3. Monthly Reconciliation

At the end of each billing cycle, the user uploads bank statement PDFs for each active account/card. The system matches logged transactions against statement lines.

### 3.1 Statement Upload

**User sends:** A PDF file to the Telegram bot with a message identifying the account (e.g., "Sapphire statement April").

**Agent does:**
1. Parses the PDF with `pdfplumber` to extract individual statement lines (date, description, amount)
2. Identifies the account from the user's message
3. Stores the statement lines in DynamoDB (idempotent — re-uploading the same statement is safe)

### 3.2 Matching Process

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

### 3.3 Reconciliation Review via Telegram

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

### 3.4 Reconciliation Outcomes

After reconciliation, every statement line has one of these statuses:
- **Matched** — linked to a logged transaction (auto or user-confirmed)
- **Added** — no prior record existed; user added it during reconciliation
- **Skipped** — user chose to skip (e.g., bank fees they don't want to track)
- **Pending** — not yet reviewed

Every logged transaction has one of these statuses:
- **Reconciled** — matched to a statement line
- **Unreconciled** — not yet matched (either no statement uploaded yet or no matching line found)

---

## 4. Dashboard

A local Streamlit application providing real-time visibility into personal finances. Reads from DynamoDB — requires AWS credentials in the environment.

```bash
export AWS_PROFILE=your-profile AWS_REGION=us-east-1
streamlit run dashboard/app.py
```

### 4.1 Monthly Summary

The default view. Shows spending for a selected month:
- Total spent (PEN and USD shown separately)
- Comparison to the previous month
- Number of transactions
- Spending by category (horizontal bar chart)
- Spending by payment method (horizontal bar chart)

### 4.2 Category Breakdown

Drill down into any category to see individual transactions. Filterable by date range and payment method.

### 4.3 Trends

Monthly spending over time:
- Total spending per month (line chart, by currency)
- Per-category trends (line chart)
- Selectable date range

### 4.4 Reconciliation Status

Overview of reconciliation health:
- Reconciled vs. unreconciled transaction counts for the selected month
- Statement line counts by status (matched, added, skipped, pending)
- List of pending statement lines (date, description, amount, account)
- List of unreconciled transactions (date, merchant, amount, payment method)

---

## 5. Payment Method Shortcuts

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
