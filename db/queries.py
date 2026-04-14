"""Database query functions for the personal finance agent."""

from __future__ import annotations

from decimal import Decimal
from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from config import DATABASE_URL
from db.models import SCHEMA_SQL, SEED_SQL


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def get_connection() -> psycopg.Connection:
    """Return a new psycopg connection with dict row factory."""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables and seed reference data (idempotent)."""
    with get_connection() as conn:
        conn.execute(SCHEMA_SQL)
        conn.execute(SEED_SQL)
        conn.commit()


# ---------------------------------------------------------------------------
# Reference-data lookups
# ---------------------------------------------------------------------------

def resolve_payment_method(alias: str) -> dict[str, Any] | None:
    """Case-insensitive search in the aliases array.

    Returns a dict with payment method fields plus nested account info,
    or None if no match is found.
    """
    sql = """
        SELECT pm.id, pm.name, pm.aliases, pm.account_id,
               a.name  AS account_name,
               a.bank  AS account_bank,
               a.currency AS account_currency,
               a.type  AS account_type
          FROM payment_methods pm
          LEFT JOIN accounts a ON a.id = pm.account_id
         WHERE LOWER(%(alias)s) = ANY (
                   SELECT LOWER(unnest(pm.aliases))
               )
         LIMIT 1
    """
    with get_connection() as conn:
        return conn.execute(sql, {"alias": alias}).fetchone()


def get_category_by_slug(slug: str) -> dict[str, Any] | None:
    """Return a category dict by slug, or None."""
    sql = "SELECT id, name, slug FROM categories WHERE slug = %(slug)s"
    with get_connection() as conn:
        return conn.execute(sql, {"slug": slug}).fetchone()


def get_category_id_by_slug(slug: str) -> int | None:
    """Return a category primary key for *slug*, or None if unknown."""
    sql = "SELECT id FROM categories WHERE slug = %(slug)s"
    with get_connection() as conn:
        row = conn.execute(sql, {"slug": slug}).fetchone()
    return int(row["id"]) if row else None


def get_all_categories() -> list[dict[str, Any]]:
    """Return every category as a list of dicts."""
    sql = "SELECT id, name, slug FROM categories ORDER BY id"
    with get_connection() as conn:
        return conn.execute(sql).fetchall()


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def check_duplicate_transaction(
    amount: Decimal | float,
    date_val: date,
    payment_method_id: int,
) -> list[dict[str, Any]]:
    """Return transactions matching the exact amount, date, and payment method."""
    sql = """
        SELECT id, amount, currency, date, merchant, description,
               category_id, payment_method_id, reconciliation_status, created_at
          FROM transactions
         WHERE amount = %(amount)s
           AND date = %(date)s
           AND payment_method_id = %(pm_id)s
    """
    with get_connection() as conn:
        return conn.execute(
            sql, {"amount": amount, "date": date_val, "pm_id": payment_method_id}
        ).fetchall()


def save_transaction(
    amount: Decimal | float,
    currency: str,
    date_val: date,
    merchant: str | None,
    description: str | None,
    category_id: int,
    payment_method_id: int,
    telegram_image_id: str | None = None,
    image_path: str | None = None,
) -> dict[str, Any]:
    """Insert a transaction and return the new row including its id."""
    sql = """
        INSERT INTO transactions
               (amount, currency, date, merchant, description,
                category_id, payment_method_id, telegram_image_id, image_path)
        VALUES (%(amount)s, %(currency)s, %(date)s, %(merchant)s, %(description)s,
                %(category_id)s, %(pm_id)s, %(telegram_image_id)s, %(image_path)s)
        RETURNING *
    """
    with get_connection() as conn:
        row = conn.execute(sql, {
            "amount": amount,
            "currency": currency,
            "date": date_val,
            "merchant": merchant,
            "description": description,
            "category_id": category_id,
            "pm_id": payment_method_id,
            "telegram_image_id": telegram_image_id,
            "image_path": image_path,
        }).fetchone()
        conn.commit()
        return row


def update_transaction_image_path(txn_id: int, image_path: str) -> None:
    """Update the image_path for an existing transaction."""
    sql = "UPDATE transactions SET image_path = %(path)s WHERE id = %(id)s"
    with get_connection() as conn:
        conn.execute(sql, {"path": image_path, "id": txn_id})
        conn.commit()


def get_unreconciled_transactions(
    account_id: int,
    amount: Decimal | float,
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    """Find unreconciled transactions for a given account, amount, and date range."""
    sql = """
        SELECT t.id, t.amount, t.currency, t.date, t.merchant, t.description,
               t.category_id, t.payment_method_id, t.reconciliation_status,
               c.name AS category_name
          FROM transactions t
          JOIN payment_methods pm ON pm.id = t.payment_method_id
          JOIN categories c ON c.id = t.category_id
         WHERE pm.account_id = %(account_id)s
           AND t.amount = %(amount)s
           AND t.date BETWEEN %(date_from)s AND %(date_to)s
           AND t.reconciliation_status = 'unreconciled'
         ORDER BY t.date
    """
    with get_connection() as conn:
        return conn.execute(sql, {
            "account_id": account_id,
            "amount": amount,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()


def find_reconciliation_candidates(
    account_id: int,
    amount: Decimal | float,
    date_val: date,
    tolerance_days: int,
) -> list[dict[str, Any]]:
    """Candidates for reconciliation: same settlement *account_id*, exact *amount*,
    *date_val* ± *tolerance_days*, unreconciled. See AGENT_SPEC / DATA_MODEL.
    """
    date_from = date_val - timedelta(days=tolerance_days)
    date_to = date_val + timedelta(days=tolerance_days)
    return get_unreconciled_transactions(account_id, amount, date_from, date_to)


def update_statement_line_status(statement_line_id: int, status: str) -> None:
    """Set ``reconciliation_status`` on a statement line (matched, pending, etc.)."""
    sql = """
        UPDATE statement_lines
           SET reconciliation_status = %(status)s
         WHERE id = %(id)s
    """
    with get_connection() as conn:
        conn.execute(sql, {"status": status, "id": statement_line_id})
        conn.commit()


def update_transaction_reconciliation_status(transaction_id: int, status: str) -> None:
    """Set ``reconciliation_status`` on a transaction (reconciled / unreconciled)."""
    sql = """
        UPDATE transactions
           SET reconciliation_status = %(status)s
         WHERE id = %(id)s
    """
    with get_connection() as conn:
        conn.execute(sql, {"status": status, "id": transaction_id})
        conn.commit()


# ---------------------------------------------------------------------------
# Statement lines
# ---------------------------------------------------------------------------

def save_statement_lines(
    account_id: int,
    billing_period: str,
    lines: list[dict[str, Any]],
) -> int:
    """Bulk-insert statement lines. Returns the number of rows inserted.

    Each dict in *lines* must have keys: date, description, amount.
    Duplicates (per unique constraint) are silently skipped.
    """
    sql = """
        INSERT INTO statement_lines (account_id, billing_period, date, description, amount)
        VALUES (%(account_id)s, %(billing_period)s, %(date)s, %(description)s, %(amount)s)
        ON CONFLICT (account_id, billing_period, date, description, amount) DO NOTHING
    """
    inserted = 0
    with get_connection() as conn:
        for line in lines:
            result = conn.execute(sql, {
                "account_id": account_id,
                "billing_period": billing_period,
                "date": line["date"],
                "description": line["description"],
                "amount": line["amount"],
            })
            if result.rowcount:
                inserted += result.rowcount
        conn.commit()
    return inserted


def get_pending_statement_lines(
    account_id: int,
    billing_period: str,
) -> list[dict[str, Any]]:
    """Return statement lines with status 'pending' for a given account and period."""
    sql = """
        SELECT id, account_id, billing_period, date, description, amount,
               reconciliation_status, created_at
          FROM statement_lines
         WHERE account_id = %(account_id)s
           AND billing_period = %(billing_period)s
           AND reconciliation_status = 'pending'
         ORDER BY date, id
    """
    with get_connection() as conn:
        return conn.execute(sql, {
            "account_id": account_id,
            "billing_period": billing_period,
        }).fetchall()


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def save_reconciliation_match(
    statement_line_id: int,
    transaction_id: int,
    verdict: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Insert a reconciliation match and update both sides' statuses.

    Returns the new reconciliation_matches row.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO reconciliation_matches
                   (statement_line_id, transaction_id, verdict, confirmed_by)
            VALUES (%(sl_id)s, %(txn_id)s, %(verdict)s, %(confirmed_by)s)
            RETURNING *
            """,
            {
                "sl_id": statement_line_id,
                "txn_id": transaction_id,
                "verdict": verdict,
                "confirmed_by": confirmed_by,
            },
        ).fetchone()

        conn.execute(
            "UPDATE statement_lines SET reconciliation_status = 'matched' WHERE id = %(id)s",
            {"id": statement_line_id},
        )
        conn.execute(
            "UPDATE transactions SET reconciliation_status = 'reconciled' WHERE id = %(id)s",
            {"id": transaction_id},
        )
        conn.commit()
        return row


