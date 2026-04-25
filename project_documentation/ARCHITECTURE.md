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
| Intent classification (route inbound text) | Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) | `agent/intent_classifier.py` |
| Transaction extraction (vision) | Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6-20250929-v1:0`) | `agent/extractor.py` |
| Transaction categorization | Claude Haiku 4.5 | `agent/categorizer.py` |
| Edit-request parsing | Claude Haiku 4.5 | `agent/tx_editor.py` |
| Query answering (tool-use over DynamoDB) | Claude Haiku 4.5 | `agent/query_agent.py` |
| Reconciliation matching (batched) | Claude Haiku 4.5 | `agent/reconciler.py` |
| Statement parsing | Python only (`pdfplumber`) | `agent/statement_parser.py` |

**Reconciliation is batched:** `reconciler.evaluate_matches(statement_line, candidates)` sends all candidates for a given statement line in a single LLM call, returning a list of verdicts. This minimizes Bedrock invocations during monthly reconciliation.

**Query is tool-using:** `query_agent.answer_query(text)` runs a bounded (≤4 iteration) tool-use loop with four DynamoDB primitives (`list_recent_transactions`, `query_transactions`, `aggregate_by_category`, `get_today`). The final response is a JSON envelope `{answer, source_txn_ids}` so the bot can render a citation trailer without a second LLM call.

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
│   ├── intent_classifier.py # Free-form text → new_transaction | edit | query (Bedrock)
│   ├── extractor.py         # Image/text → raw transaction fields (Bedrock)
│   ├── categorizer.py       # Raw fields → categorized transaction (Bedrock)
│   ├── tx_editor.py         # Natural-language edit request → field + new value (Bedrock)
│   ├── query_agent.py       # Tool-use loop over DynamoDB to answer questions (Bedrock)
│   ├── reconciler.py        # Batched match evaluation (Bedrock)
│   └── statement_parser.py  # PDF → structured statement lines (pdfplumber)
├── db/
│   ├── dynamo.py            # DynamoDB query functions
│   └── seed_dynamo.py       # One-shot reference data seeder
├── tests/
│   └── test_intent_classifier.py  # Mocked-Bedrock unit tests for the router
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

### Intent Routing

Free-form text messages enter a routing layer at the top of `_handle_message_inner`. Photos, captioned photos, and messages received while the user is mid-flow (any non-empty `state.state`) bypass the classifier and fall through to existing handlers.

```
User sends text message
        │
        ▼
  bot/handlers.py: load user state
        │
        ├── state.state non-empty?  → existing state-machine handler
        ├── photo present?           → existing new-transaction flow
        │
        ▼
  Strip "!" or "?" prefix → forced intent (edit / query)
        │
        ├── if no prefix → agent/intent_classifier.py (Claude Haiku 4.5)
        │                  returns {"intent": "...", "confident": ...}
        │
        ▼
  Dispatch on intent:
        │
        ├── new_transaction → existing extractor + categorizer flow
        ├── edit            → _handle_edit_intent
        └── query           → _handle_query_intent
```

The classifier fails open: any error or unparseable response defaults to `new_transaction`. Missing a new transaction silently loses data; misclassifying a query/edit just prompts the user to rephrase.

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

### Edit (existing transaction)

```
User sends "change amount to 25" (or "!change ...")
        │
        ▼
  intent_classifier → "edit"
        │
        ▼
  db.list_recent_transactions(limit=1) → most recent txn
        │
        ▼
  agent/tx_editor.py (Claude Haiku 4.5)
  text + target → {field, new_value, confident}
        │
        ▼
  Validate field-specific constraints (numeric amount, ISO date,
  known category slug, resolvable payment method alias)
        │
        ▼
  Bot replies with old → new diff + yes/no keyboard
  state.state = "awaiting_edit_target_confirm"
        │
        ▼
  User presses ✅ Yes
        │
        ▼
  db.update_transaction_fields(txn_id, {field: new_value})
        │
        ├── amount/date changed → TransactWriteItems(Delete old SK + Put new SK)
        │                          GSI1PK and SK rewritten atomically
        └── other fields        → plain UpdateItem on existing key
```

### Query

```
User sends "how much did I spend on food this month?"
        │
        ▼
  intent_classifier → "query"
        │
        ▼
  agent/query_agent.py
  Bedrock Converse (Claude Haiku 4.5) with toolConfig
        │
        ▼
  Tool-use loop (≤4 iterations):
        │
        ├── get_today()
        ├── query_transactions(date_from, date_to, category_slug?, ...)
        ├── aggregate_by_category(date_from, date_to)
        └── list_recent_transactions(limit)
        │
        ▼
  Final assistant message: {"answer": "...", "source_txn_ids": [...]}
        │
        ▼
  Bot reply: <answer> + "Based on: #1, #5, #12"
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

**Intent classifier fails open.** If the classifier errors or returns an unrecognized intent, the message is treated as `new_transaction`. Rationale: missing a new transaction silently loses user data; misclassifying a query or edit only prompts the user to rephrase. Fail toward the costliest-to-skip intent.

**Edit-key rewrite uses TransactWriteItems.** A transaction's primary SK and `GSI1PK` both encode `amount` and `date`. When the user edits either field, `update_transaction_fields` deletes the old item and puts the new one in a single atomic transaction (via the low-level `TransactWriteItems` API and `boto3.dynamodb.types.TypeSerializer`). All non-key fields (`id`, `created_at`, `reconciliation_status`, `telegram_image_id`, `image_path`) are preserved verbatim. Other field edits use plain `UpdateItem`.

**Query agent commits to citations.** The query agent's final response is a JSON envelope `{answer, source_txn_ids}`. Forcing the model to enumerate the transaction ids that back its answer makes hallucinations cheaper to detect and gives the user a self-serve verification path.
