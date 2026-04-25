"""Personal Finance Dashboard - Streamlit Application."""

import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import date, timedelta
from typing import Any
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard import dynamo_reader as reader
from dashboard import dynamo_writer as writer
import s3_store  # noqa: E402
from agent import reconciliation as reconciliation_agent  # noqa: E402
from agent import statement_parser  # noqa: E402
from agent import reconciler  # noqa: E402
from db import dynamo as db  # noqa: E402

# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Personal Finance", page_icon="$", layout="wide")


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

def _require_auth() -> None:
    """Simple shared-password gate backed by .streamlit/secrets.toml.

    If no password is configured in secrets, the gate stays open (useful for
    running the dashboard on a trusted single-user machine). When a password
    is configured, the app halts here until the user enters it correctly.
    """
    configured = ""
    try:
        configured = st.secrets.get("dashboard_password", "")
    except Exception:
        configured = ""

    if not configured:
        return

    if st.session_state.get("auth_ok"):
        return

    st.title("Personal Finance")
    pwd = st.text_input("Password", type="password", key="_auth_pwd")
    submit = st.button("Sign in", type="primary")
    if submit:
        if pwd == configured:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password")
    st.stop()


_require_auth()

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


@st.cache_data(ttl=60)
def list_transactions(
    start_date: date | None,
    end_date: date | None,
    categories: tuple[str, ...],
    payment_methods: tuple[str, ...],
    status: str,
    search: str,
) -> pd.DataFrame:
    return reader.list_transactions(
        start_date=start_date,
        end_date=end_date,
        categories=list(categories) or None,
        payment_methods=list(payment_methods) or None,
        status=status,
        search=search or None,
    )


@st.cache_data(ttl=60)
def list_statement_lines(
    account_id: int | None,
    billing_period: str | None,
    status: str | None,
) -> pd.DataFrame:
    return reader.list_statement_lines(
        account_id=account_id,
        billing_period=billing_period,
        status=status,
    )


@st.cache_data(ttl=60)
def list_unreconciled_transactions_flex(
    account_id: int | None,
    signed_amount: float | None,
    date_center: date | None,
    tolerance_days: int | None,
    amount_tolerance_pct: float | None,
) -> pd.DataFrame:
    return reader.list_unreconciled_transactions_flex(
        account_id=account_id,
        signed_amount=signed_amount,
        date_center=date_center,
        tolerance_days=tolerance_days,
        amount_tolerance_pct=amount_tolerance_pct,
    )


@st.cache_data(ttl=60)
def get_accounts_detailed() -> list[dict]:
    return reader.accounts_detailed()


@st.cache_data(ttl=60)
def get_categories_detailed() -> list[dict]:
    return reader.categories_detailed()


@st.cache_data(ttl=60)
def get_payment_methods_detailed() -> list[dict]:
    return reader.payment_methods_detailed()


@st.cache_data(ttl=60)
def get_billing_periods(account_id: int) -> list[str]:
    return reader.billing_periods_for_account(account_id)


@st.cache_data(ttl=60)
def get_match_for_line(line_id: str) -> dict | None:
    return reader.get_match_for_line(line_id)


@st.cache_data(ttl=60)
def list_matches_for_period(account_id: int, billing_period: str) -> pd.DataFrame:
    return reader.list_matches_for_period(account_id, billing_period)


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
# Page: Transactions (filter + inline edit)
# ---------------------------------------------------------------------------

_TXN_EDITOR_KEY = "txn_editor"


def _build_txn_lookup_maps() -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (category_by_name, payment_method_by_name) for dropdown
    resolution during edits. Names are assumed unique within each vocabulary,
    which has been true for the user's current seed data."""
    cats = get_categories_detailed()
    pms = get_payment_methods_detailed()
    return (
        {c["name"]: c for c in cats if c.get("name")},
        {pm["name"]: pm for pm in pms if pm.get("name")},
    )