# ---------------------------------------------------------------------------
# Dashboard / reporting queries
# ---------------------------------------------------------------------------

def get_monthly_spending_by_category(year: int, month: int) -> list[dict[str, Any]]:
    """Total spending per category for a given month."""
    sql = """
        SELECT c.id AS category_id, c.name AS category_name, c.slug,
               COALESCE(SUM(t.amount), 0) AS total
          FROM categories c
          LEFT JOIN transactions t
               ON t.category_id = c.id
              AND EXTRACT(YEAR  FROM t.date) = %(year)s
              AND EXTRACT(MONTH FROM t.date) = %(month)s
         GROUP BY c.id, c.name, c.slug
         ORDER BY total DESC
    """
    with get_connection() as conn:
        return conn.execute(sql, {"year": year, "month": month}).fetchall()


def get_monthly_spending_by_payment_method(year: int, month: int) -> list[dict[str, Any]]:
    """Total spending per payment method for a given month."""
    sql = """
        SELECT pm.id AS payment_method_id, pm.name AS payment_method_name,
               COALESCE(SUM(t.amount), 0) AS total
          FROM payment_methods pm
          LEFT JOIN transactions t
               ON t.payment_method_id = pm.id
              AND EXTRACT(YEAR  FROM t.date) = %(year)s
              AND EXTRACT(MONTH FROM t.date) = %(month)s
         GROUP BY pm.id, pm.name
         ORDER BY total DESC
    """
    with get_connection() as conn:
        return conn.execute(sql, {"year": year, "month": month}).fetchall()


