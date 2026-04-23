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


def _fetch_all_transactions() -> pd.DataFrame:
    items = _query_all(KeyConditionExpression=Key("PK").eq("TXN"))
    if not items:
        return pd.DataFrame(
            columns=[
                "id", "date", "merchant", "description", "amount",
                "currency", "category_name", "payment_method_name",
                "reconciliation_status", "account_id",
            ]
        )
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
                "payment_method_name": item.get("payment_method_name"),
                "reconciliation_status": item.get("reconciliation_status"),
                "account_id": item.get("account_id"),
            }
        )
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


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
        return pd.DataFrame(
            columns=[
                "id", "account_id", "billing_period", "date",
                "description", "amount", "reconciliation_status",
            ]
        )
    rows = []
    for item in items:
        rows.append(
            {
                "id": item.get("id"),
                "account_id": item.get("account_id"),
                "billing_period": item.get("billing_period"),
                "date": item.get("date"),
                "description": item.get("description"),
                "amount": _to_float(item.get("amount")),
                "reconciliation_status": item.get("reconciliation_status"),
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
