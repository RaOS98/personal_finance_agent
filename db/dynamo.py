"""DynamoDB data access layer for the personal finance agent.

Single-table design (table name from ``config.DYNAMODB_TABLE``):

    PK                          SK                                Entity
    --------------------------  --------------------------------  -----------
    COUNTER                     txn | statement_line              monotonic id counter
    REF                         CATEGORY#{slug}                   category
    REF                         ACCOUNT#{id:04d}                  account
    REF                         PM#{id:04d}                       payment method
    TXN                         {date}#{txn_id:08d}               transaction
    STMT#{acct}#{period}        {date}#{line_id:012x}             statement line
    MATCH                       STMT#{line_id:012x}#TXN#{txn_id}  reconciliation match
    STATE#{user_id}             current                           bot user state

Global secondary indexes:

    GSI1  AMT#{account_id}#{amount_cents}  {date}#{txn_id:08d}    transaction-by-amount
    GSI2  STATUS#TXN#{status}              {date}#{txn_id:08d}    transaction-by-status
    GSI3  LINE#{line_id}                   (no SK)                statement-line-by-id

Function signatures mirror the previous ``db.queries`` module so callers need
minimal changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

import config


logger = logging.getLogger(__name__)


_dynamodb = boto3.resource("dynamodb", region_name=config.AWS_REGION)
_table = _dynamodb.Table(config.DYNAMODB_TABLE)

_serializer = TypeSerializer()

_TWO_PLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _amount_cents(amount: Any) -> int:
    return int((_to_decimal(amount) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _iso(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        # Validate format (will raise if malformed).
        date.fromisoformat(value)
        return value
    raise TypeError(f"Unsupported date type: {type(value)!r}")


def _normalize_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Recursively convert Decimals to int/float and drop internal keys."""
    if item is None:
        return None
    cleaned: dict[str, Any] = {}
    for k, v in item.items():
        if k in {"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "ttl"}:
            continue
        cleaned[k] = _normalize_value(v)
    return cleaned


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    if isinstance(value, list):
        return [_normalize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in value.items()}
    if isinstance(value, set):
        return sorted(_normalize_value(v) for v in value)
    return value


def _next_id(counter_name: str) -> int:
    """Atomic counter increment. Auto-initializes to 1 on first use."""
    resp = _table.update_item(
        Key={"PK": "COUNTER", "SK": counter_name},
        UpdateExpression="ADD #v :one",
        ExpressionAttributeNames={"#v": "value"},
        ExpressionAttributeValues={":one": Decimal(1)},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["value"])


def _line_id_for(
    account_id: int,
    billing_period: str,
    date_iso: str,
    description: str,
    amount_cents: int,
) -> str:
    """Deterministic 12-hex-char id from the Postgres uniqueness tuple."""
    payload = f"{account_id}|{billing_period}|{date_iso}|{description}|{amount_cents}"
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=6).hexdigest()


# ---------------------------------------------------------------------------
# Reference-data lookups (cached at cold start)
# ---------------------------------------------------------------------------

_ref_cache: dict[str, Any] = {}


def _load_reference() -> None:
    """Fetch all reference data in one query and cache it."""
    if _ref_cache:
        return

    resp = _table.query(
        KeyConditionExpression=Key("PK").eq("REF"),
    )
    items = [_normalize_item(i) for i in resp.get("Items", [])]

    categories_by_slug: dict[str, dict] = {}
    categories_by_id: dict[int, dict] = {}
    accounts_by_id: dict[int, dict] = {}
    payment_methods_by_id: dict[int, dict] = {}

    for item in items:
        kind = item.get("kind")
        if kind == "category":
            categories_by_slug[item["slug"]] = item
            categories_by_id[item["id"]] = item
        elif kind == "account":
            accounts_by_id[item["id"]] = item
        elif kind == "payment_method":
            payment_methods_by_id[item["id"]] = item

    _ref_cache["categories_by_slug"] = categories_by_slug
    _ref_cache["categories_by_id"] = categories_by_id
    _ref_cache["accounts_by_id"] = accounts_by_id
    _ref_cache["payment_methods_by_id"] = payment_methods_by_id


def invalidate_reference_cache() -> None:
    _ref_cache.clear()


