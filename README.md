# Personal Finance Agent

A Telegram-based AI agent for tracking personal finances in Peru, where open banking is not available. The agent captures transactions via photos and text messages, categorizes spending, stores structured data, reconciles against monthly bank statements, and provides a live dashboard for financial visibility.

## The Problem

Most Peruvian banks lack APIs for downloading transaction data. There is no way to filter by date range — only the most recent transactions are visible in banking apps. This makes it nearly impossible to maintain a clear picture of personal spending, which leads to poor financial habits and difficulty saving.

## The Solution

An AI-powered agent that runs on Telegram. The user sends photos of receipts, transfer screenshots, or Yape/Plin confirmations along with a brief message indicating the payment method and category. The agent extracts transaction details, confirms them with the user, and stores everything in DynamoDB. At the end of each month, bank statement PDFs are uploaded and the agent reconciles logged transactions against them.

## How It Works

1. **Route** — Free-form text messages are classified into `new_transaction` or `edit`. Photos always route directly to the new-transaction flow. Prefix `!` forces the edit flow.
2. **Capture** — For new transactions, send a photo or text message to the Telegram bot with basic context (e.g., "Sapphire, groceries"). Claude Sonnet reads the image and extracts merchant name, amount, date, and currency.
3. **Confirm** — The agent replies with a structured summary for the user to approve or correct.
4. **Store** — Confirmed transactions are saved to DynamoDB; receipt images go to S3.
5. **Edit** — Send a natural-language correction (e.g., "change amount to 25") to update the most recent transaction. The bot shows a yes/no diff before any write.
6. **Reconcile** — Monthly bank statement PDFs are uploaded either through the Telegram bot or the Streamlit dashboard. Python pre-filters by signed amount and date; the agent evaluates all candidates for a statement line in one batched call. Confident singletons auto-match (with a date-diff tiebreaker for ties); everything else is reviewed in the dashboard's Manual Reconciliation page.
7. **Visualize and edit** — A local Streamlit dashboard backed by DynamoDB provides real-time visibility into spending and a writeable surface for transaction edits, statement uploads, and manual reconciliation. A simple shared-password gate via `st.secrets` keeps it out of casual hands.
8. **Periodic insights** — A scheduled Lambda (no LLM) reads DynamoDB, formats a short digest (month-to-date totals, top categories, last-seven-day roll-up, unreconciled count), and sends it to your Telegram chat. See `insights_handler.py` and EventBridge in `template.yaml`.

## Architecture

- **Runtime**: AWS Lambda (python3.12, arm64): Telegram webhook via API Gateway HTTP API; weekly insights digest via EventBridge schedule (`pfa-insights`, no Bedrock)
- **AI**: Amazon Bedrock — Claude Sonnet 4.6 for vision extraction, Claude Haiku 4.5 for intent classification, categorization, edit parsing, and reconciliation
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

## Widget API (iPhone widget + future web dashboard)

A second Lambda (`pfa-widget-api`) sits behind the same API Gateway and
serves a single read-only endpoint, `GET /widget/summary`, that returns a
versioned JSON envelope of the current month's spending. It backs the
Scriptable iOS widget today and is designed to back a static web dashboard
later without breaking changes.

### One-time setup

1. Provision the bearer token. Generate a random value and store it in SSM
   as a `SecureString`:

   ```bash
   aws ssm put-parameter \
     --name /pfa/widget-bearer-token \
     --type SecureString \
     --value "$(openssl rand -hex 32)" \
     --description "Bearer token for the personal-finance widget API"
   ```

2. Deploy. The same `sam deploy` command that ships the bot also creates
   the widget function, its log group, and the new `/widget/summary` route:

   ```bash
   sam build && sam deploy
   ```

   Copy the `WidgetApiUrl` value from the stack outputs.

3. Install the Scriptable widget on iPhone.

   - Install [Scriptable](https://scriptable.app/) from the App Store.
   - Paste [widgets/setup_keychain.js](widgets/setup_keychain.js) and
     [widgets/scriptable.js](widgets/scriptable.js) as two separate scripts.
   - Run `setup_keychain.js` once. It prompts for the API URL and the bearer
     token (the SSM value from step 1) and stores both in the iOS Keychain.
   - On the home screen, add a medium Scriptable widget and select
     `scriptable.js`.

### Rotating the bearer token

```bash
aws ssm put-parameter \
  --name /pfa/widget-bearer-token \
  --type SecureString \
  --overwrite \
  --value "$(openssl rand -hex 32)"

# Force the Lambda containers to pick up the new value (the lru_cache
# inside config._get_secret only refreshes on a cold start).
sam deploy
```

Re-run `setup_keychain.js` on the iPhone with the new token.

### JSON contract (`v1`)

```json
{
  "version": 1,
  "as_of": "2026-04-28T16:32:11Z",
  "period": {"year": 2026, "month": 4},
  "totals": {"month_pen": 1240.50, "month_usd": 45.00, "today_pen": 80.00, "today_usd": 0.00},
  "by_category_pen": {"groceries": 380.0, "food_dining": 220.0},
  "by_category_usd": {},
  "unreconciled_count": 3,
  "txn_count": 47
}
```

The category dicts are slug-keyed so the widget and a future SPA can render
their own display names. The Scriptable script keeps a small slug → display
map in sync with the [transaction categories](#transaction-categories) list
above; remember to update both in tandem if a category is added.

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
- Richer insight templates (budgets, anomaly flags) on top of the scheduled digest
- Budget tracking and goals
