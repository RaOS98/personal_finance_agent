# Data Model

## Overview

All data is stored in a single DynamoDB table (`finance-agent`) using a single-table design. Every entity type is distinguished by its `PK` and `SK` values. Two Global Secondary Indexes (GSI1, GSI2) cover access patterns that cannot be served by the primary key.

---

## Primary Key Structure

| Entity | PK | SK |
|--------|----|----|
| Atomic counter | `COUNTER` | `{name}` (e.g., `txn`) |
| Reference data | `REF` | `CATEGORY#{slug}` / `ACCOUNT#{id:04d}` / `PM#{id:04d}` |
| Transaction | `TXN` | `{date_iso}#{txn_id:08d}` |
| Statement line | `STMT#{account_id}#{billing_period}` | `{date_iso}#{line_id}` |
| Reconciliation match | `MATCH` | `STMT#{line_id}#TXN#{txn_id:08d}` |
| User bot state | `STATE#{user_id}` | `current` |

---

## Global Secondary Indexes

### GSI1 — Reconciliation candidate lookup

Enables finding unreconciled transactions by settlement account and exact amount.

| Key | Value |
|-----|-------|
| GSI1PK | `AMT#{account_id}#{amount_cents}` |
| GSI1SK | `{date_iso}#{txn_id:08d}` |

Query pattern: given a statement line with `account_id` and `amount`, find all transactions with matching account and amount within a date range.

### GSI2 — Status-based transaction queries

| Key | Value |
|-----|-------|
| GSI2PK | `STATUS#TXN#{reconciliation_status}` |
| GSI2SK | `{date_iso}#{txn_id:08d}` |

Query pattern: find all unreconciled (or reconciled) transactions across all dates.

---

## Entity Definitions

### COUNTER

Atomic auto-increment counter. Used for transaction IDs.

| Attribute | Type | Description |
|-----------|------|-------------|
| PK | S | `COUNTER` |
| SK | S | Counter name (e.g., `txn`) |
| value | N | Current counter value |

Incremented atomically with `UpdateItem ADD value :1 RETURN UPDATED_NEW`.

---

### REF — Reference Data

All reference data shares `PK = REF`. Loaded once per Lambda cold start and cached in memory.

#### CATEGORY

| Attribute | Type | Description |
|-----------|------|-------------|
| PK | S | `REF` |
| SK | S | `CATEGORY#{slug}` |
| id | N | Integer category ID |
| name | S | Display name (e.g., "Food & Dining") |
| slug | S | Code-friendly key (e.g., `food_dining`) |

**Seed data:**

| id | name | slug |
|----|------|------|
| 1 | Food & Dining | food_dining |
| 2 | Groceries | groceries |
| 3 | Transportation | transportation |
| 4 | Housing | housing |
| 5 | Utilities | utilities |
| 6 | Health | health |
| 7 | Personal Care | personal_care |
| 8 | Entertainment | entertainment |
| 9 | Shopping | shopping |
| 10 | Education | education |
| 11 | Work | work |
| 12 | Other | other |

#### ACCOUNT

| Attribute | Type | Description |
|-----------|------|-------------|
| PK | S | `REF` |
| SK | S | `ACCOUNT#{id:04d}` |
| id | N | Integer account ID |
| name | S | Display name |
| bank | S | Bank name |
| currency | S | `PEN` or `USD` |
| type | S | `credit_card` or `current_account` |

**Seed data:**

| id | name | bank | currency | type |
|----|------|------|----------|------|
| 1 | Visa Infinite Sapphire | BCP | PEN | credit_card |
| 2 | Amex Platinum | BCP | PEN | credit_card |
| 3 | Soles Current Account | BCP | PEN | current_account |
| 4 | Dollars Current Account | BCP | USD | current_account |

#### PAYMENT METHOD (PM)

| Attribute | Type | Description |
|-----------|------|-------------|
| PK | S | `REF` |
| SK | S | `PM#{id:04d}` |
| id | N | Integer payment method ID |
| name | S | Display name |
| aliases | L | List of alias strings (lowercase) |
| account_id | N | Settlement account ID (absent for Cash) |

**Seed data:**

| id | name | aliases | account_id |
|----|------|---------|------------|
| 1 | BCP Visa Infinite Sapphire | sapphire, sap, visa | 1 |
| 2 | BCP Amex Platinum | amex, platinum | 2 |
| 3 | Yape | yape | 3 |
| 4 | BCP Transfer (Soles) | transfer, bcp | 3 |
| 5 | BCP Transfer (Dollars) | usd, dollars | 4 |
| 6 | Cash | cash, efectivo | — |

---

### TXN — Transactions

One item per confirmed transaction.