def _apply_txn_edits(
    edited_rows: dict[int, dict],
    snapshot: pd.DataFrame,
    category_by_name: dict[str, dict],
    pm_by_name: dict[str, dict],
) -> tuple[int, list[str]]:
    """Given the edited_rows emitted by st.data_editor, persist each delta.

    Returns (number_of_updates_applied, list_of_error_messages).
    """
    if not edited_rows:
        return 0, []

    applied = 0
    errors: list[str] = []

    for row_index, edits in edited_rows.items():
        try:
            base_row = snapshot.iloc[int(row_index)]
        except Exception:
            errors.append(f"Row {row_index} out of range; skipping edit.")
            continue

        txn_id = int(base_row["id"])
        fields: dict[str, Any] = {}

        for col, new_value in edits.items():
            if col == "category_name":
                if not new_value:
                    errors.append(f"Transaction {txn_id}: category cannot be empty.")
                    continue
                cat = category_by_name.get(new_value)
                if cat is None:
                    errors.append(
                        f"Transaction {txn_id}: unknown category '{new_value}'."
                    )
                    continue
                fields["category_id"] = int(cat["id"])
                fields["category_slug"] = cat.get("slug") or ""
                fields["category_name"] = cat["name"]
            elif col == "payment_method_name":
                if not new_value:
                    errors.append(
                        f"Transaction {txn_id}: payment method cannot be empty."
                    )
                    continue
                pm = pm_by_name.get(new_value)
                if pm is None:
                    errors.append(
                        f"Transaction {txn_id}: unknown payment method '{new_value}'."
                    )
                    continue
                fields["payment_method_id"] = int(pm["id"])
                fields["payment_method_name"] = pm["name"]
            elif col == "amount":
                try:
                    fields["amount"] = float(new_value)
                except (TypeError, ValueError):
                    errors.append(
                        f"Transaction {txn_id}: amount '{new_value}' is not numeric."
                    )
                    continue
            elif col == "date":
                if isinstance(new_value, (pd.Timestamp,)):
                    fields["date"] = new_value.date().isoformat()
                elif isinstance(new_value, date):
                    fields["date"] = new_value.isoformat()
                elif isinstance(new_value, str) and new_value:
                    fields["date"] = new_value
                else:
                    errors.append(
                        f"Transaction {txn_id}: date '{new_value}' is not valid."
                    )
                    continue
            elif col in {"merchant", "description"}:
                fields[col] = "" if new_value is None else str(new_value)
            else:
                errors.append(
                    f"Transaction {txn_id}: column '{col}' is not editable."
                )

        if not fields:
            continue

        try:
            writer.update_transaction(txn_id, fields)
            applied += 1
        except Exception as exc:
            logger_errors = str(exc) or exc.__class__.__name__
            errors.append(f"Transaction {txn_id}: update failed — {logger_errors}")

    return applied, errors


