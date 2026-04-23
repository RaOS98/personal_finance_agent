"""Personal Finance Dashboard - Streamlit Application."""

import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import date, timedelta
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard import dynamo_reader as reader

# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Personal Finance", page_icon="$", layout="wide")

COLOR_SEQUENCE = px.colors.qualitative.Set2


# ---------------------------------------------------------------------------
# Cached query functions (thin wrappers over dynamo_reader)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def get_monthly_totals(year: int, month: int) -> pd.DataFrame:
    return reader.get_monthly_totals(year, month)


@st.cache_data(ttl=60)
def get_spending_by_category(year: int, month: int) -> pd.DataFrame:
    return reader.get_spending_by_category(year, month)


@st.cache_data(ttl=60)
def get_spending_by_payment_method(year: int, month: int) -> pd.DataFrame:
    return reader.get_spending_by_payment_method(year, month)


@st.cache_data(ttl=60)
def get_categories() -> list[str]:
    return reader.get_categories()


@st.cache_data(ttl=60)
def get_payment_methods() -> list[str]:
    return reader.get_payment_methods()


@st.cache_data(ttl=60)
def get_category_transactions(
    category: str,
    start_date: date,
    end_date: date,
    payment_method: str | None = None,
) -> pd.DataFrame:
    return reader.get_category_transactions(category, start_date, end_date, payment_method)


@st.cache_data(ttl=60)
def get_monthly_spending_trend(start_date: date, end_date: date) -> pd.DataFrame:
    return reader.get_monthly_spending_trend(start_date, end_date)


@st.cache_data(ttl=60)
def get_category_trend(start_date: date, end_date: date) -> pd.DataFrame:
    return reader.get_category_trend(start_date, end_date)


@st.cache_data(ttl=60)
def get_reconciliation_summary(year: int, month: int) -> dict:
    return reader.get_reconciliation_summary(year, month)


@st.cache_data(ttl=60)
def get_pending_statement_lines(year: int, month: int) -> pd.DataFrame:
    return reader.get_pending_statement_lines(year, month)


@st.cache_data(ttl=60)
def get_unreconciled_transactions(year: int, month: int) -> pd.DataFrame:
    return reader.get_unreconciled_transactions(year, month)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_currency(amount: float, currency: str) -> str:
    if currency == "PEN":
        return f"S/. {amount:,.2f}"
    return f"$ {amount:,.2f}"


def pct_change(current: float, previous: float) -> str:
    if previous == 0:
        return "N/A"
    change = ((current - previous) / previous) * 100
    return f"{change:+.1f}%"


# ---------------------------------------------------------------------------
# Page: Monthly Summary
# ---------------------------------------------------------------------------

