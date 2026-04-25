"""DynamoDB-backed reader for the local Streamlit dashboard.

Exposes the same function names and DataFrame shapes as the previous
Postgres-based dashboard queries, so ``dashboard/app.py`` only needs to swap
its top-level query imports to this module.

Data volume is tiny (hundreds of transactions per year), so every function
scans the relevant partition and aggregates with pandas. No caching here --
the Streamlit layer already caches with ``@st.cache_data(ttl=60)``.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from typing import Any

import boto3
import pandas as pd
from boto3.dynamodb.conditions import Key


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402


_table = boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(
    config.DYNAMODB_TABLE
)


def _to_float(v: Any) -> float:
    if isinstance(v, Decimal):
        return float(v)
    return float(v) if v is not None else 0.0


def _query_all(**kwargs) -> list[dict]:
    """Paginated query covering LastEvaluatedKey."""
    items: list[dict] = []
    while True:
        resp = _table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


_TXN_COLUMNS = [
    "id", "date", "merchant", "description", "amount",
    "currency", "category_name", "category_slug",
    "payment_method_name", "payment_method_id",
    "reconciliation_status", "account_id", "image_path",
]


def _fetch_all_transactions() -> pd.DataFrame:
    items = _query_all(KeyConditionExpression=Key("PK").eq("TXN"))
    if not items:
        return pd.DataFrame(columns=_TXN_COLUMNS)
    rows = []
    for item in items:
        rows.append(
            {
                "id": int(item.get("id", 0)),
                "date": item.get("date"),
                "merchant": item.get("merchant"),
                "description": item.get("description"),
                "amount": _to_float(item.get("amount")),
                "currency": item.get("currency"),
                "category_name": item.get("category_name"),
                "category_slug": item.get("category_slug"),
                "payment_method_name": item.get("payment_method_name"),
                "payment_method_id": (
                    int(item["payment_method_id"])
                    if item.get("payment_method_id") is not None
                    else None
                ),
                "reconciliation_status": item.get("reconciliation_status"),
                "account_id": (
                    int(item["account_id"])
                    if item.get("account_id") is not None
                    else None
                ),
                "image_path": item.get("image_path"),
            }
        )
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


_STMT_COLUMNS = [
    "id", "account_id", "billing_period", "date",
    "description", "amount", "reconciliation_status", "pdf_s3_key",
]


def _fetch_all_statement_lines() -> pd.DataFrame:
    """Scan-based fetch of all STMT# items (small volume)."""
    items: list[dict] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "begins_with(PK, :p)",
        "ExpressionAttributeValues": {":p": "STMT#"},
    }
    while True:
        resp = _table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    if not items:
        return pd.DataFrame(columns=_STMT_COLUMNS)
    rows = []
    for item in items:
        rows.append(
            {
                "id": item.get("id"),
                "account_id": int(item["account_id"]) if item.get("account_id") is not None else None,
                "billing_period": item.get("billing_period"),
                "date": item.get("date"),
                "description": item.get("description"),
                "amount": _to_float(item.get("amount")),
                "reconciliation_status": item.get("reconciliation_status"),
                "pdf_s3_key": item.get("pdf_s3_key"),
            }
        )
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def _fetch_accounts() -> dict[int, str]:
    items = _query_all(
        KeyConditionExpression=Key("PK").eq("REF") & Key("SK").begins_with("ACCOUNT#"),
    )
    return {int(i["id"]): i.get("name", "") for i in items}


def _fetch_categories() -> list[str]:
    items = _query_all(
        KeyConditionExpression=Key("PK").eq("REF") & Key("SK").begins_with("CATEGORY#"),
    )
    return sorted(i.get("name", "") for i in items)


def _fetch_payment_methods() -> list[str]:
    items = _query_all(
        KeyConditionExpression=Key("PK").eq("REF") & Key("SK").begins_with("PM#"),
    )
    return sorted(i.get("name", "") for i in items)


def _fetch_payment_methods_detailed() -> list[dict[str, Any]]:
    """All payment methods with id, name, and settlement account_id."""
    items = _query_all(
        KeyConditionExpression=Key("PK").eq("REF") & Key("SK").begins_with("PM#"),
    )
    result: list[dict[str, Any]] = []
    for i in items:
        result.append(
            {
                "id": int(i.get("id", 0)),
                "name": i.get("name", ""),
                "account_id": (
                    int(i["account_id"]) if i.get("account_id") is not None else None
                ),
            }
        )
    result.sort(key=lambda pm: pm["id"])
    return result


def _fetch_categories_detailed() -> list[dict[str, Any]]:
    """All categories with id, name, and slug."""
    items = _query_all(
        KeyConditionExpression=Key("PK").eq("REF") & Key("SK").begins_with("CATEGORY#"),
    )
    result = [
        {
            "id": int(i.get("id", 0)),
            "name": i.get("name", ""),
            "slug": i.get("slug", ""),
        }
        for i in items
    ]
    result.sort(key=lambda c: c["id"])
    return result


