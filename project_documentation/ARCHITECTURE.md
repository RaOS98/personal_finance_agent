# Architecture

## Overview

The system is fully serverless on AWS. A Telegram webhook triggers a Lambda function that drives all bot logic. Amazon Bedrock provides AI capabilities. DynamoDB stores all structured data. S3 stores receipt images and bank statement PDFs. A local Streamlit dashboard reads and writes the same DynamoDB table for analytics, edits, statement uploads, and reconciliation review.

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│   Telegram   │────▶│  API Gateway     │────▶│    Lambda    │
│   (input)    │◀────│  HTTP API        │     │  pfa-bot     │
└─────────────┘     └──────────────────┘     └──────┬───────┘
                                                     │
                    ┌──────────────────┐              │ boto3
                    │    Streamlit     │       ┌──────▼────────────┐
                    │  (local dash;    │◀─────▶│    DynamoDB       │
                    │  read + write)   │       │  finance-agent    │
                    └──────────────────┘       └──────┬────────────┘
                                                      │
                    ┌──────────────────┐              │
                    │  Amazon Bedrock  │◀─────────────┘
                    │  Claude Sonnet   │       S3: receipts/  + statements/
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

**Auto-match loop is shared.** `agent/reconciliation.py` wraps the loop body — pull pending lines, find candidates, call `evaluate_matches`, persist confident singletons (with a date-diff tiebreaker for ties) — and exposes it to both the bot (`bot/handlers.run_reconciliation`) and the Streamlit dashboard (`page_upload_statement`). It accepts an optional `progress_callback` so the dashboard can drive `st.progress` while the bot ignores it.

**Query is tool-using:** `query_agent.answer_query(text)` runs a bounded (≤4 iteration) tool-use loop with four DynamoDB primitives (`list_recent_transactions`, `query_transactions`, `aggregate_by_category`, `get_today`). The final response is a JSON envelope `{answer, source_txn_ids}` so the bot can render a citation trailer without a second LLM call.

### 3. DynamoDB (Single-Table Design)

All operational data lives in one table (`finance-agent`). See [DATA_MODEL.md](DATA_MODEL.md) for the full schema.

Key access patterns:
- Fetch all transactions (`PK = TXN`)
- Find reconciliation candidates by amount + account (`GSI1PK = AMT#{account_id}#{cents}`)
- Find transactions by reconciliation status (`GSI2PK = STATUS#TXN#{status}`)
- Look up a statement line by id in O(1) (`GSI3PK = LINE#{line_id}`)
- Statement lines per account and billing period (`PK = STMT#{account_id}#{period}`)
- User bot state with auto-expiry (`PK = STATE#{user_id}`, TTL attribute)
- Reference data: categories, accounts, payment methods (`PK = REF`)

### 4. S3 (Receipt Images and Statement PDFs)

The receipts bucket stores two kinds of binary artifacts.

**Receipt images** follow a two-phase lifecycle:
1. **Tmp upload**: `receipts/tmp/{user_id}.jpg` — written immediately when a photo arrives
2. **Finalize**: copied to `receipts/{yyyy}/{mm}/txn_{id}.jpg` on transaction confirm; tmp object deleted
3. On cancel, the tmp object is deleted

**Bank statement PDFs** are uploaded once and referenced from each line they spawned (`pdf_s3_key` on every STMT row), so the dashboard can render a presigned link back to the original document. The PDF is uploaded *before* parsing, so a parse failure still leaves a debug artifact in S3:
- Path: `statements/{account_id}/{billing_period}/{uuid}.pdf`
- Helpers: `s3_store.upload_statement_pdf(...)` and `s3_store.statement_pdf_url(...)`

Lifecycle rules (apply to the whole bucket):
- Objects transition to Standard-IA after 30 days
- `receipts/tmp/` objects expire after 1 day (safety net for abandoned flows)

### 5. Streamlit Dashboard (Local)

A local web application for visualizing and editing financial data. Reads from DynamoDB via `dashboard/dynamo_reader.py`; writes via `dashboard/dynamo_writer.py`, which calls `st.cache_data.clear()` after every mutation so subsequent reruns see fresh data. Requires AWS credentials in the environment.

