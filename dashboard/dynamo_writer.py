"""Dashboard-side writebacks.

All writers here are thin wrappers around :mod:`db.dynamo` (plus a couple of
:mod:`s3_store` helpers) that additionally call ``st.cache_data.clear()`` so
readers on subsequent reruns see the freshly written state. The bot never
imports this module; it stays on the Lambda-friendly direct path.

Keeping cache invalidation out of the low-level data access layer means the
bot doesn't take an unnecessary ``streamlit`` dependency.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import streamlit as st


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import dynamo as db  # noqa: E402
import s3_store  # noqa: E402


logger = logging.getLogger(__name__)


def invalidate_cache() -> None:
    """Flush every ``@st.cache_data`` entry.

    We always nuke the whole cache instead of targeting individual readers
    because the dashboard has just a handful of top-level reads (all of them
    cheap DynamoDB scans) and the simplicity matters more than the small cost
    of an extra refetch. Call this from pages that bypass the other writers
    here -- e.g. after kicking off :func:`agent.reconciliation.auto_reconcile`
    which writes directly via :mod:`db.dynamo`.
    """
    try:
        st.cache_data.clear()
    except Exception:
        logger.exception("Failed to clear Streamlit cache")


_invalidate_cache = invalidate_cache  # keep the private alias used internally


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def update_transaction(txn_id: int, fields: dict[str, Any]) -> dict[str, Any]:
    """Apply partial edits to a transaction, then flush dashboard caches.

    Raises the same exceptions as :func:`db.dynamo.update_transaction_fields`
    (``KeyError`` if missing, ``ValueError`` if a disallowed field slips in).
    """
    result = db.update_transaction_fields(int(txn_id), fields)
    _invalidate_cache()
    return result


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def save_reconciliation(
    line_id: str,
    txn_id: int,
    verdict: str = "user-confirmed",
    confirmed_by: str = "user-dashboard",
) -> dict[str, Any]:
    """Persist a manual match and flip both sides' statuses."""
    result = db.save_reconciliation_match(
        statement_line_id=line_id,
        transaction_id=int(txn_id),
        verdict=verdict,
        confirmed_by=confirmed_by,
    )
    _invalidate_cache()
    return result


def unmatch(line_id: str, txn_id: int) -> bool:
    """Remove an existing match and roll both sides back to pending."""
    deleted = db.delete_reconciliation_match(line_id, int(txn_id))
    _invalidate_cache()
    return deleted


def set_statement_line_status(line_id: str, status: str) -> None:
    """Used to mark lines as ``skipped`` without creating a match row."""
    db.update_statement_line_status(line_id, status)
    _invalidate_cache()


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

def commit_statement(
    account_id: int,
    billing_period: str,
    pdf_bytes: bytes,
    lines: list[dict[str, Any]],
) -> tuple[str, int]:
    """Upload the PDF, persist statement lines, and return (pdf_s3_key, inserted).

    ``inserted`` is the count of lines that were newly written (the underlying
    bulk insert is idempotent via a deterministic content hash).
    """
    pdf_s3_key = s3_store.upload_statement_pdf(
        account_id=int(account_id),
        billing_period=billing_period,
        pdf_bytes=pdf_bytes,
    )
    inserted = db.save_statement_lines(
        int(account_id),
        billing_period,
        lines,
        pdf_s3_key=pdf_s3_key,
    )
    _invalidate_cache()
    return pdf_s3_key, inserted


def create_transaction(
    amount: float,
    currency: str,
    date_val: Any,
    merchant: str | None,
    description: str | None,
    category_id: int,
    payment_method_id: int,
    telegram_image_id: str | None = None,
    image_path: str | None = None,
) -> dict[str, Any]:
    """Create a new transaction from the dashboard.

    Typically invoked from the Manual Reconciliation page when the user
    confirms that a statement line corresponds to a purchase they never
    logged via the Telegram bot.
    """
    result = db.save_transaction(
        amount=amount,
        currency=currency,
        date_val=date_val,
        merchant=merchant,
        description=description,
        category_id=int(category_id),
        payment_method_id=int(payment_method_id),
        telegram_image_id=telegram_image_id,
        image_path=image_path,
    )
    _invalidate_cache()
    return result
