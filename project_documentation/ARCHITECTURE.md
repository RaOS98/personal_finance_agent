# Architecture

## Overview

The system is fully serverless on AWS. A Telegram webhook triggers a Lambda function that drives all bot logic. Amazon Bedrock provides AI capabilities. DynamoDB stores all structured data. S3 stores receipt images.

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│   Telegram   │────▶│  API Gateway     │────▶│    Lambda    │
│   (input)    │◀────│  HTTP API        │     │  pfa-bot     │
└─────────────┘     └──────────────────┘     └──────┬───────┘
                                                     │
                    ┌──────────────────┐              │ boto3
                    │    Streamlit     │       ┌──────▼────────────┐
                    │  (local dash)    │◀──────│    DynamoDB       │
                    └──────────────────┘       │  finance-agent    │
                                               └──────┬────────────┘
                    ┌──────────────────┐              │
                    │  Amazon Bedrock  │◀─────────────┘
                    │  Claude Sonnet   │         S3: receipt images
                    │  Claude Haiku    │
                    └──────────────────┘
```

## Components

### 1. Telegram Bot (Lambda)

The entry point for all user interaction. Built with `python-telegram-bot` in webhook mode (no polling).

**Entry point:** `lambda_handler.py`
- Verifies the `X-Telegram-Bot-Api-Secret-Token` header on every request
- Deserializes the Telegram `Update` object and dispatches to registered handlers
- Holds the PTB `Application` and an `asyncio` event loop in module-level globals across warm invocations
- Always returns HTTP 200 to Telegram to prevent retry storms

**Handler layer:** `bot/handlers.py`
- Stateless handler functions; user conversation state is loaded from DynamoDB on entry and saved on exit via a `try/finally` block
- Uploads receipt images to S3 as soon as a photo arrives (tmp key); finalizes to a permanent key on confirm, deletes on cancel

**Not responsible for:**
- Any business logic, extraction, or categorization
- Direct DynamoDB writes (delegates to `db/dynamo.py`)

### 2. AI Agent (Amazon Bedrock)

All LLM calls go through the Bedrock Converse API. System prompts use `cachePoint` blocks for prompt caching.

| Task | Model | File |
|------|-------|------|
| Transaction extraction (vision) | Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6-20250929-v1:0`) | `agent/extractor.py` |
| Transaction categorization | Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) | `agent/categorizer.py` |
| Reconciliation matching (batched) | Claude Haiku 4.5 | `agent/reconciler.py` |
| Statement parsing | Python only (`pdfplumber`) | `agent/statement_parser.py` |

**Reconciliation is batched:** `reconciler.evaluate_matches(statement_line, candidates)` sends all candidates for a given statement line in a single LLM call, returning a list of verdicts. This minimizes Bedrock invocations during monthly reconciliation.

### 3. DynamoDB (Single-Table Design)

All operational data lives in one table (`finance-agent`). See [DATA_MODEL.md](DATA_MODEL.md) for the full schema.

Key access patterns:
- Fetch all transactions (`PK = TXN`)
- Find reconciliation candidates by amount + account (`GSI1PK = AMT#{account_id}#{cents}`)
- Find transactions by reconciliation status (`GSI2PK = STATUS#TXN#{status}`)
- Statement lines per account and billing period (`PK = STMT#{account_id}#{period}`)
- User bot state with auto-expiry (`PK = STATE#{user_id}`, TTL attribute)
- Reference data: categories, accounts, payment methods (`PK = REF`)

### 4. S3 (Receipt Images)

Receipt images follow a two-phase lifecycle:
1. **Tmp upload**: `receipts/tmp/{user_id}.jpg` — written immediately when a photo arrives
2. **Finalize**: copied to `receipts/{yyyy}/{mm}/txn_{id}.jpg` on transaction confirm; tmp object deleted
3. On cancel, the tmp object is deleted

Lifecycle rules:
- Objects transition to Standard-IA after 30 days
- `receipts/tmp/` objects expire after 1 day (safety net for abandoned flows)

### 5. Streamlit Dashboard (Local)

A local web application for visualizing financial data. Reads directly from DynamoDB via `dashboard/dynamo_reader.py`. Requires AWS credentials in the environment.

```bash
export AWS_PROFILE=your-profile AWS_REGION=us-east-1
streamlit run dashboard/app.py
```