def page_transactions():
    st.title("Transactions")
    st.warning(
        "Edits here are live. Changes save directly to DynamoDB when you "
        "click **Save changes**.",
        icon="⚠️",
    )

    today = date.today()
    category_names = get_categories()
    payment_method_names = get_payment_methods()

    with st.container():
        col_dates, col_status, col_search = st.columns([2, 1, 2])
        with col_dates:
            date_range = st.date_input(
                "Date range",
                value=(today.replace(day=1), today),
                key="txn_date_range",
            )
        with col_status:
            status = st.selectbox(
                "Status",
                ["all", "reconciled", "unreconciled"],
                index=0,
                key="txn_status",
            )
        with col_search:
            search = st.text_input(
                "Search merchant / description",
                "",
                key="txn_search",
            )

        col_cat, col_pm = st.columns(2)
        with col_cat:
            selected_categories = st.multiselect(
                "Categories",
                category_names,
                default=[],
                key="txn_categories",
            )
        with col_pm:
            selected_pms = st.multiselect(
                "Payment methods",
                payment_method_names,
                default=[],
                key="txn_payment_methods",
            )

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    elif isinstance(date_range, date):
        start_date = end_date = date_range
    else:
        start_date = end_date = None

    df = list_transactions(
        start_date=start_date,
        end_date=end_date,
        categories=tuple(selected_categories),
        payment_methods=tuple(selected_pms),
        status=status,
        search=search,
    )

    if df.empty:
        st.info("No transactions match the selected filters.")
        return

    editable_cols = [
        "id", "date", "merchant", "description", "amount", "currency",
        "category_name", "payment_method_name", "reconciliation_status",
        "image_path",
    ]
    editable_df = df[editable_cols].copy().reset_index(drop=True)

    snapshot = editable_df.copy()

    category_options = sorted({c["name"] for c in get_categories_detailed() if c.get("name")})
    pm_options = sorted({pm["name"] for pm in get_payment_methods_detailed() if pm.get("name")})

    edited = st.data_editor(
        editable_df,
        key=_TXN_EDITOR_KEY,
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        disabled=["id", "currency", "reconciliation_status", "image_path"],
        column_config={
            "id": st.column_config.NumberColumn("ID", disabled=True),
            "date": st.column_config.DateColumn("Date"),
            "merchant": st.column_config.TextColumn("Merchant"),
            "description": st.column_config.TextColumn("Description"),
            "amount": st.column_config.NumberColumn("Amount", format="%.2f"),
            "currency": st.column_config.TextColumn("Currency"),
            "category_name": st.column_config.SelectboxColumn(
                "Category", options=category_options, required=True
            ),
            "payment_method_name": st.column_config.SelectboxColumn(
                "Payment Method", options=pm_options, required=True
            ),
            "reconciliation_status": st.column_config.TextColumn("Status"),
            "image_path": st.column_config.TextColumn("Image key"),
        },
    )

    col_save, col_stats = st.columns([1, 3])
    with col_save:
        save_clicked = st.button("Save changes", type="primary")
    with col_stats:
        editor_state = st.session_state.get(_TXN_EDITOR_KEY, {})
        pending_edits = editor_state.get("edited_rows") or {}
        if pending_edits:
            st.caption(f"Unsaved edits: {len(pending_edits)} row(s).")
        else:
            st.caption("No pending edits.")

    if save_clicked:
        category_by_name, pm_by_name = _build_txn_lookup_maps()
        applied, errors = _apply_txn_edits(
            pending_edits,
            snapshot,
            category_by_name,
            pm_by_name,
        )
        if applied:
            st.success(f"Saved {applied} transaction(s).")
        if errors:
            st.error("Some edits failed:\n\n- " + "\n- ".join(errors))
        if applied:
            st.rerun()
        return

    with st.expander("Receipt previews (click a row's image key to view)"):
        rows_with_images = edited[edited["image_path"].notna() & (edited["image_path"] != "")]
        if rows_with_images.empty:
            st.caption("No receipts attached to the filtered transactions.")
        else:
            pick = st.selectbox(
                "Transaction",
                options=rows_with_images["id"].tolist(),
                format_func=lambda txn_id: (
                    f"#{txn_id} — "
                    f"{rows_with_images.loc[rows_with_images['id']==txn_id, 'merchant'].iloc[0]} "
                    f"{rows_with_images.loc[rows_with_images['id']==txn_id, 'date'].iloc[0]}"
                ),
                key="txn_receipt_pick",
            )
            row = rows_with_images.loc[rows_with_images["id"] == pick].iloc[0]
            try:
                url = s3_store.presigned_url(row["image_path"])
                st.image(url, caption=f"Receipt for #{row['id']}", use_container_width=True)
            except Exception as exc:
                st.error(f"Could not load receipt: {exc}")


# ---------------------------------------------------------------------------
# Page: Upload Statement
# ---------------------------------------------------------------------------

_UPLOAD_PREVIEW_KEY = "upload_preview"
_UPLOAD_PDF_KEY = "upload_pdf_bytes"
_UPLOAD_FILE_NAME_KEY = "upload_file_name"
_UPLOAD_LAST_SAVE_KEY = "upload_last_save"


def _current_billing_period() -> tuple[int, int]:
    today = date.today()
    return today.year, today.month