```bash
export AWS_PROFILE=your-profile AWS_REGION=us-east-1
# Optional: configure the password gate first
cp .streamlit/secrets.toml.example .streamlit/secrets.toml  # then edit the password
streamlit run dashboard/app.py
```

**Auth gate:** if `dashboard_password` is set in `.streamlit/secrets.toml`, the app halts at a sign-in form until the user enters the matching password. With no password configured the gate is open (single-user trusted machine).

**Views:**
- **Monthly Summary**: KPI metrics, spending by category and payment method
- **Transactions**: filter by date / category / payment method / status / search; inline edit via `st.data_editor` (merchant, description, amount, date, category, payment method); receipt thumbnail expander
- **Category Breakdown**: transaction-level drill-down with filters
- **Trends**: monthly totals and per-category trends over a configurable date range
- **Upload Statement**: PDF upload, parse preview (editable `st.data_editor`), commit via `dynamo_writer.commit_statement`, optional auto-reconcile button driving a progress bar
- **Manual Reconciliation**: per-line candidate review with widen-search controls (date / amount-tolerance / cross-account toggle), "Ask the agent" button, match/skip/add-as-new actions, reconciled-this-period table with unmatch, and a "start from a transaction" helper
- **Reconciliation Status**: matched/unmatched counts, pending statement lines, unreconciled transactions

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
│   ├── reconciliation.py    # Shared auto-match loop used by bot + dashboard
│   └── statement_parser.py  # PDF → structured statement lines (pdfplumber)
├── db/
│   ├── dynamo.py            # DynamoDB query and write functions
│   └── seed_dynamo.py       # One-shot reference data seeder
├── tests/
│   ├── test_intent_classifier.py  # Mocked-Bedrock unit tests for the router
│   ├── test_query_agent.py        # Mocked-Bedrock tests for the tool-use loop
│   └── test_tx_editor.py          # Mocked-Bedrock tests for the edit parser
├── dashboard/
│   ├── app.py               # Streamlit application (auth gate + 7 pages)
│   ├── dynamo_reader.py     # Cached read layer for the dashboard
│   ├── dynamo_writer.py     # Write layer that invalidates the read cache
│   └── requirements.txt     # Dashboard-only dependencies
├── .streamlit/
│   └── secrets.toml.example # Template for the dashboard password gate
├── project_documentation/
│   ├── ARCHITECTURE.md      # This file
│   ├── FEATURES.md          # User-facing feature catalog
│   ├── AGENT_SPEC.md        # AI agent contracts and prompts
│   └── DATA_MODEL.md        # DynamoDB schema reference
├── config.py                # Environment variable bindings
├── lambda_handler.py        # Lambda entrypoint
├── s3_store.py              # S3 helpers (receipt images + statement PDFs)
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

Statements can be ingested through two front-doors. Both end up in the same auto-match loop and the same manual-review surface.

```
                   ┌──────────────────────────┐
                   │  Telegram bot upload     │   bot/handlers.py
                   │  (PDF document message)  │
                   └────────────┬─────────────┘
                                │
                   ┌────────────┴─────────────┐
                   │ Streamlit dashboard      │   dashboard/app.py
                   │ (Upload Statement page)  │   page_upload_statement
                   └────────────┬─────────────┘
                                │
                                ▼
        s3_store.upload_statement_pdf  → statements/{acct}/{period}/{uuid}.pdf
                                │
                                ▼
        agent/statement_parser.py (pdfplumber) → list of signed-amount lines
                                │
                                ▼
        db.save_statement_lines (idempotent, persists pdf_s3_key on every line)
                                │
                                ▼
        agent/reconciliation.auto_reconcile(billing_period, account_id)
                                │
                                ▼
   For each pending statement line:
        │
        ├── db.find_reconciliation_candidates  (GSI1: AMT#{account}#{cents}, ±date)
        │
        ├── [no candidates]      → leave line pending
        │
        ├── [candidates found]
        │       ▼
        │   agent/reconciler.evaluate_matches  (one batched Bedrock call)
        │   verdicts: confident / likely / uncertain
        │       ▼
        │   pick_auto_match: confident singleton, or smallest |Δdate|
        │       │
        │       ├── auto-pick     → db.create_reconciliation
        │       └── ambiguous     → leave pending for manual review
        │
        ▼
   Manual review: dashboard "Manual Reconciliation" page
        │
        ├── widen-search controls (date / amount tolerance / cross-account)
        ├── "Ask the agent"  → re-runs reconciler.evaluate_matches with the
        │                      currently-displayed candidate set
        ├── match / skip / add-as-new buttons → dashboard/dynamo_writer.py
        └── reconciled-this-period table with one-click "Unmatch"
```