**Views:**
- Monthly Summary: KPI metrics, spending by category and payment method
- Category Breakdown: transaction-level drill-down with filters
- Trends: monthly totals and per-category trends over a configurable date range
- Reconciliation Status: matched/unmatched counts, pending statement lines, unreconciled transactions

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Runtime | AWS Lambda (arm64/Graviton, 512 MB, 30 s timeout) |
| API | AWS API Gateway HTTP API |
| Telegram bot | `python-telegram-bot` 21.x (webhook mode) |
| AI models | Amazon Bedrock — Claude Sonnet 4.6, Claude Haiku 4.5 |
| Database | DynamoDB (on-demand billing, PITR enabled) |
| Image storage | S3 (versioned, private) |
| Secrets | AWS SSM Parameter Store |
| PDF parsing | `pdfplumber` |
| Dashboard | Streamlit + pandas + plotly |
| IaC | AWS SAM (`template.yaml`) |

## Project Structure

```
personal-finance-agent/
├── bot/
│   ├── handlers.py          # Telegram message handlers
│   └── keyboards.py         # Inline keyboards for confirmations
├── agent/
│   ├── extractor.py         # Image/text → raw transaction fields (Bedrock)
│   ├── categorizer.py       # Raw fields → categorized transaction (Bedrock)
│   ├── reconciler.py        # Batched match evaluation (Bedrock)
│   └── statement_parser.py  # PDF → structured statement lines (pdfplumber)
├── db/
│   ├── dynamo.py            # DynamoDB query functions
│   └── seed_dynamo.py       # One-shot reference data seeder
├── dashboard/
│   ├── app.py               # Streamlit application
│   ├── dynamo_reader.py     # DynamoDB-backed query layer for the dashboard
│   └── requirements.txt     # Dashboard-only dependencies
├── config.py                # Environment variable bindings
├── lambda_handler.py        # Lambda entrypoint
├── s3_store.py              # S3 receipt image helpers
├── requirements.txt         # Lambda runtime dependencies
└── template.yaml            # AWS SAM stack definition
```

## Data Flow

### Transaction Capture

```
User sends photo + message
        │
        ▼
  API Gateway → Lambda
        │
        ▼
  bot/handlers.py: load user state from DynamoDB
        │
        ├── Upload image to S3 (receipts/tmp/{user_id}.jpg)
        │
        ▼
  agent/extractor.py
  Bedrock Converse (Claude Sonnet 4.6)
  image → merchant, amount, date, currency
        │
        ▼
  agent/categorizer.py
  Bedrock Converse (Claude Haiku 4.5)
  merchant + hint → category slug
        │
        ▼
  Bot sends confirmation summary to user
        │
        ▼
  User confirms
        │
        ├── db/dynamo.py: save transaction to DynamoDB
        ├── s3_store.py: finalize image to receipts/{yyyy}/{mm}/txn_{id}.jpg
        └── Save user state to DynamoDB
```

### Monthly Reconciliation

```
User uploads bank statement PDF + account name
        │
        ▼
  agent/statement_parser.py (pdfplumber)
  PDF → list of {date, description, amount}
        │
        ▼
  db/dynamo.py: save statement lines (idempotent via condition expression)
        │
        ▼
  For each pending statement line:
        │
        ├── db/dynamo.py: find candidates via GSI1
        │   (same account, same amount_cents, date ± tolerance)
        │
        ├── [no candidates] → mark "pending", notify user
        │
        └── [candidates found]
                │
                ▼
          agent/reconciler.py
          Bedrock Converse (Claude Haiku 4.5) — ONE call per statement line
          evaluates all candidates at once
                │
                ▼
          Verdict per candidate: confident / likely / uncertain
                │
                ├── CONFIDENT → auto-confirm match
                ├── LIKELY    → send to user for quick yes/no
                └── UNCERTAIN → send to user with full context
```

## Key Design Decisions

**Webhook over polling.** Lambda is invoked only when Telegram sends an update. No idle compute cost.

**PTB Application held in Lambda globals.** The `Application` object and `asyncio` event loop are created once on cold start and reused across warm invocations, avoiding per-request initialization overhead.

**DynamoDB single-table.** All entities in one table simplifies IAM, billing, and backup. Two GSIs cover the two non-primary access patterns (reconciliation candidate lookup by amount, status-based queries).

**Batched reconciliation.** All candidates for a statement line are evaluated in a single Bedrock call. This reduces per-statement latency and Bedrock token costs versus the former one-call-per-candidate approach.

**Bedrock prompt caching.** System prompts for all three LLM tasks use `cachePoint` blocks. On repeated invocations within the cache TTL (5 minutes), the system prompt tokens are served from cache at ~10% of the normal input token cost.

**Content-hash IDs for statement lines.** Statement lines use a deterministic `blake2b(6)` hash of `(account_id, billing_period, date, description, amount_cents)` as their ID. This makes re-importing the same statement idempotent without a separate unique index.

**S3 tmp→final copy pattern.** Images are staged at a short-lived tmp key before the transaction ID is known. On confirmation, they are copied to the final key and the tmp object is deleted. A 1-day lifecycle rule on `receipts/tmp/` cleans up any orphaned uploads.