def resolve_payment_method(alias: str) -> dict[str, Any] | None:
    """Case-insensitive alias match across payment_methods. Returns the payment
    method dict with nested account fields (account_name, account_bank,
    account_currency, account_type), matching the prior SQL shape.
    """
    _load_reference()
    needle = alias.lower().strip()
    for pm in _ref_cache["payment_methods_by_id"].values():
        aliases = [a.lower() for a in pm.get("aliases", [])]
        if needle in aliases:
            account = _ref_cache["accounts_by_id"].get(pm.get("account_id"))
            return {
                "id": pm["id"],
                "name": pm["name"],
                "aliases": pm.get("aliases", []),
                "account_id": pm.get("account_id"),
                "account_name": account.get("name") if account else None,
                "account_bank": account.get("bank") if account else None,
                "account_currency": account.get("currency") if account else None,
                "account_type": account.get("type") if account else None,
            }
    return None


def get_category_by_slug(slug: str) -> dict[str, Any] | None:
    _load_reference()
    cat = _ref_cache["categories_by_slug"].get(slug)
    if cat is None:
        return None
    return {"id": cat["id"], "name": cat["name"], "slug": cat["slug"]}


def get_category_id_by_slug(slug: str) -> int | None:
    cat = get_category_by_slug(slug)
    return int(cat["id"]) if cat else None


def get_all_categories() -> list[dict[str, Any]]:
    _load_reference()
    cats = list(_ref_cache["categories_by_slug"].values())
    return sorted(
        [{"id": c["id"], "name": c["name"], "slug": c["slug"]} for c in cats],
        key=lambda c: c["id"],
    )


def _account_id_for_payment_method(pm_id: int) -> int | None:
    _load_reference()
    pm = _ref_cache["payment_methods_by_id"].get(int(pm_id))
    return pm.get("account_id") if pm else None


def _payment_method_name(pm_id: int) -> str | None:
    _load_reference()
    pm = _ref_cache["payment_methods_by_id"].get(int(pm_id))
    return pm.get("name") if pm else None


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def check_duplicate_transaction(
    amount: Decimal | float,
    date_val: date,
    payment_method_id: int,
) -> list[dict[str, Any]]:
    """Return transactions with the same (amount, date, payment_method_id)."""
    account_id = _account_id_for_payment_method(payment_method_id)
    if account_id is None:
        return []

    date_iso = _iso(date_val)
    cents = _amount_cents(amount)
    gsi1pk = f"AMT#{account_id}#{cents}"

    resp = _table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(gsi1pk)
        & Key("GSI1SK").begins_with(date_iso),
        FilterExpression=Attr("payment_method_id").eq(int(payment_method_id)),
    )
    return [_normalize_item(i) for i in resp.get("Items", [])]


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
    """Insert a transaction and return its stored fields (including id)."""
    _load_reference()
    txn_id = _next_id("txn")
    date_iso = _iso(date_val)
    cents = _amount_cents(amount)
    amount_dec = _to_decimal(amount)
    account_id = _account_id_for_payment_method(payment_method_id)
    category = _ref_cache["categories_by_id"].get(int(category_id))
    pm_name = _payment_method_name(payment_method_id)
    created_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    item: dict[str, Any] = {
        "PK": "TXN",
        "SK": f"{date_iso}#{txn_id:08d}",
        "GSI1PK": f"AMT#{account_id}#{cents}" if account_id is not None else f"AMT#NONE#{cents}",
        "GSI1SK": f"{date_iso}#{txn_id:08d}",
        "GSI2PK": "STATUS#TXN#unreconciled",
        "GSI2SK": f"{date_iso}#{txn_id:08d}",
        "id": txn_id,
        "amount": amount_dec,
        "amount_cents": cents,
        "currency": currency,
        "date": date_iso,
        "merchant": merchant,
        "description": description,
        "category_id": int(category_id),
        "category_slug": category.get("slug") if category else None,
        "category_name": category.get("name") if category else None,
        "payment_method_id": int(payment_method_id),
        "payment_method_name": pm_name,
        "account_id": account_id,
        "telegram_image_id": telegram_image_id,
        "image_path": image_path,
        "reconciliation_status": "unreconciled",
        "created_at": created_at,
    }

    _table.put_item(Item=item)
    return _normalize_item(item)


def update_transaction_image_path(txn_id: int, image_path: str) -> None:
    """Update the image_path for an existing transaction."""
    txn = _get_transaction(int(txn_id))
    if txn is None:
        logger.warning("update_transaction_image_path: txn %s not found", txn_id)
        return
    _table.update_item(
        Key={"PK": "TXN", "SK": txn["_sk"]},
        UpdateExpression="SET image_path = :p",
        ExpressionAttributeValues={":p": image_path},
    )


def update_transaction_reconciliation_status(transaction_id: int, status: str) -> None:
    txn = _get_transaction(int(transaction_id))
    if txn is None:
        logger.warning("update_transaction_reconciliation_status: txn %s not found", transaction_id)
        return
    _table.update_item(
        Key={"PK": "TXN", "SK": txn["_sk"]},
        UpdateExpression="SET reconciliation_status = :s, GSI2PK = :g",
        ExpressionAttributeValues={
            ":s": status,
            ":g": f"STATUS#TXN#{status}",
        },
    )


