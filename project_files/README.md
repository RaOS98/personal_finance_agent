# Personal Finance Agent

A Telegram-based AI agent for tracking personal finances in Peru, where open banking is not available. The agent captures transactions via photos and text messages, categorizes spending, stores structured data, reconciles against monthly bank statements, and provides a live dashboard for financial visibility.

## The Problem

Most Peruvian banks lack APIs for downloading transaction data. There is no way to filter by date range — only the most recent transactions are visible in banking apps. This makes it nearly impossible to maintain a clear picture of personal spending, which leads to poor financial habits and difficulty saving.

## The Solution

An AI-powered agent that runs on Telegram. The user sends photos of receipts, transfer screenshots, or Yape/Plin confirmations along with a brief message indicating the payment method and category. The agent extracts transaction details, confirms them with the user, and stores everything in a structured database. At the end of each month, bank statement PDFs are uploaded and the agent reconciles logged transactions against them.

## How It Works

1. **Capture** — Send a photo or text message to the Telegram bot with basic context (e.g., "Sapphire, groceries").
2. **Extract** — The agent reads the image and extracts merchant name, amount, date, and currency.
3. **Confirm** — The agent replies with a structured summary for the user to approve or correct.
4. **Store** — Confirmed transactions are saved to a PostgreSQL database.
5. **Reconcile** — Monthly bank statement PDFs are uploaded. Python pre-filters by amount and date to find candidates, then the agent evaluates matches semantically. Matches are triaged into three tiers: Confident (auto-confirmed), Likely (quick user confirmation), and Uncertain (user review with context).
6. **Visualize** — A Streamlit dashboard provides real-time visibility into spending by category, payment method, and time period.

## MVP Scope

- **Model**: Gemma4 31B running locally via Ollama
- **Accounts**: BCP only (Visa Infinite Sapphire, Amex Platinum, soles current account, dollars current account)
- **Input**: Telegram (photos and text messages)
- **Database**: PostgreSQL
- **Dashboard**: Streamlit (local)
- **Reconciliation**: Monthly, against BCP statement PDFs

## Transaction Categories

| # | Category | Examples |
|---|----------|----------|
| 1 | Food & Dining | Restaurants, delivery apps, cafés |
| 2 | Groceries | Supermarkets, markets, bodegas |
| 3 | Transportation | Fuel, taxis, Uber, parking, tolls |
| 4 | Housing | Rent, maintenance, repairs, home services |
| 5 | Utilities | Electricity, water, gas, internet, phone |
| 6 | Health | Pharmacy, doctors, gym, insurance |
| 7 | Entertainment | Streaming, outings, events, hobbies |
| 8 | Shopping | Clothing, electronics, household items |
| 9 | Education | Courses, books, learning subscriptions |
| 10 | Other | Catch-all with mandatory note |

## Payment Methods (MVP)

| Payment Method | Settlement Account | Statement Source |
|----------------|-------------------|-----------------|
| BCP Visa Infinite Sapphire | Sapphire credit card | Sapphire statement |
| BCP Amex Platinum | Amex credit card | Amex statement |
| Yape | BCP soles current account | BCP soles statement |
| BCP transfer / direct debit | BCP soles current account | BCP soles statement |
| BCP transfer (USD) | BCP dollars current account | BCP dollars statement |
| Cash | None (no reconciliation) | — |

## Design Principles

- **Simplicity first** — The simplest system that delivers financial visibility.
- **Correct data over convenience** — Every transaction is confirmed before storage. Small friction is acceptable.
- **Currency-aware from day one** — All transactions record original currency (PEN or USD). FX conversion is a future concern.
- **Capture ≠ categorization** — Extraction and categorization are separate steps for reliability and debuggability.
- **Payment method ≠ settlement account** — Yape is a payment method; BCP soles current account is where it settles. Both are tracked.
- **Smart reconciliation** — No hard-coded matching rules. Python pre-filters by amount and date; the agent handles semantic matching with a three-tier verdict (Confident / Likely / Uncertain).

## Future Scope (Post-MVP)

- Cloud deployment (AWS or GCP)
- Additional banks and cards (Interbank, BBVA, Cencosud Scotiabank or replacement)
- Automated spending insights and alerts
- Budget tracking and goals

## Project Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System components, tech stack, and how they connect |
| [FEATURES.md](FEATURES.md) | Detailed feature specifications and user flows |
| [DATA_MODEL.md](DATA_MODEL.md) | Database schema and data structures |
| [AGENT_SPEC.md](AGENT_SPEC.md) | Agent behavior, prompts, and decision logic |
