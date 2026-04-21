# Architecture

## Overview

The system consists of four components connected through a PostgreSQL database: a Telegram bot that captures transactions, an AI agent that processes them, a reconciliation pipeline for monthly bank statements, and a Streamlit dashboard for visualization. Everything runs locally for the MVP.

```
┌─────────────┐     ┌─────────────────┐     ┌──────────────┐
│   Telegram   │────▶│   Python App    │────▶│  PostgreSQL   │
│   (input)    │◀────│   + Gemma4 31B  │     │  (storage)    │
└─────────────┘     └─────────────────┘     └──────┬───────┘
                                                    │
                    ┌─────────────────┐              │
                    │    Streamlit    │◀─────────────┘
                    │   (dashboard)   │
                    └─────────────────┘
```

## Components

### 1. Telegram Bot

The entry point for all user interaction. Built with the `python-telegram-bot` library.

**Responsibilities:**
- Receive photos and text messages from the user
- Forward inputs to the AI agent for processing
- Present extracted transaction summaries for user confirmation
- Handle user corrections and approvals
- Deliver reconciliation review requests during monthly reconciliation

**Not responsible for:**
- Any business logic, extraction, or categorization
- Direct database access

### 2. AI Agent

The core processing layer. Communicates with Gemma4 31B via the Ollama API (`ollama` Python library or direct HTTP calls to `localhost:11434`).

**Responsibilities:**
- **Extraction**: Read images and text to extract structured transaction data (merchant, amount, date, currency)
- **Categorization**: Assign a category based on merchant and user hint
- **Reconciliation matching**: Evaluate candidate matches between logged transactions and bank statement lines, producing a three-tier verdict (Confident / Likely / Uncertain)
- **Statement parsing**: Extract structured line items from bank statement PDFs

The agent performs extraction and categorization as two distinct steps. Extraction produces raw fields; categorization maps them to one of the 10 defined categories. This separation makes errors easier to identify and correct.

### 3. PostgreSQL Database

Single local PostgreSQL instance. Stores all transaction data, reconciliation state, and reference data (categories, payment methods, accounts).

Schema is defined in [DATA_MODEL.md](DATA_MODEL.md).

### 4. Streamlit Dashboard

A local web application for visualizing financial data. Connects directly to PostgreSQL.

**Views:**
- Spending by category (current month, historical)
- Spending by payment method
- Monthly totals and trends
- Reconciliation status (matched, unmatched, pending review)
- Unreconciled transactions

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Telegram bot | `python-telegram-bot` |
| AI model | Gemma4 31B via Ollama |
| AI interface | `ollama` Python library or HTTP (`localhost:11434`) |
| Database | PostgreSQL 16 |
| DB access | `psycopg` (v3) |
| PDF parsing | `pdfplumber` |
| Dashboard | Streamlit |

## Project Structure

```
personal-finance-agent/
├── bot/
│   ├── __init__.py
│   ├── handlers.py          # Telegram message handlers
│   └── keyboards.py         # Inline keyboards for confirmations
├── agent/
│   ├── __init__.py
│   ├── extractor.py         # Image/text → raw transaction fields
│   ├── categorizer.py       # Raw fields → categorized transaction
│   ├── reconciler.py        # Match transactions to statement lines
│   └── statement_parser.py  # PDF → structured statement lines
├── db/
│   ├── __init__.py
│   ├── models.py            # Database table definitions
│   ├── queries.py           # SQL queries
│   └── migrations/          # Schema migrations
├── dashboard/
│   └── app.py               # Streamlit application
├── storage/
│   └── images/              # Saved receipt/screenshot images (txn_{id}.jpg)
├── config.py                # Environment variables and settings
├── main.py                  # Application entry point (starts bot)
└── requirements.txt
```

## Data Flow

### Transaction Capture

```
User sends photo + message
        │
        ▼
  Telegram Bot receives message
        │
        ▼
  Agent: Extraction
  (image → merchant, amount, date, currency)
        │
        ▼
  Agent: Categorization
  (merchant + user hint → category)
        │
        ▼
  Bot sends summary to user for confirmation
        │
        ▼
  User confirms or corrects
        │
        ▼
  Transaction saved to PostgreSQL
```

### Monthly Reconciliation

```
User uploads bank statement PDF
        │
        ▼
  Agent: Statement Parsing
  (PDF → list of statement lines)
        │
        ▼
  Python pre-filter: Candidate Selection
  (match by exact amount + date proximity)
        │
        ▼
  Agent: Reconciliation Matching
  (evaluate candidates via semantic matching on merchant names)
        │
        ▼
  Three-tier verdict per match
        │
        ├── CONFIDENT → auto-confirmed
        │   (exact amount, clearly same merchant, consistent date)
        │
        ├── LIKELY → sent to user for quick confirmation
        │   (exact amount, plausible merchant name difference, date ±1-2 days)
        │
        └── UNCERTAIN → sent to user with context
            (ambiguous match, multiple candidates, or no match found)
        │
        ▼
  Reconciliation status updated in database
```

The reconciliation process is split between Python and the AI agent. Python handles the quantitative filtering (exact amount matching, date range narrowing) to produce a shortlist of candidates. The agent then handles the qualitative judgement (does "Wong Supermercados" correspond to "CENCOSUD RETAIL SA"?) and assigns a verdict. This keeps math in code and language understanding in the model.

## Key Design Decisions

**Ollama for local inference.** Gemma4 31B runs locally via Ollama, keeping all financial data on the user's machine. No external API calls for transaction processing. This also eliminates ongoing API costs during the MVP phase.

**Separation of extraction and categorization.** The agent first extracts raw data (what the receipt says), then categorizes (what it means). If categorization is wrong, the raw data is still correct and re-categorizable without re-processing the image.

**Confirmation before storage.** Every transaction requires user approval before being persisted. This adds a small interaction cost but ensures data quality from the start.

**PDF parsing with pdfplumber.** BCP statements contain selectable text, so OCR is not needed. `pdfplumber` extracts text and tables reliably from text-based PDFs.

**Stateless agent.** The agent does not maintain conversation history. Each message is processed independently. The Telegram bot manages conversation context (e.g., awaiting confirmation) through simple state tracking per user.

## Local Development Setup

1. Install and start Ollama with Gemma4 31B: `ollama pull gemma4:31b`
2. Install and start PostgreSQL 16
3. Create the database: `createdb personal_finance`
4. Install Python dependencies: `pip install -r requirements.txt`
5. Run database migrations
6. Set environment variables (Telegram bot token, database URL)
7. Start the bot: `python main.py`
8. Start the dashboard: `streamlit run dashboard/app.py`

## Future Migration Path

When moving to the cloud post-MVP:
- PostgreSQL → managed instance (AWS RDS or Google Cloud SQL)
- Ollama + Gemma4 → cloud-hosted model or API-based model
- Telegram bot → deployed on a small VM or container
- Streamlit → hosted on the same VM or a managed service
- No architectural changes required — all components communicate through the same interfaces
