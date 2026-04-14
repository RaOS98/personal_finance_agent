# Data Model

## Overview

All data is stored in a single PostgreSQL database. The schema is designed to be simple and flat — no deep nesting or complex relationships. There are six tables: three reference tables that hold configuration data, and three operational tables that hold transaction and reconciliation data.

## Entity Relationship Diagram

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   accounts   │     │ payment_methods   │     │   categories     │
│──────────────│     │──────────────────│     │──────────────────│
│ id           │◀─┐  │ id               │     │ id               │
│ name         │  │  │ name             │     │ name             │
│ bank         │  │  │ alias[]          │     │ slug             │
│ currency     │  │  │ account_id (FK)  │──┘  └──────────────────┘
│ type         │  │  └──────────────────┘              │
└──────────────┘  │                                     │
       ▲          │  ┌──────────────────┐              │
       │          │  │  transactions    │              │
       │          │  │──────────────────│              │
       │          └──│ payment_method_id│              │
       │             │ category_id (FK) │──────────────┘
       │             │ amount           │
       │             │ currency         │
       │             │ date             │
       │             │ merchant         │
       │             │ ...              │
       │             └──────────────────┘
       │                      │
       │                      │ (via reconciliation_matches)
       │                      │
       ▲             ┌──────────────────┐
       │             │ statement_lines  │
       │             │──────────────────│
       └─────────────│ account_id (FK)  │
                     │ amount           │
                     │ description      │
                     │ date             │
                     │ ...              │
                     └──────────────────┘
