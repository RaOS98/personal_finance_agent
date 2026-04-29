"""Deterministic text for periodic Telegram spending digests.

No LLM: aggregates come from DynamoDB via ``db.list_transactions_between``
and the same month rollup as ``api.aggregator.build_summary``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from db import dynamo as db


def week_start_monday(d: date) -> date:
    """Monday as start of week (``date.weekday()``: Monday = 0)."""
    return d - timedelta(days=d.weekday())


def rolling_week_pen_by_category(today: date, days: int = 7) -> dict[str, float]:
    """Sum PEN amounts by ``category_slug`` for the inclusive last ``days`` days."""
    start = today - timedelta(days=days - 1)
    items = db.list_transactions_between(start, today)
    pen: dict[str, float] = {}
    for item in items:
        if item.get("currency") == "USD":
            continue
        slug = item.get("category_slug") or "uncategorized"
        amt = float(item.get("amount") or 0)
        pen[slug] = pen.get(slug, 0.0) + amt
    return {k: round(v, 2) for k, v in pen.items()}


def top_n_categories(totals: dict[str, float], n: int = 3) -> list[tuple[str, float]]:
    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    return ordered[:n]


def format_insights_message(
    today: date,
    summary: dict[str, Any],
    week_pen_by_cat: dict[str, float],
) -> str | None:
    """Return plain-text digest, or ``None`` if there is nothing to report."""
    if int(summary.get("txn_count") or 0) == 0:
        return None

    ws = week_start_monday(today)
    lines: list[str] = [f"Week of {ws.isoformat()} — Quick look"]

    totals = summary.get("totals") or {}
    month_pen = float(totals.get("month_pen") or 0.0)
    month_usd = float(totals.get("month_usd") or 0.0)
    today_pen = float(totals.get("today_pen") or 0.0)
    today_usd = float(totals.get("today_usd") or 0.0)

    line = f"Month-to-date (PEN): S/. {month_pen:.2f} · Today: S/. {today_pen:.2f}"
    if month_usd or today_usd:
        line += f" · USD MTD: ${month_usd:.2f} (today ${today_usd:.2f})"
    lines.append(line)

    bc = summary.get("by_category_pen") or {}
    top_m = top_n_categories(
        {k: float(v) for k, v in bc.items()} if bc else {}, 3
    )
    if top_m:
        parts = [f"{slug} S/. {v:.2f}" for slug, v in top_m]
        lines.append("Top categories MTD (PEN): " + ", ".join(parts))

    top_w = top_n_categories(week_pen_by_cat, 3)
    if top_w:
        parts = [f"{slug} S/. {v:.2f}" for slug, v in top_w]
        lines.append("Last 7 days (PEN): " + ", ".join(parts))

    ur = int(summary.get("unreconciled_count") or 0)
    lines.append(f"Unreconciled txns: {ur}")
    lines.append(f"Logged txns this month: {int(summary.get('txn_count') or 0)}")
    return "\n".join(lines)