_EDITABLE_TXN_FIELDS = {
    "merchant",
    "description",
    "amount",
    "category_id",
    "category_slug",
    "category_name",
    "date",
    "payment_method_id",
    "payment_method_name",
}


def update_transaction_fields(txn_id: int, fields: dict[str, Any]) -> dict[str, Any]:
    """Apply partial updates to an existing transaction.

    If ``amount`` or ``date`` is among the edited fields, the SK and GSI1
    partition key are derived from them — a plain UpdateItem cannot rewrite
    keys. In that case the item is deleted and re-inserted under the new
    keys in a single TransactWriteItems call, preserving ``id``,
    ``created_at``, ``reconciliation_status``, ``telegram_image_id``, and
    ``image_path``.

    Returns the final normalized item. Raises ``KeyError`` if the txn
    does not exist, or ``ValueError`` if a disallowed field is supplied.
    """
    current = _get_transaction(int(txn_id))
    if current is None:
        raise KeyError(f"Transaction {txn_id} not found")

    disallowed = set(fields.keys()) - _EDITABLE_TXN_FIELDS
    if disallowed:
        raise ValueError(f"Disallowed fields: {sorted(disallowed)}")

    old_sk = current.pop("_sk")
    old_key = {"PK": "TXN", "SK": old_sk}

    needs_key_rewrite = "amount" in fields or "date" in fields

    if not needs_key_rewrite:
        update_parts = []
        expr_values: dict[str, Any] = {}
        expr_names: dict[str, str] = {}
        for k, v in fields.items():
            placeholder_name = f"#f_{k}"
            placeholder_value = f":v_{k}"
            expr_names[placeholder_name] = k
            expr_values[placeholder_value] = (
                _to_decimal(v) if k == "amount" else v
            )
            update_parts.append(f"{placeholder_name} = {placeholder_value}")

        _table.update_item(
            Key=old_key,
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
        updated = _get_transaction(int(txn_id))
        if updated is not None:
            updated.pop("_sk", None)
        return updated

    # --- Key rewrite path: delete old + put new, atomically -----------------
    _load_reference()
    new_amount = fields.get("amount", current["amount"])
    new_date = fields.get("date", current["date"])
    new_merchant = fields.get("merchant", current.get("merchant"))
    new_description = fields.get("description", current.get("description"))
    new_category_id = fields.get("category_id", current.get("category_id"))
    new_category_slug = fields.get("category_slug", current.get("category_slug"))
    new_category_name = fields.get("category_name", current.get("category_name"))
    new_pm_id = fields.get("payment_method_id", current.get("payment_method_id"))
    new_pm_name = fields.get("payment_method_name", current.get("payment_method_name"))

    date_iso = _iso(new_date)
    cents = _amount_cents(new_amount)
    amount_dec = _to_decimal(new_amount)
    account_id = _account_id_for_payment_method(int(new_pm_id)) if new_pm_id is not None else current.get("account_id")
    txn_id_int = int(current["id"])
    status = current.get("reconciliation_status", "unreconciled")

    new_item: dict[str, Any] = {
        "PK": "TXN",
        "SK": f"{date_iso}#{txn_id_int:08d}",
        "GSI1PK": f"AMT#{account_id}#{cents}" if account_id is not None else f"AMT#NONE#{cents}",
        "GSI1SK": f"{date_iso}#{txn_id_int:08d}",
        "GSI2PK": f"STATUS#TXN#{status}",
        "GSI2SK": f"{date_iso}#{txn_id_int:08d}",
        "id": txn_id_int,
        "amount": amount_dec,
        "amount_cents": cents,
        "currency": current.get("currency"),
        "date": date_iso,
        "merchant": new_merchant,
        "description": new_description,
        "category_id": int(new_category_id) if new_category_id is not None else None,
        "category_slug": new_category_slug,
        "category_name": new_category_name,
        "payment_method_id": int(new_pm_id) if new_pm_id is not None else None,
        "payment_method_name": new_pm_name,
        "account_id": account_id,
        "telegram_image_id": current.get("telegram_image_id"),
        "image_path": current.get("image_path"),
        "reconciliation_status": status,
        "created_at": current.get("created_at"),
    }

    def _serialize_map(d: dict[str, Any]) -> dict[str, Any]:
        return {k: _serializer.serialize(v) for k, v in d.items() if v is not None}

    _table.meta.client.transact_write_items(
        TransactItems=[
            {"Delete": {
                "TableName": config.DYNAMODB_TABLE,
                "Key": _serialize_map(old_key),
            }},
            {"Put": {
                "TableName": config.DYNAMODB_TABLE,
                "Item": _serialize_map(new_item),
            }},
        ]
    )
    return _normalize_item(new_item)


def list_recent_transactions(limit: int) -> list[dict[str, Any]]:
    """Return the most recent transactions, newest first.

    SK format ``{date_iso}#{txn_id:08d}`` means reverse-sort within the TXN
    partition yields newest-by-date (ties broken by descending id).
    """
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq("TXN"),
        ScanIndexForward=False,
        Limit=int(limit),
    )
    return [_normalize_item(i) for i in resp.get("Items", [])]