def page_upload_statement():
    st.title("Upload Bank Statement")
    st.caption(
        "Upload a BCP statement PDF, review the parsed lines, and commit "
        "them. Auto-reconciliation is optional and can be rerun from the "
        "Manual Reconciliation page."
    )

    accounts = get_accounts_detailed()
    if not accounts:
        st.error("No accounts are configured. Seed reference data first.")
        return

    account_labels = {a["id"]: f"{a['name']} ({a.get('currency') or '?'})" for a in accounts}
    cur_year, cur_month = _current_billing_period()

    col_acct, col_year, col_month = st.columns([2, 1, 1])
    with col_acct:
        account_id = st.selectbox(
            "Account",
            options=list(account_labels.keys()),
            format_func=lambda a: account_labels[a],
            key="upload_account_id",
        )
    with col_year:
        year = st.selectbox(
            "Year",
            list(range(cur_year, cur_year - 5, -1)),
            index=0,
            key="upload_year",
        )
    with col_month:
        month = st.selectbox(
            "Month",
            list(range(1, 13)),
            index=cur_month - 1,
            format_func=lambda m: date(2000, m, 1).strftime("%B"),
            key="upload_month",
        )

    billing_period = f"{year:04d}-{month:02d}"

    uploaded = st.file_uploader("Bank statement PDF", type=["pdf"])

    if uploaded is not None:
        pdf_bytes = uploaded.getvalue()
        if st.session_state.get(_UPLOAD_FILE_NAME_KEY) != uploaded.name or not st.session_state.get(_UPLOAD_PREVIEW_KEY):
            with st.spinner("Parsing PDF..."):
                try:
                    lines = statement_parser.parse_statement_pdf(pdf_bytes)
                except ValueError as exc:
                    st.error(f"Could not parse PDF: {exc}")
                    return
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Unexpected parse error: {exc}")
                    return
            preview_df = pd.DataFrame(lines)
            preview_df["date"] = pd.to_datetime(preview_df["date"]).dt.date
            st.session_state[_UPLOAD_PREVIEW_KEY] = preview_df
            st.session_state[_UPLOAD_PDF_KEY] = pdf_bytes
            st.session_state[_UPLOAD_FILE_NAME_KEY] = uploaded.name
            st.session_state.pop(_UPLOAD_LAST_SAVE_KEY, None)

    preview_df = st.session_state.get(_UPLOAD_PREVIEW_KEY)
    if preview_df is None or preview_df.empty:
        st.info("Upload a PDF to preview the parsed lines.")
        return

    st.subheader(f"Parsed lines ({len(preview_df)})")
    st.caption(
        "Charges are positive, credits/refunds negative. Edit before saving; "
        "remove junk rows by flipping the amount to 0 — zero-amount rows are "
        "skipped on save."
    )

    edited = st.data_editor(
        preview_df,
        key="upload_editor",
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "date": st.column_config.DateColumn("Date", required=True),
            "description": st.column_config.TextColumn("Description"),
            "amount": st.column_config.NumberColumn("Amount (signed)", format="%.2f"),
        },
    )

    col_save, col_reset = st.columns([1, 3])
    with col_save:
        save_clicked = st.button("Save statement", type="primary")
    with col_reset:
        if st.button("Reset", help="Discard the current parse and start over"):
            for k in (_UPLOAD_PREVIEW_KEY, _UPLOAD_PDF_KEY, _UPLOAD_FILE_NAME_KEY, _UPLOAD_LAST_SAVE_KEY):
                st.session_state.pop(k, None)
            st.rerun()

    if save_clicked:
        pdf_bytes = st.session_state.get(_UPLOAD_PDF_KEY)
        if not pdf_bytes:
            st.error("Missing PDF bytes. Re-upload the file.")
            return

        records = []
        for _, row in edited.iterrows():
            amount = row.get("amount")
            if amount is None or pd.isna(amount) or float(amount) == 0:
                continue
            d = row.get("date")
            if isinstance(d, pd.Timestamp):
                date_iso = d.date().isoformat()
            elif isinstance(d, date):
                date_iso = d.isoformat()
            elif isinstance(d, str) and d:
                date_iso = d
            else:
                continue
            records.append(
                {
                    "date": date_iso,
                    "description": (row.get("description") or "Unknown"),
                    "amount": float(amount),
                }
            )

        if not records:
            st.warning("No non-zero lines to save.")
            return

        try:
            pdf_s3_key, inserted = writer.commit_statement(
                account_id=int(account_id),
                billing_period=billing_period,
                pdf_bytes=pdf_bytes,
                lines=records,
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Save failed: {exc}")
            return

        st.session_state[_UPLOAD_LAST_SAVE_KEY] = {
            "account_id": int(account_id),
            "billing_period": billing_period,
            "pdf_s3_key": pdf_s3_key,
            "inserted": inserted,
            "submitted_total": len(records),
        }
        st.success(
            f"Saved statement: {inserted} new line(s) inserted "
            f"(of {len(records)} submitted). Duplicates are skipped."
        )
        st.rerun()

    last_save = st.session_state.get(_UPLOAD_LAST_SAVE_KEY)
    if last_save:
        st.divider()
        st.subheader("Statement saved")
        try:
            pdf_url = s3_store.statement_pdf_url(last_save["pdf_s3_key"])
            st.markdown(f"[View uploaded PDF]({pdf_url})")
        except Exception as exc:  # noqa: BLE001
            st.caption(f"(Could not build PDF link: {exc})")
        st.caption(f"S3 key: `{last_save['pdf_s3_key']}`")

        st.subheader("Auto-reconcile")
        st.caption(
            "Matches confident singletons now; everything else will live in "
            "the Manual Reconciliation page."
        )
        if st.button("Auto-reconcile now"):
            progress_bar = st.progress(0.0, text="Starting...")
            events: list[dict] = []

            def _callback(done: int, total: int, event: dict | None) -> None:
                pct = 0.0 if total == 0 else done / total
                label = f"Reconciling {done}/{total}..."
                if event and event.get("status"):
                    label = f"{label} last: {event['status']}"
                progress_bar.progress(min(1.0, pct), text=label)
                if event is not None:
                    events.append(event)

            with st.spinner("Running auto-reconciliation..."):
                try:
                    result = reconciliation_agent.auto_reconcile(
                        account_id=last_save["account_id"],
                        billing_period=last_save["billing_period"],
                        progress_callback=_callback,
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Auto-reconcile failed: {exc}")
                    return

            writer.invalidate_cache()
            progress_bar.progress(1.0, text="Done.")

            auto_n = len(result["auto_matched"])
            review_n = len(result["needs_review"])
            unmatched_n = len(result["unmatched"])

            c1, c2, c3 = st.columns(3)
            c1.metric("Auto-matched", auto_n)
            c2.metric("Needs review", review_n)
            c3.metric("Unmatched", unmatched_n)

            if review_n or unmatched_n:
                st.info(
                    "Head to the **Manual Reconciliation** page to finish "
                    "matching the remaining lines."
                )
            else:
                st.success("Every line auto-matched. Nothing left to review.")


# ---------------------------------------------------------------------------
# Page: Manual Reconciliation
# ---------------------------------------------------------------------------

_SELECTED_LINE_KEY = "manual_recon_selected_line"
_CANDIDATE_CACHE_KEY = "manual_recon_candidate_cache"
_VERDICT_CACHE_KEY = "manual_recon_verdict_cache"


def _line_display_label(line: dict | pd.Series) -> str:
    return (
        f"{line['date']} | {line['description'][:40]:<40} | "
        f"{float(line['amount']):>10.2f} | {line['reconciliation_status']}"
    )


def _txn_display_label(txn: dict | pd.Series) -> str:
    merchant = txn.get("merchant") or ""
    return (
        f"#{int(txn['id'])} {txn['date']} | {merchant[:30]:<30} | "
        f"{float(txn['amount']):>10.2f} {txn.get('currency') or ''} "
        f"| {txn.get('payment_method_name') or '-'}"
    )


def _candidates_for_line(
    line: dict,
    tolerance_days: int,
    amount_tol_pct: float,
    ignore_account: bool,
) -> list[dict]:
    """Return unreconciled transactions that could match ``line``, using the
    widen-search knobs. If amount tolerance is 0, we hit the exact-cents path
    (fast GSI1 query). Otherwise we post-filter within a date window."""
    signed_amount = float(line["amount"])
    try:
        line_date = reconciliation_agent.coerce_date(line["date"])
    except Exception:
        line_date = None

    if amount_tol_pct <= 0.0001 and line_date is not None and not ignore_account:
        return db.find_reconciliation_candidates(
            account_id=int(line["account_id"]),
            amount=signed_amount,
            date_val=line_date,
            tolerance_days=int(tolerance_days),
        )

    df = list_unreconciled_transactions_flex(
        account_id=None if ignore_account else int(line["account_id"]),
        signed_amount=signed_amount,
        date_center=line_date,
        tolerance_days=tolerance_days,
        amount_tolerance_pct=amount_tol_pct,
    )
    if df.empty:
        return []
    return df.to_dict("records")


def _ask_agent(line: dict, candidates: list[dict]) -> list[dict]:
    return reconciler.evaluate_matches(
        statement_line={
            "date": str(line["date"]),
            "description": line.get("description", ""),
            "amount": float(line["amount"]),
        },
        candidates=[
            {
                "date": str(c["date"]),
                "merchant": c.get("merchant", ""),
                "amount": float(c["amount"]),
                "category": c.get("category_name", ""),
            }
            for c in candidates
        ],
    )


def _render_start_from_transaction(account_id: int, billing_period: str):
    with st.expander("Start from a transaction (find a matching statement line)"):
        unreconciled = list_unreconciled_transactions_flex(None, None, None, None, None)
        if unreconciled.empty:
            st.caption("No unreconciled transactions.")
            return
        st.caption(
            "Pick an unreconciled transaction to see statement lines in this "
            "period whose signed cents match."
        )
        pick_id = st.selectbox(
            "Unreconciled transactions",
            options=unreconciled["id"].tolist(),
            format_func=lambda i: _txn_display_label(
                unreconciled.loc[unreconciled["id"] == i].iloc[0]
            ),
            key="manual_recon_start_txn",
        )
        if pick_id is None:
            return
        txn_row = unreconciled.loc[unreconciled["id"] == pick_id].iloc[0]

        lines = list_statement_lines(account_id, billing_period, None)
        if lines.empty:
            st.caption("No statement lines for this period.")
            return

        matches = lines[
            lines["amount"].round(2) == round(float(txn_row["amount"]), 2)
        ]
        if matches.empty:
            st.info("No statement lines in this period with a matching amount.")
            return

        st.caption(f"Found {len(matches)} line(s) with matching amount.")
        for _, line in matches.iterrows():
            col_info, col_btn = st.columns([4, 1])
            with col_info:
                st.text(_line_display_label(line))
            with col_btn:
                if st.button(
                    "Match",
                    key=f"start_txn_match_{pick_id}_{line['id']}",
                ):
                    try:
                        writer.save_reconciliation(
                            line_id=str(line["id"]),
                            txn_id=int(pick_id),
                            verdict="user-confirmed",
                        )
                        st.success(f"Matched #{pick_id} ↔ {line['id']}")
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Match failed: {exc}")


def _render_candidate_panel(line: dict):
    st.subheader("Candidates")
    st.caption(
        f"For line **{line['id']}**: "
        f"{line['date']} · {line['description']} · "
        f"signed amount {float(line['amount']):+.2f}"
    )

    default_tol = 7
    col_tol, col_amt_tol, col_ignore = st.columns(3)
    with col_tol:
        tol_days = st.slider(
            "Date tolerance (days)",
            min_value=0,
            max_value=30,
            value=default_tol,
            key="manual_recon_tol_days",
        )
    with col_amt_tol:
        amt_tol_pct = st.slider(
            "Amount tolerance (%)",
            min_value=0.0,
            max_value=5.0,
            value=0.0,
            step=0.25,
            key="manual_recon_amt_tol",
        )
    with col_ignore:
        ignore_account = st.checkbox(
            "Search across all accounts",
            value=False,
            key="manual_recon_ignore_account",
        )

    candidates = _candidates_for_line(line, tol_days, amt_tol_pct, ignore_account)

    if not candidates:
        st.info("No candidate transactions found. Widen the search or add as new.")
    else:
        st.caption(f"{len(candidates)} candidate(s).")

        cache: dict = st.session_state.setdefault(_VERDICT_CACHE_KEY, {})
        verdict_key = (str(line["id"]), tuple(sorted(int(c["id"]) for c in candidates)))
        if st.button("Ask the agent", disabled=not candidates):
            with st.spinner("Asking Bedrock..."):
                try:
                    verdicts = _ask_agent(line, candidates)
                    cache[verdict_key] = verdicts
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Agent call failed: {exc}")

        verdicts = cache.get(verdict_key)
        if verdicts:
            for c, v in zip(candidates, verdicts):
                c["verdict"] = str(v.get("verdict") or "uncertain").strip().lower()
                c["reason"] = v.get("reason", "")

        st.divider()

        for idx, cand in enumerate(candidates):
            with st.container():
                col_main, col_btn = st.columns([4, 1])
                with col_main:
                    st.text(_txn_display_label(cand))
                    verdict = cand.get("verdict")
                    reason = cand.get("reason")
                    if verdict:
                        badge = {
                            "confident": "🟢",
                            "likely": "🟡",
                            "uncertain": "⚪",
                        }.get(verdict, "")
                        st.caption(f"{badge} **{verdict}** — {reason or ''}")
                with col_btn:
                    if st.button(
                        "Match",
                        key=f"manual_recon_match_{line['id']}_{cand['id']}_{idx}",
                    ):
                        try:
                            writer.save_reconciliation(
                                line_id=str(line["id"]),
                                txn_id=int(cand["id"]),
                                verdict=cand.get("verdict") or "user-confirmed",
                            )
                            st.session_state.pop(_SELECTED_LINE_KEY, None)
                            st.success("Match saved.")
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Save failed: {exc}")

    st.divider()
    col_skip, col_add = st.columns(2)
    with col_skip:
        if st.button("Skip this line"):
            try:
                writer.set_statement_line_status(str(line["id"]), "skipped")
                st.session_state.pop(_SELECTED_LINE_KEY, None)
                st.info("Line marked as skipped.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Skip failed: {exc}")
    with col_add:
        with st.popover("Add as new transaction"):
            _render_add_new_txn_form(line)


def _render_add_new_txn_form(line: dict):
    """Lightweight capture form; amount + date + currency come from the line
    so the user only picks merchant / category / payment method."""
    st.caption("Create a transaction from this statement line.")
    accounts = {a["id"]: a for a in get_accounts_detailed()}
    account = accounts.get(int(line["account_id"]))
    currency = account.get("currency", "PEN") if account else "PEN"
    st.text_input("Date", value=str(line["date"]), disabled=True)
    st.text_input(
        "Amount", value=f"{float(line['amount']):.2f} {currency}", disabled=True
    )
    merchant = st.text_input("Merchant", value=line.get("description", ""))

    categories = get_categories_detailed()
    pms = [pm for pm in get_payment_methods_detailed() if pm.get("account_id") == int(line["account_id"])]
    if not pms:
        pms = get_payment_methods_detailed()

    if not categories:
        st.error("No categories configured.")
        return
    if not pms:
        st.error("No payment methods configured.")
        return

    cat_map = {c["name"]: c for c in categories}
    pm_map = {pm["name"]: pm for pm in pms}
    cat_name = st.selectbox("Category", list(cat_map.keys()))
    pm_name = st.selectbox("Payment method", list(pm_map.keys()))
    description = st.text_input("Description", value="")

    if st.button("Create and match", type="primary"):
        cat = cat_map[cat_name]
        pm = pm_map[pm_name]
        try:
            txn = writer.create_transaction(
                amount=float(line["amount"]),
                currency=currency,
                date_val=reconciliation_agent.coerce_date(line["date"]),
                merchant=merchant or None,
                description=description or None,
                category_id=int(cat["id"]),
                payment_method_id=int(pm["id"]),
            )
            writer.save_reconciliation(
                line_id=str(line["id"]),
                txn_id=int(txn["id"]),
                verdict="user-added",
            )
            st.session_state.pop(_SELECTED_LINE_KEY, None)
            st.success(f"Created transaction #{txn['id']} and matched.")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Create + match failed: {exc}")


def page_manual_reconciliation():
    st.title("Manual Reconciliation")

    accounts = get_accounts_detailed()
    if not accounts:
        st.error("No accounts configured.")
        return

    account_labels = {a["id"]: f"{a['name']} ({a.get('currency') or '?'})" for a in accounts}
    col_acct, col_period = st.columns([2, 2])
    with col_acct:
        account_id = st.selectbox(
            "Account",
            options=list(account_labels.keys()),
            format_func=lambda a: account_labels[a],
            key="manual_recon_account",
        )
    periods = get_billing_periods(int(account_id))
    if not periods:
        st.info("No statement lines uploaded yet for this account.")
        return
    with col_period:
        billing_period = st.selectbox(
            "Billing period",
            periods,
            key="manual_recon_period",
        )

    pending_lines_df = list_statement_lines(int(account_id), billing_period, "pending")

    col_left, col_right = st.columns([3, 4])

    with col_left:
        st.subheader("Pending statement lines")
        if pending_lines_df.empty:
            st.success("No pending lines for this period.")
        else:
            display_cols = ["id", "date", "description", "amount", "reconciliation_status"]
            display_df = pending_lines_df[display_cols].copy()
            display_df["pdf"] = pending_lines_df["pdf_s3_key"].apply(
                lambda k: "Open PDF" if k else ""
            )
            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "id": st.column_config.TextColumn("Line ID", width="small"),
                    "amount": st.column_config.NumberColumn("Amount", format="%.2f"),
                },
            )

            selected_id = st.selectbox(
                "Select a line to reconcile",
                options=pending_lines_df["id"].tolist(),
                format_func=lambda lid: _line_display_label(
                    pending_lines_df.loc[pending_lines_df["id"] == lid].iloc[0]
                ),
                key=_SELECTED_LINE_KEY,
            )

            sel_row = pending_lines_df.loc[pending_lines_df["id"] == selected_id]
            if not sel_row.empty and sel_row.iloc[0].get("pdf_s3_key"):
                try:
                    pdf_url = s3_store.statement_pdf_url(sel_row.iloc[0]["pdf_s3_key"])
                    st.markdown(f"[Open original PDF]({pdf_url})")
                except Exception:
                    pass

    with col_right:
        selected_id = st.session_state.get(_SELECTED_LINE_KEY)
        if not pending_lines_df.empty and selected_id is not None:
            selected_rows = pending_lines_df.loc[pending_lines_df["id"] == selected_id]
            if not selected_rows.empty:
                _render_candidate_panel(selected_rows.iloc[0].to_dict())
        else:
            st.info("Select a pending line to see candidates.")

    st.divider()

    # -------------------------------------------------------------------
    # Reconciled view with Unmatch
    # -------------------------------------------------------------------
    st.subheader("Reconciled lines this period")
    matches_df = list_matches_for_period(int(account_id), billing_period)
    if matches_df.empty:
        st.caption("No matched lines yet.")
    else:
        display = matches_df[
            [
                "line_id", "line_date", "line_description", "amount",
                "txn_id", "merchant", "category_name", "payment_method_name",
                "verdict", "confirmed_by",
            ]
        ].copy()
        st.dataframe(
            display,
            use_container_width=True,
            hide_index=True,
            column_config={
                "amount": st.column_config.NumberColumn("Amount", format="%.2f"),
            },
        )

        to_unmatch_id = st.selectbox(
            "Unmatch a pairing",
            options=[""] + matches_df["line_id"].tolist(),
            format_func=lambda lid: (
                "(pick one)" if not lid
                else f"line {lid} ↔ txn #{int(matches_df.loc[matches_df['line_id']==lid, 'txn_id'].iloc[0])}"
            ),
            key="manual_recon_unmatch_pick",
        )
        if to_unmatch_id:
            row = matches_df.loc[matches_df["line_id"] == to_unmatch_id].iloc[0]
            if st.button(
                f"Unmatch line {to_unmatch_id} ↔ txn #{int(row['txn_id'])}",
                type="secondary",
            ):
                try:
                    writer.unmatch(str(to_unmatch_id), int(row["txn_id"]))
                    st.success("Unmatched.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Unmatch failed: {exc}")

    st.divider()
    _render_start_from_transaction(int(account_id), billing_period)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

PAGES = {
    "Monthly Summary": page_monthly_summary,
    "Transactions": page_transactions,
    "Category Breakdown": page_category_breakdown,
    "Trends": page_trends,
    "Upload Statement": page_upload_statement,
    "Manual Reconciliation": page_manual_reconciliation,
    "Reconciliation Status": page_reconciliation,
}

st.sidebar.title("Personal Finance")
selection = st.sidebar.selectbox("Navigate", list(PAGES.keys()))
PAGES[selection]()