def page_monthly_summary():
    st.title("Monthly Summary")

    today = date.today()
    col_y, col_m = st.columns(2)
    with col_y:
        year = st.selectbox("Year", range(today.year, today.year - 5, -1), index=0)
    with col_m:
        month = st.selectbox(
            "Month",
            range(1, 13),
            index=today.month - 1,
            format_func=lambda m: date(2000, m, 1).strftime("%B"),
        )

    # Current month data
    totals = get_monthly_totals(year, month)

    # Previous month for comparison
    if month == 1:
        prev_y, prev_m = year - 1, 12
    else:
        prev_y, prev_m = year, month - 1
    prev_totals = get_monthly_totals(prev_y, prev_m)

    def _total_for(df: pd.DataFrame, cur: str) -> float:
        row = df.loc[df["currency"] == cur]
        return float(row["total"].iloc[0]) if not row.empty else 0.0

    def _count_for(df: pd.DataFrame) -> int:
        return int(df["txn_count"].sum()) if not df.empty else 0

    pen_total = _total_for(totals, "PEN")
    usd_total = _total_for(totals, "USD")
    pen_prev = _total_for(prev_totals, "PEN")
    usd_prev = _total_for(prev_totals, "USD")
    txn_count = _count_for(totals)
    txn_prev = _count_for(prev_totals)

    # KPI row
    k1, k2, k3 = st.columns(3)
    k1.metric("Total Spent (PEN)", fmt_currency(pen_total, "PEN"), pct_change(pen_total, pen_prev))
    k2.metric("Total Spent (USD)", fmt_currency(usd_total, "USD"), pct_change(usd_total, usd_prev))
    k3.metric("Transactions", txn_count, pct_change(txn_count, txn_prev))

    st.divider()

    # Charts
    c1, c2 = st.columns(2)

    with c1:
        st.subheader("Spending by Category")
        cat_df = get_spending_by_category(year, month)
        if cat_df.empty:
            st.info("No transactions this month.")
        else:
            fig = px.bar(
                cat_df,
                y="category",
                x="total",
                color="currency",
                orientation="h",
                color_discrete_sequence=COLOR_SEQUENCE,
                labels={"total": "Amount", "category": ""},
            )
            fig.update_layout(yaxis={"categoryorder": "total ascending"}, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, use_container_width=True)

    with c2:
        st.subheader("Spending by Payment Method")
        pm_df = get_spending_by_payment_method(year, month)
        if pm_df.empty:
            st.info("No transactions this month.")
        else:
            fig = px.bar(
                pm_df,
                y="payment_method",
                x="total",
                color="currency",
                orientation="h",
                color_discrete_sequence=COLOR_SEQUENCE,
                labels={"total": "Amount", "payment_method": ""},
            )
            fig.update_layout(yaxis={"categoryorder": "total ascending"}, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Page: Category Breakdown
# ---------------------------------------------------------------------------

def page_category_breakdown():
    st.title("Category Breakdown")

    categories = get_categories()
    if not categories:
        st.warning("No categories found in the database.")
        return

    category = st.selectbox("Category", categories)

    col_start, col_end = st.columns(2)
    today = date.today()
    with col_start:
        start_date = st.date_input("Start date", today.replace(day=1))
    with col_end:
        end_date = st.date_input("End date", today)

    payment_methods = get_payment_methods()
    pm_filter = st.selectbox("Payment Method", ["All"] + payment_methods)
    pm_value = None if pm_filter == "All" else pm_filter

    df = get_category_transactions(category, start_date, end_date, pm_value)

    if df.empty:
        st.info("No transactions match the selected filters.")
        return

    total_pen = df.loc[df["currency"] == "PEN", "amount"].sum()
    total_usd = df.loc[df["currency"] == "USD", "amount"].sum()

    m1, m2, m3 = st.columns(3)
    m1.metric("Transactions", len(df))
    m2.metric("Total (PEN)", fmt_currency(float(total_pen), "PEN"))
    m3.metric("Total (USD)", fmt_currency(float(total_usd), "USD"))

    st.divider()

    display = df[["date", "merchant", "amount", "currency", "payment_method"]].copy()
    display["amount"] = display.apply(lambda r: fmt_currency(float(r["amount"]), r["currency"]), axis=1)
    display.columns = ["Date", "Merchant", "Amount", "Currency", "Payment Method"]
    st.dataframe(display, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Page: Trends
# ---------------------------------------------------------------------------

def page_trends():
    st.title("Spending Trends")

    today = date.today()
    default_start = today - timedelta(days=365)

    col_s, col_e = st.columns(2)
    with col_s:
        start_date = st.date_input("From", default_start, key="trend_start")
    with col_e:
        end_date = st.date_input("To", today, key="trend_end")

    # Monthly total
    st.subheader("Monthly Spending Over Time")
    trend_df = get_monthly_spending_trend(start_date, end_date)
    if trend_df.empty:
        st.info("No transactions in the selected range.")
    else:
        fig = px.line(
            trend_df,
            x="month",
            y="total",
            color="currency",
            markers=True,
            color_discrete_sequence=COLOR_SEQUENCE,
            labels={"total": "Amount", "month": "Month"},
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # Per-category trends
    st.subheader("Per-Category Trends")
    cat_trend = get_category_trend(start_date, end_date)
    if cat_trend.empty:
        st.info("No transactions in the selected range.")
    else:
        fig = px.line(
            cat_trend,
            x="month",
            y="total",
            color="category",
            markers=True,
            color_discrete_sequence=COLOR_SEQUENCE,
            labels={"total": "Amount", "month": "Month", "category": "Category"},
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Page: Reconciliation Status
# ---------------------------------------------------------------------------

def page_reconciliation():
    st.title("Reconciliation Status")

    today = date.today()
    col_y, col_m = st.columns(2)
    with col_y:
        year = st.selectbox("Year", range(today.year, today.year - 5, -1), index=0, key="rec_y")
    with col_m:
        month = st.selectbox(
            "Month",
            range(1, 13),
            index=today.month - 1,
            format_func=lambda m: date(2000, m, 1).strftime("%B"),
            key="rec_m",
        )

    summary = get_reconciliation_summary(year, month)

    # Transaction reconciliation metrics
    st.subheader("Transactions")
    txn_df = summary["transactions"]
    reconciled = 0
    unreconciled = 0
    if not txn_df.empty:
        for _, row in txn_df.iterrows():
            if row["reconciliation_status"] == "reconciled":
                reconciled = int(row["cnt"])
            else:
                unreconciled = int(row["cnt"])

    t1, t2, t3 = st.columns(3)
    t1.metric("Reconciled", reconciled)
    t2.metric("Unreconciled", unreconciled)
    t3.metric("Total", reconciled + unreconciled)

    st.divider()

    # Statement line metrics
    st.subheader("Statement Lines")
    sl_df = summary["statement_lines"]
    sl_counts = {"matched": 0, "added": 0, "skipped": 0, "pending": 0}
    if not sl_df.empty:
        for _, row in sl_df.iterrows():
            status = row["reconciliation_status"]
            if status in sl_counts:
                sl_counts[status] = int(row["cnt"])

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Matched", sl_counts["matched"])
    s2.metric("Added", sl_counts["added"])
    s3.metric("Skipped", sl_counts["skipped"])
    s4.metric("Pending", sl_counts["pending"])

    st.divider()

    # Pending / unmatched tables
    st.subheader("Pending Statement Lines")
    pending = get_pending_statement_lines(year, month)
    if pending.empty:
        st.success("No pending statement lines.")
    else:
        st.dataframe(pending, use_container_width=True, hide_index=True)

    st.subheader("Unreconciled Transactions")
    unrec = get_unreconciled_transactions(year, month)
    if unrec.empty:
        st.success("All transactions reconciled.")
    else:
        st.dataframe(unrec, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

PAGES = {
    "Monthly Summary": page_monthly_summary,
    "Category Breakdown": page_category_breakdown,
    "Trends": page_trends,
    "Reconciliation Status": page_reconciliation,
}

st.sidebar.title("Personal Finance")
selection = st.sidebar.selectbox("Navigate", list(PAGES.keys()))
PAGES[selection]()