def _get_transaction(transaction_id: int) -> dict[str, Any] | None:
    """Find a transaction by integer id. Returns the raw item with ``_sk``
    populated so callers can do targeted updates. Returns None if missing."""
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq("TXN"),
        FilterExpression=Attr("id").eq(int(transaction_id)),
    )
    items = resp.get("Items", [])
    if not items:
        return None
    raw = items[0]
    result = _normalize_item(raw)
    result["_sk"] = raw["SK"]
    return result


def get_unreconciled_transactions(
    account_id: int,
    amount: Decimal | float,
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    cents = _amount_cents(amount)
    gsi1pk = f"AMT#{int(account_id)}#{cents}"
    date_from_iso = _iso(date_from)
    date_to_iso = _iso(date_to) + "#99999999"  # inclusive of end-of-day

    resp = _table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(gsi1pk)
        & Key("GSI1SK").between(date_from_iso, date_to_iso),
        FilterExpression=Attr("reconciliation_status").eq("unreconciled"),
    )
    items = [_normalize_item(i) for i in resp.get("Items", [])]
    items.sort(key=lambda x: x.get("date", ""))
    return items


def find_reconciliation_candidates(
    account_id: int,
    amount: Decimal | float,
    date_val: date,
    tolerance_days: int,
) -> list[dict[str, Any]]:
    date_from = date_val - timedelta(days=tolerance_days)
    date_to = date_val + timedelta(days=tolerance_days)
    return get_unreconciled_transactions(account_id, amount, date_from, date_to)


# ---------------------------------------------------------------------------
# Statement lines
# ---------------------------------------------------------------------------

def save_statement_lines(
    account_id: int,
    billing_period: str,
    lines: list[dict[str, Any]],
    pdf_s3_key: str | None = None,
) -> int:
    """Idempotent bulk insert. Returns the number of NEW rows inserted.

    If ``pdf_s3_key`` is supplied, every newly-inserted line stores the key so
    the dashboard can render a presigned link back to the original PDF.
    """
    inserted = 0
    for line in lines:
        date_iso = _iso(line["date"])
        amount = _to_decimal(line["amount"])
        cents = _amount_cents(line["amount"])
        description = line.get("description") or "Unknown"

        line_id = _line_id_for(account_id, billing_period, date_iso, description, cents)
        pk = f"STMT#{int(account_id)}#{billing_period}"
        sk = f"{date_iso}#{line_id}"
        created_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"

        item: dict[str, Any] = {
            "PK": pk,
            "SK": sk,
            "GSI3PK": f"LINE#{line_id}",
            "id": line_id,
            "account_id": int(account_id),
            "billing_period": billing_period,
            "date": date_iso,
            "description": description,
            "amount": amount,
            "amount_cents": cents,
            "reconciliation_status": "pending",
            "created_at": created_at,
        }
        if pdf_s3_key:
            item["pdf_s3_key"] = pdf_s3_key

        try:
            _table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
            inserted += 1
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return inserted


def get_pending_statement_lines(
    account_id: int,
    billing_period: str,
) -> list[dict[str, Any]]:
    pk = f"STMT#{int(account_id)}#{billing_period}"
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(pk),
        FilterExpression=Attr("reconciliation_status").eq("pending"),
    )
    items = [_normalize_item(i) for i in resp.get("Items", [])]
    items.sort(key=lambda x: (x.get("date", ""), x.get("id", "")))
    return items


def update_statement_line_status(statement_line_id: str, status: str) -> None:
    """Statement line ids are strings (content-hash)."""
    line = _get_statement_line(statement_line_id)
    if line is None:
        logger.warning("update_statement_line_status: line %s not found", statement_line_id)
        return
    _table.update_item(
        Key={"PK": line["_pk"], "SK": line["_sk"]},
        UpdateExpression="SET reconciliation_status = :s",
        ExpressionAttributeValues={":s": status},
    )