def _fetch_accounts_detailed() -> list[dict[str, Any]]:
    """All accounts with id, name, currency, type."""
    items = _query_all(
        KeyConditionExpression=Key("PK").eq("REF") & Key("SK").begins_with("ACCOUNT#"),
    )
    result = [
        {
            "id": int(i.get("id", 0)),
            "name": i.get("name", ""),
            "currency": i.get("currency", ""),
            "type": i.get("type", ""),
        }
        for i in items
    ]
    result.sort(key=lambda a: a["id"])
    return result


# ---------------------------------------------------------------------------
# Public query functions (shape matches the Postgres-era functions)
# ---------------------------------------------------------------------------

def get_monthly_totals(year: int, month: int) -> pd.DataFrame:
    df = _fetch_all_transactions()
    if df.empty:
        return pd.DataFrame(columns=["currency", "total", "txn_count"])
    mask = df["date"].apply(lambda d: d.year == year and d.month == month)
    filtered = df[mask]
    if filtered.empty:
        return pd.DataFrame(columns=["currency", "total", "txn_count"])
    grouped = (
        filtered.groupby("currency")
        .agg(total=("amount", "sum"), txn_count=("id", "count"))
        .reset_index()
    )
    return grouped


def get_spending_by_category(year: int, month: int) -> pd.DataFrame:
    df = _fetch_all_transactions()
    if df.empty:
        return pd.DataFrame(columns=["category", "currency", "total"])
    mask = df["date"].apply(lambda d: d.year == year and d.month == month)
    filtered = df[mask]
    if filtered.empty:
        return pd.DataFrame(columns=["category", "currency", "total"])
    grouped = (
        filtered.groupby(["category_name", "currency"])["amount"]
        .sum()
        .reset_index()
        .rename(columns={"category_name": "category", "amount": "total"})
        .sort_values("total", ascending=False)
    )
    return grouped


def get_spending_by_payment_method(year: int, month: int) -> pd.DataFrame:
    df = _fetch_all_transactions()
    if df.empty:
        return pd.DataFrame(columns=["payment_method", "currency", "total"])
    mask = df["date"].apply(lambda d: d.year == year and d.month == month)
    filtered = df[mask]
    if filtered.empty:
        return pd.DataFrame(columns=["payment_method", "currency", "total"])
    grouped = (
        filtered.groupby(["payment_method_name", "currency"])["amount"]
        .sum()
        .reset_index()
        .rename(columns={"payment_method_name": "payment_method", "amount": "total"})
        .sort_values("total", ascending=False)
    )
    return grouped


def get_categories() -> list[str]:
    return _fetch_categories()


def get_payment_methods() -> list[str]:
    return _fetch_payment_methods()


def get_category_transactions(
    category: str,
    start_date: date,
    end_date: date,
    payment_method: str | None = None,
) -> pd.DataFrame:
    df = _fetch_all_transactions()
    if df.empty:
        return pd.DataFrame(
            columns=["date", "merchant", "amount", "currency", "payment_method", "description"]
        )
    mask = (
        (df["category_name"] == category)
        & (df["date"] >= start_date)
        & (df["date"] <= end_date)
    )
    if payment_method:
        mask &= df["payment_method_name"] == payment_method
    filtered = df[mask].copy()
    if filtered.empty:
        return pd.DataFrame(
            columns=["date", "merchant", "amount", "currency", "payment_method", "description"]
        )
    filtered = filtered.rename(columns={"payment_method_name": "payment_method"})
    filtered = filtered[["date", "merchant", "amount", "currency", "payment_method", "description"]]
    filtered = filtered.sort_values("date", ascending=False)
    return filtered


def get_monthly_spending_trend(start_date: date, end_date: date) -> pd.DataFrame:
    df = _fetch_all_transactions()
    if df.empty:
        return pd.DataFrame(columns=["month", "currency", "total"])
    mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    filtered = df[mask].copy()
    if filtered.empty:
        return pd.DataFrame(columns=["month", "currency", "total"])
    filtered["month"] = filtered["date"].apply(lambda d: d.strftime("%Y-%m"))
    grouped = (
        filtered.groupby(["month", "currency"])["amount"]
        .sum()
        .reset_index()
        .rename(columns={"amount": "total"})
        .sort_values("month")
    )
    return grouped