def get_monthly_totals(currency: str) -> list[dict[str, Any]]:
    """Monthly spending totals over time, filtered by currency."""
    sql = """
        SELECT EXTRACT(YEAR  FROM date)::int AS year,
               EXTRACT(MONTH FROM date)::int AS month,
               SUM(amount) AS total
          FROM transactions
         WHERE currency = %(currency)s
         GROUP BY year, month
         ORDER BY year, month
    """
    with get_connection() as conn:
        return conn.execute(sql, {"currency": currency}).fetchall()


def get_transactions_for_period(
    year: int,
    month: int,
    category_id: int | None = None,
    payment_method_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return transactions for a month with optional category/payment-method filters."""
    conditions = [
        "EXTRACT(YEAR  FROM t.date) = %(year)s",
        "EXTRACT(MONTH FROM t.date) = %(month)s",
    ]
    params: dict[str, Any] = {"year": year, "month": month}

    if category_id is not None:
        conditions.append("t.category_id = %(category_id)s")
        params["category_id"] = category_id
    if payment_method_id is not None:
        conditions.append("t.payment_method_id = %(pm_id)s")
        params["pm_id"] = payment_method_id

    sql = f"""
        SELECT t.id, t.amount, t.currency, t.date, t.merchant, t.description,
               c.name AS category_name, pm.name AS payment_method_name,
               t.reconciliation_status, t.created_at
          FROM transactions t
          JOIN categories c      ON c.id  = t.category_id
          JOIN payment_methods pm ON pm.id = t.payment_method_id
         WHERE {' AND '.join(conditions)}
         ORDER BY t.date DESC, t.id DESC
    """
    with get_connection() as conn:
        return conn.execute(sql, params).fetchall()


def get_reconciliation_summary(year: int, month: int) -> list[dict[str, Any]]:
    """Count of transactions grouped by reconciliation_status for a given month."""
    sql = """
        SELECT reconciliation_status, COUNT(*) AS count
          FROM transactions
         WHERE EXTRACT(YEAR  FROM date) = %(year)s
           AND EXTRACT(MONTH FROM date) = %(month)s
         GROUP BY reconciliation_status
         ORDER BY reconciliation_status
    """
    with get_connection() as conn:
        return conn.execute(sql, {"year": year, "month": month}).fetchall()