Both entry points share `agent/reconciliation.auto_reconcile`, which means the bot and the dashboard always agree on what counts as a confident match. The dashboard passes a `progress_callback`; the bot passes `None`.

## Key Design Decisions

**Webhook over polling.** Lambda is invoked only when Telegram sends an update. No idle compute cost.

**PTB Application held in Lambda globals.** The `Application` object and `asyncio` event loop are created once on cold start and reused across warm invocations, avoiding per-request initialization overhead.

**DynamoDB single-table.** All entities in one table simplifies IAM, billing, and backup. Three GSIs cover the non-primary access patterns: `GSI1` for reconciliation candidate lookup by amount, `GSI2` for status-based queries, and `GSI3` for O(1) statement-line lookup by id. `db.dynamo._get_statement_line` falls back to a table scan if `GSI3PK` is absent so legacy items keep working.

**Batched reconciliation.** All candidates for a statement line are evaluated in a single Bedrock call. This reduces per-statement latency and Bedrock token costs versus the former one-call-per-candidate approach.

**Shared auto-reconciliation loop.** `agent/reconciliation.py` is the single source of truth for the auto-match decision. Both the bot and the dashboard call `auto_reconcile`, so a confident match in one path is a confident match in the other. The dashboard supplies a `progress_callback` for the UI progress bar.

**Signed-amount model for statement lines.** Statement-line `amount` is stored as a signed integer (positive = debit/purchase, negative = credit/refund). `_parse_amount` honours `-`, `( … )`, `CR`, and `ABONO` markers, and the reconciler prompt is taught to match same-sign candidates only. This avoids the previous "skip negative lines" hack that lost refunds and credits.

**Dashboard write cache invalidation.** `dashboard/dynamo_writer.py` is the only path through which the Streamlit app mutates DynamoDB. Every public function calls `st.cache_data.clear()` after a successful write so subsequent reads (which are wrapped in `@st.cache_data`) see fresh data without the user having to manually rerun.

**Dashboard auth gate.** The dashboard reads `st.secrets["dashboard_password"]`; if set, an unauthenticated session sees only a sign-in form and `st.stop()` is called until the password matches. The secrets file is gitignored and a `.streamlit/secrets.toml.example` template is checked in.

**Bedrock prompt caching.** System prompts for all three LLM tasks use `cachePoint` blocks. On repeated invocations within the cache TTL (5 minutes), the system prompt tokens are served from cache at ~10% of the normal input token cost.

**Content-hash IDs for statement lines.** Statement lines use a deterministic `blake2b(6)` hash of `(account_id, billing_period, date, description, amount_cents)` as their ID. This makes re-importing the same statement idempotent without a separate unique index.

**S3 tmp→final copy pattern.** Images are staged at a short-lived tmp key before the transaction ID is known. On confirmation, they are copied to the final key and the tmp object is deleted. A 1-day lifecycle rule on `receipts/tmp/` cleans up any orphaned uploads.

**Intent classifier fails open.** If the classifier errors or returns an unrecognized intent, the message is treated as `new_transaction`. Rationale: missing a new transaction silently loses user data; misclassifying a query or edit only prompts the user to rephrase. Fail toward the costliest-to-skip intent.

**Edit-key rewrite uses TransactWriteItems.** A transaction's primary SK and `GSI1PK` both encode `amount` and `date`. When the user edits either field, `update_transaction_fields` deletes the old item and puts the new one in a single atomic transaction (via the low-level `TransactWriteItems` API and `boto3.dynamodb.types.TypeSerializer`). All non-key fields (`id`, `created_at`, `reconciliation_status`, `telegram_image_id`, `image_path`) are preserved verbatim. Other field edits use plain `UpdateItem`.

**Query agent commits to citations.** The query agent's final response is a JSON envelope `{answer, source_txn_ids}`. Forcing the model to enumerate the transaction ids that back its answer makes hallucinations cheaper to detect and gives the user a self-serve verification path.