def get_category_trend(start_date: date, end_date: date) -> pd.DataFrame:
    df = _fetch_all_transactions()
    if df.empty:
        return pd.DataFrame(columns=["month", "category", "total"])
    mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    filtered = df[mask].copy()
    if filtered.empty:
        return pd.DataFrame(columns=["month", "category", "total"])
    filtered["month"] = filtered["date"].apply(lambda d: d.strftime("%Y-%m"))
    grouped = (
        filtered.groupby(["month", "category_name"])["amount"]
        .sum()
        .reset_index()
        .rename(columns={"category_name": "category", "amount": "total"})
        .sort_values("month")
    )
    return grouped


def get_reconciliation_summary(year: int, month: int) -> dict[str, pd.DataFrame]:
    txn_df = _fetch_all_transactions()
    sl_df = _fetch_all_statement_lines()

    if not txn_df.empty:
        tx_mask = txn_df["date"].apply(lambda d: d.year == year and d.month == month)
        tx_out = (
            txn_df[tx_mask]
            .groupby("reconciliation_status")
            .size()
            .reset_index(name="cnt")
        )
    else:
        tx_out = pd.DataFrame(columns=["reconciliation_status", "cnt"])

    billing = f"{year:04d}-{month:02d}"
    if not sl_df.empty:
        sl_mask = sl_df["billing_period"] == billing
        sl_out = (
            sl_df[sl_mask]
            .groupby("reconciliation_status")
            .size()
            .reset_index(name="cnt")
        )
    else:
        sl_out = pd.DataFrame(columns=["reconciliation_status", "cnt"])

    return {"transactions": tx_out, "statement_lines": sl_out}


def get_pending_statement_lines(year: int, month: int) -> pd.DataFrame:
    sl_df = _fetch_all_statement_lines()
    if sl_df.empty:
        return pd.DataFrame(
            columns=["date", "description", "amount", "account", "reconciliation_status"]
        )
    billing = f"{year:04d}-{month:02d}"
    mask = (sl_df["billing_period"] == billing) & (sl_df["reconciliation_status"] == "pending")
    filtered = sl_df[mask].copy()
    if filtered.empty:
        return pd.DataFrame(
            columns=["date", "description", "amount", "account", "reconciliation_status"]
        )
    accounts = _fetch_accounts()
    filtered["account"] = filtered["account_id"].apply(lambda a: accounts.get(int(a), "?"))
    return (
        filtered[["date", "description", "amount", "account", "reconciliation_status"]]
        .sort_values("date")
    )


def get_unreconciled_transactions(year: int, month: int) -> pd.DataFrame:
    df = _fetch_all_transactions()
    if df.empty:
        return pd.DataFrame(
            columns=["date", "merchant", "amount", "currency", "payment_method", "reconciliation_status"]
        )
    mask = (
        df["date"].apply(lambda d: d.year == year and d.month == month)
        & (df["reconciliation_status"] == "unreconciled")
    )
    filtered = df[mask].copy()
    if filtered.empty:
        return pd.DataFrame(
            columns=["date", "merchant", "amount", "currency", "payment_method", "reconciliation_status"]
        )
    filtered = filtered.rename(columns={"payment_method_name": "payment_method"})
    return (
        filtered[["date", "merchant", "amount", "currency", "payment_method", "reconciliation_status"]]
        .sort_values("date")
    )


# ---------------------------------------------------------------------------
# Richer readers used by the Transactions / Upload / Manual Reconciliation pages
# ---------------------------------------------------------------------------


def list_transactions(
    start_date: date | None = None,
    end_date: date | None = None,
    categories: list[str] | None = None,
    payment_methods: list[str] | None = None,
    status: str | None = None,
    search: str | None = None,
    account_ids: list[int] | None = None,
) -> pd.DataFrame:
    """Flat, filtered transactions DataFrame for the Transactions page.

    ``status`` accepts "all", "reconciled", or "unreconciled". ``search`` does
    a case-insensitive substring match over merchant and description.
    """
    df = _fetch_all_transactions()
    if df.empty:
        return df

    mask = pd.Series(True, index=df.index)
    if start_date is not None:
        mask &= df["date"] >= start_date
    if end_date is not None:
        mask &= df["date"] <= end_date
    if categories:
        mask &= df["category_name"].isin(categories)
    if payment_methods:
        mask &= df["payment_method_name"].isin(payment_methods)
    if account_ids:
        mask &= df["account_id"].isin(account_ids)
    if status and status != "all":
        mask &= df["reconciliation_status"] == status
    if search:
        needle = search.strip().lower()
        if needle:
            merchant = df["merchant"].fillna("").str.lower()
            description = df["description"].fillna("").str.lower()
            mask &= merchant.str.contains(needle, regex=False) | description.str.contains(
                needle, regex=False
            )

    filtered = df[mask].copy().sort_values(["date", "id"], ascending=[False, False])
    return filtered