```

---

## Reference Tables

### categories

Static lookup table. Populated once at setup, rarely changes.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | SERIAL | PRIMARY KEY | Auto-increment ID |
| name | VARCHAR(50) | NOT NULL, UNIQUE | Display name (e.g., "Food & Dining") |
| slug | VARCHAR(30) | NOT NULL, UNIQUE | Lowercase key for code use (e.g., "food_dining") |

**Seed data:**

| id | name | slug |
|----|------|------|
| 1 | Food & Dining | food_dining |
| 2 | Groceries | groceries |
| 3 | Transportation | transportation |
| 4 | Housing | housing |
| 5 | Utilities | utilities |
| 6 | Health | health |
| 7 | Entertainment | entertainment |
| 8 | Shopping | shopping |
| 9 | Education | education |
| 10 | Other | other |

### accounts

Represents bank accounts and credit cards that appear on bank statements. Each account maps to one statement source.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | SERIAL | PRIMARY KEY | Auto-increment ID |
| name | VARCHAR(100) | NOT NULL, UNIQUE | Display name |
| bank | VARCHAR(50) | NOT NULL | Bank name |
| currency | VARCHAR(3) | NOT NULL | PEN or USD |
| type | VARCHAR(20) | NOT NULL | "credit_card" or "current_account" |

**Seed data (MVP):**

| id | name | bank | currency | type |
|----|------|------|----------|------|
| 1 | Visa Infinite Sapphire | BCP | PEN | credit_card |
| 2 | Amex Platinum | BCP | PEN | credit_card |
| 3 | Soles Current Account | BCP | PEN | current_account |
| 4 | Dollars Current Account | BCP | USD | current_account |

### payment_methods

Represents how the user pays. Each payment method settles to a specific account (which determines where it appears on a bank statement).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | SERIAL | PRIMARY KEY | Auto-increment ID |
| name | VARCHAR(100) | NOT NULL, UNIQUE | Display name |
| aliases | TEXT[] | NOT NULL, DEFAULT '{}' | Shorthand names the user can type in Telegram |
| account_id | INTEGER | FOREIGN KEY → accounts(id), NULLABLE | Settlement account. NULL for cash. |

**Seed data (MVP):**

| id | name | aliases | account_id |
|----|------|---------|------------|
| 1 | BCP Visa Infinite Sapphire | {sapphire, sap, visa} | 1 |
| 2 | BCP Amex Platinum | {amex, platinum} | 2 |
| 3 | Yape | {yape} | 3 |
| 4 | BCP Transfer (Soles) | {transfer, bcp} | 3 |
| 5 | BCP Transfer (Dollars) | {usd, dollars} | 4 |
| 6 | Cash | {cash, efectivo} | NULL |

---

## Operational Tables

### transactions

The core table. One row per confirmed transaction.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | SERIAL | PRIMARY KEY | Auto-increment ID |
| amount | DECIMAL(12,2) | NOT NULL | Transaction amount |
| currency | VARCHAR(3) | NOT NULL | PEN or USD |
| date | DATE | NOT NULL | Transaction date |
| merchant | VARCHAR(200) | | Merchant or payee name (as extracted) |
| description | TEXT | | Free-text notes (mandatory when category is Other) |
| category_id | INTEGER | NOT NULL, FOREIGN KEY → categories(id) | Spending category |
| payment_method_id | INTEGER | NOT NULL, FOREIGN KEY → payment_methods(id) | How the user paid |
| telegram_image_id | VARCHAR(200) | | Telegram file ID (secondary reference) |
| image_path | VARCHAR(300) | | Local file path to the saved receipt/screenshot image |
| reconciliation_status | VARCHAR(20) | NOT NULL, DEFAULT 'unreconciled' | "reconciled" or "unreconciled" |
| created_at | TIMESTAMP | NOT NULL, DEFAULT NOW() | When the record was created |

**Index:** `(amount, date, payment_method_id)` — used for duplicate detection and reconciliation candidate filtering.

### statement_lines

Rows extracted from bank statement PDFs. One row per line item on a statement.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | SERIAL | PRIMARY KEY | Auto-increment ID |
| account_id | INTEGER | NOT NULL, FOREIGN KEY → accounts(id) | Which account this statement belongs to |
| billing_period | VARCHAR(7) | NOT NULL | Year-month of the statement (e.g., "2026-04") |
| date | DATE | NOT NULL | Transaction date as shown on statement |
| description | VARCHAR(300) | NOT NULL | Merchant/description as shown on statement |
| amount | DECIMAL(12,2) | NOT NULL | Amount (positive = debit/charge) |
| reconciliation_status | VARCHAR(20) | NOT NULL, DEFAULT 'pending' | "matched", "added", "skipped", or "pending" |
| created_at | TIMESTAMP | NOT NULL, DEFAULT NOW() | When the record was imported |

**Index:** `(account_id, amount, date)` — used for reconciliation candidate lookup.

**Unique constraint:** `(account_id, billing_period, date, description, amount)` — prevents importing the same statement twice.

### reconciliation_matches

Join table linking statement lines to transactions. Created during reconciliation.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | SERIAL | PRIMARY KEY | Auto-increment ID |
| statement_line_id | INTEGER | NOT NULL, FOREIGN KEY → statement_lines(id), UNIQUE | One statement line matches at most one transaction |
| transaction_id | INTEGER | NOT NULL, FOREIGN KEY → transactions(id), UNIQUE | One transaction matches at most one statement line |
| verdict | VARCHAR(20) | NOT NULL | "confident", "likely", or "uncertain" |
| confirmed_by | VARCHAR(20) | NOT NULL | "auto" or "user" |
| created_at | TIMESTAMP | NOT NULL, DEFAULT NOW() | When the match was created |

**Constraint:** Both `statement_line_id` and `transaction_id` are UNIQUE, enforcing a strict one-to-one relationship. A statement line cannot be matched to multiple transactions and vice versa.

---

## Notes

**Currency is stored on the transaction, not derived from the account.** While BCP soles account transactions are almost always in PEN, storing the currency explicitly on each transaction avoids assumptions and keeps the data self-describing.

**Cash transactions have no settlement account.** The payment method "Cash" has a NULL `account_id`, so cash transactions are never included in reconciliation. Their `reconciliation_status` stays "unreconciled" permanently, which is correct — there is no statement to reconcile against.

**The `Other` category requires a description.** This is enforced at the application level, not the database level. When the agent assigns the Other category, it must ensure the description field is populated (either from the user's message or by asking).

**Statement credits are skipped in the MVP.** Salary deposits, reimbursements, and payments from friends will appear as statement lines during reconciliation. The user skips them, and their status is set to "skipped". Credit tracking is planned for v2.

**Images are stored locally, not just referenced.** When the user sends a photo via Telegram, the bot downloads it and saves it to `storage/images/` with the naming convention `txn_{id}.jpg`. The local file path is stored in `image_path`. The `telegram_image_id` is kept as a secondary reference but the local copy is the source of truth — Telegram file IDs can expire over time. Images can be viewed from the Streamlit dashboard alongside their linked transaction and, through reconciliation_matches, alongside the corresponding statement line.
