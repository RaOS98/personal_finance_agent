# Personal Finance Agent

A Telegram-based AI agent for tracking personal finances in Peru, where open banking is not available. The agent captures transactions via photos and text messages, categorizes spending, stores structured data, reconciles against monthly bank statements, and provides a live dashboard for financial visibility.

## The Problem

Most Peruvian banks lack APIs for downloading transaction data. There is no way to filter by date range — only the most recent transactions are visible in banking apps. This makes it nearly impossible to maintain a clear picture of personal spending, which leads to poor financial habits and difficulty saving.

## The Solution

An AI-powered agent that runs on Telegram. The user sends photos of receipts, transfer screenshots, or Yape/Plin confirmations along with a brief message indicating the payment method and category. The agent extracts transaction details, confirms them with the user, and stores everything in DynamoDB. At the end of each month, bank statement PDFs are uploaded and the agent reconciles logged transactions against them.

## How It Works

1. **Route** — Free-form text messages are first classified into one of three intents: `new_transaction`, `edit`, or `query`. Photos always route directly to the new-transaction flow. The `!` and `?` prefixes force the edit and query flows respectively.
2. **Capture** — For new transactions, send a photo or text message to the Telegram bot with basic context (e.g., "Sapphire, groceries"). Claude Sonnet reads the image and extracts merchant name, amount, date, and currency.
3. **Confirm** — The agent replies with a structured summary for the user to approve or correct.
4. **Store** — Confirmed transactions are saved to DynamoDB; receipt images go to S3.
5. **Edit** — Send a natural-language correction (e.g., "change amount to 25") to update the most recent transaction. The bot shows a yes/no diff before any write.
6. **Query** — Ask questions in plain English or Spanish (e.g., "how much did I spend on food this month?"). The agent runs a tool-use loop over DynamoDB and replies with cited transaction ids.
7. **Reconcile** — Monthly bank statement PDFs are uploaded either through the Telegram bot or the Streamlit dashboard. Python pre-filters by signed amount and date; the agent evaluates all candidates for a statement line in one batched call. Confident singletons auto-match (with a date-diff tiebreaker for ties); everything else is reviewed in the dashboard's Manual Reconciliation page.
8. **Visualize and edit** — A local Streamlit dashboard backed by DynamoDB provides real-time visibility into spending and a writeable surface for transaction edits, statement uploads, and manual reconciliation. A simple shared-password gate via `st.secrets` keeps it out of casual hands.

## Architecture

- **Runtime**: AWS Lambda (python3.12, arm64), triggered by Telegram webhook via API Gateway HTTP API
- **AI**: Amazon Bedrock — Claude Sonnet 4.6 for vision extraction, Claude Haiku 4.5 for intent classification, categorization, edit parsing, query tool-use, and reconciliation
- **Database**: DynamoDB single-table design with three GSIs
- **Storage**: S3 for receipt images and bank statement PDFs (Standard → Standard-IA lifecycle)
- **Secrets**: AWS SSM Parameter Store (Telegram tokens), `.streamlit/secrets.toml` (dashboard password)
- **Bot state**: DynamoDB items with 1-hour TTL

## Transaction Categories

| # | Category | Examples |
|---|----------|----------|
| 1 | Food & Dining | Restaurants, delivery apps, cafés |
| 2 | Groceries | Supermarkets, markets, bodegas |
| 3 | Transportation | Fuel, taxis, Uber, parking, tolls |
| 4 | Housing | Rent, maintenance, repairs, home services |
| 5 | Utilities | Electricity, water, gas, internet, phone |
| 6 | Health | Pharmacy, doctors, clinics, insurance, medical |
| 7 | Personal Care | Haircuts, salons, barbers, grooming, cosmetics, spa, gym |
| 8 | Entertainment | Streaming, outings, events, hobbies |
| 9 | Shopping | Clothing, electronics, household items |
| 10 | Education | Courses, books, learning subscriptions |
| 11 | Work | Business meals, coworking, office supplies, work travel |
| 12 | Other | Catch-all with mandatory note |

## Payment Methods

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

## Deployment

```bash
# 1. Store secrets in SSM
aws ssm put-parameter --name /pfa/telegram-bot-token       --type SecureString --value "$TOKEN"
aws ssm put-parameter --name /pfa/telegram-webhook-secret  --type SecureString --value "$(openssl rand -hex 32)"
aws ssm put-parameter --name /pfa/allowed-user-id          --type String       --value "$YOUR_TG_ID"

# 2. Enable Bedrock model access in the console (Claude Sonnet 4.6 + Haiku 4.5, us-east-1)

# 3. Build and deploy
sam build
sam deploy --guided

# 4. Seed reference data (run once)
export AWS_REGION=us-east-1 TABLE_NAME=finance-agent BUCKET_NAME=pfa-receipts-<acct>
python -m db.seed_dynamo

# 5. Register the Telegram webhook
curl -X POST "https://api.telegram.org/bot$TOKEN/setWebhook" \
  -d "url=<WebhookUrl from sam outputs>" \
  -d "secret_token=$WEBHOOK_SECRET"
```

## Dashboard (local)

```bash
export AWS_PROFILE=your-profile AWS_REGION=us-east-1
# Optional: enable the password gate for the dashboard
cp .streamlit/secrets.toml.example .streamlit/secrets.toml  # then edit the password
streamlit run dashboard/app.py
```

The dashboard surfaces seven pages: Monthly Summary, Transactions (filter +
inline edit), Category Breakdown, Trends, Upload Statement (PDF parse +
preview + auto-reconcile), Manual Reconciliation (per-line candidate review,
unmatch, add-as-new), and Reconciliation Status.

## Project Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](project_documentation/ARCHITECTURE.md) | System components, tech stack, and how they connect |
| [FEATURES.md](project_documentation/FEATURES.md) | Detailed feature specifications and user flows |
| [DATA_MODEL.md](project_documentation/DATA_MODEL.md) | DynamoDB schema and access patterns |
| [AGENT_SPEC.md](project_documentation/AGENT_SPEC.md) | Agent behavior, prompts, and decision logic |

## Future Scope

- Additional banks and cards (Interbank, BBVA, Cencosud Scotiabank)
- Income tracking (salary, recurring inflows)
- Automated spending insights and alerts
- Budget tracking and goals