def _get_statement_line(line_id: str) -> dict[str, Any] | None:
    """Look up a statement line by id.

    Tries the dedicated GSI3 (``GSI3PK = LINE#{line_id}``) first because each
    line has a unique partition there; falls back to a full-table scan for
    legacy items that were written before GSI3 existed.
    """
    try:
        resp = _table.query(
            IndexName="GSI3",
            KeyConditionExpression=Key("GSI3PK").eq(f"LINE#{line_id}"),
            Limit=1,
        )
        items = resp.get("Items", [])
    except ClientError as exc:
        # GSI3 not yet deployed in this environment. Fall back to scan and log
        # so we notice during the migration window.
        if exc.response["Error"]["Code"] in {
            "ValidationException",
            "ResourceNotFoundException",
        }:
            logger.debug("GSI3 unavailable, falling back to scan: %s", exc)
            items = []
        else:
            raise

    if not items:
        resp = _table.scan(
            FilterExpression=Attr("id").eq(line_id) & Attr("PK").begins_with("STMT#"),
        )
        items = resp.get("Items", [])
        if not items:
            return None

    raw = items[0]
    result = _normalize_item(raw)
    result["_pk"] = raw["PK"]
    result["_sk"] = raw["SK"]
    return result


# ---------------------------------------------------------------------------
# Reconciliation matches
# ---------------------------------------------------------------------------

def save_reconciliation_match(
    statement_line_id: str,
    transaction_id: int,
    verdict: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Insert a match and flip both sides' statuses."""
    created_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    sk = f"STMT#{statement_line_id}#TXN#{int(transaction_id):08d}"
    item = {
        "PK": "MATCH",
        "SK": sk,
        "statement_line_id": statement_line_id,
        "transaction_id": int(transaction_id),
        "verdict": verdict,
        "confirmed_by": confirmed_by,
        "created_at": created_at,
    }
    _table.put_item(Item=item)

    update_statement_line_status(statement_line_id, "matched")
    update_transaction_reconciliation_status(int(transaction_id), "reconciled")
    return _normalize_item(item)


def delete_reconciliation_match(
    statement_line_id: str,
    transaction_id: int,
) -> bool:
    """Remove a match and flip both sides back to their pre-match statuses.

    Returns True if a match row was deleted, False if no match was found
    (still re-asserts the pending/unreconciled statuses for safety).
    """
    sk = f"STMT#{statement_line_id}#TXN#{int(transaction_id):08d}"
    deleted = False
    try:
        _table.delete_item(
            Key={"PK": "MATCH", "SK": sk},
            ConditionExpression="attribute_exists(PK)",
        )
        deleted = True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    update_statement_line_status(statement_line_id, "pending")
    update_transaction_reconciliation_status(int(transaction_id), "unreconciled")
    return deleted


def list_matches_for_line(statement_line_id: str) -> list[dict[str, Any]]:
    """Return the match rows pointing at a given statement line, if any."""
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq("MATCH")
        & Key("SK").begins_with(f"STMT#{statement_line_id}#"),
    )
    return [_normalize_item(i) for i in resp.get("Items", [])]


def list_matches_for_transaction(transaction_id: int) -> list[dict[str, Any]]:
    """Return the match rows pointing at a given transaction id."""
    resp = _table.scan(
        FilterExpression=Attr("PK").eq("MATCH")
        & Attr("transaction_id").eq(int(transaction_id)),
    )
    return [_normalize_item(i) for i in resp.get("Items", [])]


# ---------------------------------------------------------------------------
# Bot user state (DynamoDB-backed, TTL auto-cleanup)
# ---------------------------------------------------------------------------

def load_user_state(user_id: int) -> dict[str, Any]:
    resp = _table.get_item(Key={"PK": f"STATE#{int(user_id)}", "SK": "current"})
    item = resp.get("Item")
    if not item:
        return {}
    data = item.get("data")
    if not data:
        return {}
    try:
        return json.loads(data)
    except (TypeError, ValueError):
        logger.exception("Failed to decode user state for %s", user_id)
        return {}


def save_user_state(user_id: int, state: dict[str, Any]) -> None:
    ttl = int(time.time()) + config.USER_STATE_TTL_SECONDS
    _table.put_item(
        Item={
            "PK": f"STATE#{int(user_id)}",
            "SK": "current",
            "data": json.dumps(state, default=_json_default),
            "ttl": ttl,
        }
    )


def clear_user_state(user_id: int) -> None:
    _table.delete_item(Key={"PK": f"STATE#{int(user_id)}", "SK": "current"})


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Cannot serialize {type(obj).__name__}")