def list_statement_lines(
    account_id: int | None = None,
    billing_period: str | None = None,
    status: str | None = None,
) -> pd.DataFrame:
    """Statement lines filtered by account/period/status."""
    df = _fetch_all_statement_lines()
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if account_id is not None:
        mask &= df["account_id"] == int(account_id)
    if billing_period:
        mask &= df["billing_period"] == billing_period
    if status and status != "all":
        mask &= df["reconciliation_status"] == status
    return df[mask].copy().sort_values(["date", "id"])


def list_unreconciled_transactions_flex(
    account_id: int | None = None,
    signed_amount: float | None = None,
    date_center: date | None = None,
    tolerance_days: int | None = None,
    amount_tolerance_pct: float | None = None,
) -> pd.DataFrame:
    """Flexible unreconciled-transaction search for manual reconciliation.

    Unlike ``get_unreconciled_transactions`` (which takes year+month),
    this accepts an optional amount and date-centered window so the manual
    reconciliation page can widen search beyond exact-cents / small windows.
    All filters are optional; if everything is None, returns every
    unreconciled transaction.
    """
    df = _fetch_all_transactions()
    if df.empty:
        return df

    mask = df["reconciliation_status"] == "unreconciled"

    if account_id is not None:
        mask &= df["account_id"] == int(account_id)

    if date_center is not None and tolerance_days is not None:
        from datetime import timedelta as _td

        date_from = date_center - _td(days=int(tolerance_days))
        date_to = date_center + _td(days=int(tolerance_days))
        mask &= (df["date"] >= date_from) & (df["date"] <= date_to)

    if signed_amount is not None:
        if amount_tolerance_pct and amount_tolerance_pct > 0:
            tol = abs(float(signed_amount)) * (amount_tolerance_pct / 100.0)
            lo = float(signed_amount) - tol
            hi = float(signed_amount) + tol
            if lo > hi:
                lo, hi = hi, lo
            mask &= df["amount"].between(lo, hi)
        else:
            mask &= df["amount"].round(2) == round(float(signed_amount), 2)

    return df[mask].copy().sort_values(["date", "id"])


def get_match_for_line(line_id: str) -> dict | None:
    """Return the current match row for a statement line, if any.

    Uses a direct query against the MATCH partition (not the dashboard's
    cached transaction scan) so it always reflects the latest state.
    """
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq("MATCH")
        & Key("SK").begins_with(f"STMT#{line_id}#"),
    )
    items = resp.get("Items", [])
    if not items:
        return None
    raw = items[0]
    return {
        "statement_line_id": raw.get("statement_line_id"),
        "transaction_id": int(raw.get("transaction_id", 0)),
        "verdict": raw.get("verdict"),
        "confirmed_by": raw.get("confirmed_by"),
        "created_at": raw.get("created_at"),
    }


def list_matches_for_period(
    account_id: int,
    billing_period: str,
) -> pd.DataFrame:
    """All match rows joined with line + txn metadata for a given period."""
    lines = list_statement_lines(account_id=account_id, billing_period=billing_period)
    if lines.empty:
        return pd.DataFrame(
            columns=[
                "line_id", "line_date", "line_description", "amount",
                "txn_id", "txn_date", "merchant", "category_name",
                "payment_method_name", "verdict", "confirmed_by",
            ]
        )
    matches_rows: list[dict] = []
    txn_df = _fetch_all_transactions()
    txn_by_id = {int(r["id"]): r for _, r in txn_df.iterrows()} if not txn_df.empty else {}

    for _, line in lines.iterrows():
        match = get_match_for_line(str(line["id"]))
        if not match:
            continue
        txn = txn_by_id.get(int(match["transaction_id"]))
        matches_rows.append(
            {
                "line_id": line["id"],
                "line_date": line["date"],
                "line_description": line["description"],
                "amount": line["amount"],
                "txn_id": match["transaction_id"],
                "txn_date": txn["date"] if txn is not None else None,
                "merchant": txn.get("merchant") if txn is not None else None,
                "category_name": txn.get("category_name") if txn is not None else None,
                "payment_method_name": (
                    txn.get("payment_method_name") if txn is not None else None
                ),
                "verdict": match.get("verdict"),
                "confirmed_by": match.get("confirmed_by"),
            }
        )
    return pd.DataFrame(matches_rows)


def accounts_detailed() -> list[dict[str, Any]]:
    return _fetch_accounts_detailed()


def payment_methods_detailed() -> list[dict[str, Any]]:
    return _fetch_payment_methods_detailed()


def categories_detailed() -> list[dict[str, Any]]:
    return _fetch_categories_detailed()


def billing_periods_for_account(account_id: int) -> list[str]:
    """Distinct billing periods that have at least one statement line for
    the given account, newest first."""
    df = _fetch_all_statement_lines()
    if df.empty:
        return []
    filtered = df[df["account_id"] == int(account_id)]
    return sorted(set(filtered["billing_period"].dropna().tolist()), reverse=True)