| Attribute | Type | Description |
|-----------|------|-------------|
| PK | S | `TXN` |
| SK | S | `{date_iso}#{txn_id:08d}` |
| GSI1PK | S | `AMT#{account_id}#{amount_cents}` |
| GSI1SK | S | `{date_iso}#{txn_id:08d}` |
| GSI2PK | S | `STATUS#TXN#{reconciliation_status}` |
| GSI2SK | S | `{date_iso}#{txn_id:08d}` |
| id | N | Integer transaction ID (from COUNTER) |
| amount | N (Decimal) | Transaction amount |
| amount_cents | N | Amount × 100 as integer (used in GSI1PK) |
| currency | S | `PEN` or `USD` |
| date | S | `YYYY-MM-DD` |
| merchant | S | Merchant or payee name |
| description | S | Free-text notes |
| category_id | N | Category reference |
| category_slug | S | Category slug |
| category_name | S | Category display name |
| payment_method_id | N | Payment method reference |
| payment_method_name | S | Payment method display name |
| account_id | N | Settlement account ID (absent for Cash) |
| telegram_image_id | S | Telegram file ID (secondary reference) |
| image_path | S | S3 key (`receipts/{yyyy}/{mm}/txn_{id}.jpg`) |
| reconciliation_status | S | `reconciled` or `unreconciled` |
| created_at | S | ISO 8601 timestamp |

**Notes:**
- `account_id` is absent for Cash transactions. Cash transactions are never candidates for reconciliation.
- `image_path` is populated after the receipt image is finalized in S3.
- GSI key values are updated atomically when `reconciliation_status` changes (requires a new item write in DynamoDB; status is stored in the key itself in GSI2PK).

---

### STMT — Statement Lines

Items extracted from bank statement PDFs. Grouped by account and billing period.

| Attribute | Type | Description |
|-----------|------|-------------|
| PK | S | `STMT#{account_id}#{billing_period}` |
| SK | S | `{date_iso}#{line_id}` |
| id | S | Content hash (blake2b 6-byte digest, hex) |
| account_id | N | Settlement account ID |
| billing_period | S | `YYYY-MM` |
| date | S | `YYYY-MM-DD` |
| description | S | Merchant/description as shown on statement |
| amount | N (Decimal) | Charge amount (positive = debit) |
| amount_cents | N | Amount × 100 as integer |
| reconciliation_status | S | `pending`, `matched`, `added`, or `skipped` |
| created_at | S | ISO 8601 timestamp |

**ID generation:** `blake2b(f"{account_id}|{billing_period}|{date}|{description}|{amount_cents}", digest_size=6).hexdigest()`. This makes re-importing the same statement idempotent — duplicate lines are rejected by `ConditionExpression="attribute_not_exists(PK)"`.

---

### MATCH — Reconciliation Matches

Links a statement line to a transaction. Created during reconciliation.

| Attribute | Type | Description |
|-----------|------|-------------|
| PK | S | `MATCH` |
| SK | S | `STMT#{line_id}#TXN#{txn_id:08d}` |
| statement_line_id | S | Statement line content hash |
| transaction_id | N | Transaction integer ID |
| verdict | S | `confident`, `likely`, or `uncertain` |
| confirmed_by | S | `auto` or `user` |
| created_at | S | ISO 8601 timestamp |

---

### STATE — User Bot State

Stores per-user conversation state for the Telegram bot. Auto-expires after inactivity.

| Attribute | Type | Description |
|-----------|------|-------------|
| PK | S | `STATE#{user_id}` |
| SK | S | `current` |
| state | S | JSON-serialized state dict |
| ttl | N | Unix timestamp; DynamoDB TTL deletes item after expiry |

TTL is set to `now + USER_STATE_TTL_SECONDS` (default: 3600) on every write. Expired items are deleted by DynamoDB automatically — no cleanup needed.

---

## Notes

**Currency is stored on the transaction, not derived from the account.** Storing currency explicitly on each transaction avoids assumptions and keeps records self-describing.

**Cash transactions have no settlement account.** The payment method "Cash" has no `account_id`, so cash transactions are never included in reconciliation. Their `reconciliation_status` stays `unreconciled` permanently.

**The `Other` category requires a description.** Enforced at the application level. When the agent assigns the Other category, it ensures the description field is populated.

**Statement credits are skipped.** Salary deposits, reimbursements, and payments from friends appear as statement lines but are skipped by the user during reconciliation. Their status is set to `skipped`.

**Images are stored in S3, not locally.** Receipt images follow a tmp→final copy pattern via `s3_store.py`. The `image_path` attribute on a transaction holds the S3 object key (`receipts/{yyyy}/{mm}/txn_{id}.jpg`).
