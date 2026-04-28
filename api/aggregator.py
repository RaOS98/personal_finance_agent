"""Pure-Python aggregation of monthly transactions for the widget API.

Reads the same DynamoDB table the bot writes to and produces a small,
versioned JSON envelope that's safe to render in a Scriptable widget or a
future static web dashboard. Stays allocation-light on purpose: no pandas,
no DataFrame round-trips -- a single Query plus a couple of dict
accumulators keeps the widget Lambda's cold start and per-call latency
both well under what iOS will tolerate.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from db import dynamo as db


_VERSION = 1


def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, float):
        return v
    return float(v)


def build_summary(today: date) -> dict[str, Any]:
    """Build the widget summary JSON envelope for the month containing ``today``.

    ``today`` is expected to be the user's wall-clock date (the caller is
    responsible for any UTC-to-local conversion). The whole computation is
    backed by a single ``Query`` against the ``TXN`` partition with a
    ``YYYY-MM`` SK prefix, which stays well under DynamoDB's 1 MB single-page
    limit at the expected data volume.

    Currency-split categories (``by_category_pen`` / ``by_category_usd``)
    keep the response easy for a future web client that may want to render
    PEN and USD side-by-side; the iPhone widget renders only the PEN view
    since USD spend is rare.
    """
    items = db.list_transactions_in_month(today.year, today.month)

    month_pen = 0.0
    month_usd = 0.0
    today_pen = 0.0
    today_usd = 0.0
    by_category_pen: dict[str, float] = {}
    by_category_usd: dict[str, float] = {}
    unreconciled_count = 0

    today_iso = today.isoformat()

    for item in items:
        amount = _to_float(item.get("amount"))
        currency = item.get("currency")
        date_iso = item.get("date") or ""
        slug = item.get("category_slug") or "uncategorized"
        status = item.get("reconciliation_status")

        if currency == "USD":
            month_usd += amount
            by_category_usd[slug] = by_category_usd.get(slug, 0.0) + amount
            if date_iso == today_iso:
                today_usd += amount
        else:
            # Default everything that isn't explicitly USD to PEN. The bot
            # enforces currency at save time, so this branch should always
            # be PEN in practice; defaulting unknowns here keeps the widget
            # honest if a malformed item ever sneaks in.
            month_pen += amount
            by_category_pen[slug] = by_category_pen.get(slug, 0.0) + amount
            if date_iso == today_iso:
                today_pen += amount

        if status == "unreconciled":
            unreconciled_count += 1

    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "version": _VERSION,
        "as_of": as_of,
        "period": {"year": today.year, "month": today.month},
        "totals": {
            "month_pen": round(month_pen, 2),
            "month_usd": round(month_usd, 2),
            "today_pen": round(today_pen, 2),
            "today_usd": round(today_usd, 2),
        },
        "by_category_pen": {k: round(v, 2) for k, v in by_category_pen.items()},
        "by_category_usd": {k: round(v, 2) for k, v in by_category_usd.items()},
        "unreconciled_count": unreconciled_count,
        "txn_count": len(items),
    }
