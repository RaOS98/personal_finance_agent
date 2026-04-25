"""Telegram bot handlers (serverless / DynamoDB + S3 backed)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, date

from telegram import Update
from telegram.ext import ContextTypes

import config
import s3_store
from agent import (
    extractor,
    categorizer,
    reconciliation as reconciliation_agent,
    statement_parser,
    intent_classifier,
    tx_editor,
    query_agent,
)
from db import dynamo as db
from bot.keyboards import (
    confirmation_keyboard,
    edit_field_keyboard,
    category_keyboard,
    yes_no_keyboard,
    reconciliation_candidates_keyboard,
    add_skip_keyboard,
    CATEGORIES,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State management (DynamoDB-backed)
# ---------------------------------------------------------------------------

def _load_state(user_id: int) -> dict:
    return db.load_user_state(user_id)


def _persist(user_id: int, state: dict) -> None:
    """Save at end of a handler invocation, or delete if marked cleared."""
    if state.pop("__cleared__", False):
        db.clear_user_state(user_id)
    elif state:
        db.save_user_state(user_id, state)
    else:
        # Nothing to save and nothing to clear — leave any existing state alone.
        pass


def _mark_cleared(state: dict) -> None:
    state.clear()
    state["__cleared__"] = True


def _is_allowed(user_id: int) -> bool:
    return user_id == config.ALLOWED_USER_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CATEGORY_NAME_BY_SLUG = {slug: name for name, slug in CATEGORIES}


def _coerce_txn_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"Unsupported date type: {type(value)!r}")


def format_transaction_summary(txn: dict) -> str:
    currency_symbol = "S/." if txn.get("currency") == "PEN" else "$"
    category_name = txn.get("category_name") or CATEGORY_NAME_BY_SLUG.get(
        txn.get("category_slug"), "—"
    )
    payment_name = txn.get("payment_method_name", "—")
    txn_date = txn.get("date") or "—"

    return (
        f"📝 New transaction:\n"
        f"  Merchant:     {txn.get('merchant') or '—'}\n"
        f"  Description:  {txn.get('description') or '—'}\n"
        f"  Amount:       {currency_symbol} {txn.get('amount', '—')}\n"
        f"  Date:         {txn_date}\n"
        f"  Currency:     {txn.get('currency', '—')}\n"
        f"  Category:     {category_name}\n"
        f"  Paid with:    {payment_name}"
    )


_ACCOUNT_KEYWORDS = {
    "sapphire": 1,
    "amex": 2,
    "soles": 3,
    "yape": 3,
    "bcp": 3,
    "dollars": 4,
    "usd": 4,
}


def resolve_account_from_caption(caption: str) -> tuple[int | None, str]:
    if not caption:
        return None, ""

    lower = caption.lower()

    account_id = None
    for keyword, aid in _ACCOUNT_KEYWORDS.items():
        if keyword in lower:
            account_id = aid
            break

    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
        "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
        "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    }

    billing_period = ""

    for name, num in month_names.items():
        if name in lower:
            year_match = re.search(r"20\d{2}", lower)
            year = int(year_match.group()) if year_match else datetime.now().year
            billing_period = f"{year}-{num:02d}"
            break

    if not billing_period:
        m = re.search(r"(\d{1,2})[/-](20\d{2})", lower)
        if m:
            billing_period = f"{m.group(2)}-{int(m.group(1)):02d}"

    if not billing_period:
        now = datetime.now()
        billing_period = f"{now.year}-{now.month:02d}"

    return account_id, billing_period


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Welcome to your Personal Finance Bot!\n\n"
        "Send me a photo of a receipt or a text description of a transaction "
        "and I will log it for you.\n\n"
        "You can include the payment method in your message "
        "(e.g. 'sapphire', 'amex', 'yape', 'cash').\n\n"
        "To reconcile a bank statement, send me the PDF with a caption "
        "indicating the account (e.g. 'Sapphire statement April 2026')."
    )


# ---------------------------------------------------------------------------
# Top-level handlers (persist state at the end of each invocation)
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    state = _load_state(user_id)
    try:
        await _handle_message_inner(update, context, state, user_id)
    finally:
        _persist(user_id, state)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return

    state = _load_state(user_id)
    try:
        await _handle_callback_inner(update, context, state, user_id)
    finally:
        _persist(user_id, state)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    state = _load_state(user_id)
    try:
        await _handle_document_inner(update, context, state, user_id)
    finally:
        _persist(user_id, state)


# ---------------------------------------------------------------------------
# Main text/photo flow
# ---------------------------------------------------------------------------

async def _handle_message_inner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    user_id: int,
) -> None:
    current = state.get("state")

    if current == "awaiting_edit_value":
        await _handle_edit_value(update, context, state)
        return

    if current == "awaiting_missing_field":
        await _handle_missing_field(update, context, state)
        return

    # --- Intent routing: only for non-photo, free-form text messages --------
    text = update.message.text or update.message.caption or ""

    if not update.message.photo and text.strip():
        forced_intent: str | None = None
        stripped = text.strip()
        if stripped.startswith("!"):
            forced_intent = "edit"
            text = stripped[1:].strip()
        elif stripped.startswith("?"):
            forced_intent = "query"
            text = stripped[1:].strip()

        if forced_intent:
            intent = forced_intent
        else:
            classified = intent_classifier.classify_intent(text)
            intent = classified.get("intent", "new_transaction")

        if intent == "edit":
            await _handle_edit_intent(update, context, state, user_id, text)
            return
        if intent == "query":
            await _handle_query_intent(update, context, state, user_id, text)
            return
        # new_transaction → fall through

    # --- New transaction from text or photo ---------------------------------
    image_bytes = None
    telegram_image_id = None
    tmp_s3_key = None

    if update.message.photo:
        photo = update.message.photo[-1]
        telegram_image_id = photo.file_id
        photo_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await photo_file.download_as_bytearray())
        tmp_s3_key = s3_store.upload_tmp_image(user_id, image_bytes)

    # 1. Extract transaction data
    try:
        extracted = extractor.extract_transaction(image_bytes, text)
    except Exception:
        logger.exception("Extraction failed")
        if tmp_s3_key:
            s3_store.delete_tmp_image(tmp_s3_key)
        await update.message.reply_text(
            "I could not process that input. Please try again or describe "
            "the transaction in text."
        )
        return

    if extracted is None:
        if tmp_s3_key:
            s3_store.delete_tmp_image(tmp_s3_key)
        await update.message.reply_text(
            "I could not extract transaction details. Please try again."
        )
        return

    # 2. Resolve payment method
    alias = extracted.get("payment_method_alias")
    pm = None
    if alias:
        pm = db.resolve_payment_method(alias)

    if pm:
        extracted["payment_method_id"] = pm["id"]
        extracted["payment_method_name"] = pm["name"]
    else:
        extracted["payment_method_id"] = None
        extracted["payment_method_name"] = None

    # 3. Categorize
    try:
        cat_result = categorizer.categorize_transaction(
            merchant=extracted.get("merchant"),
            amount=extracted.get("amount"),
            category_hint=extracted.get("category_hint"),
            user_note=text.strip() or None,
        )
    except Exception:
        logger.exception("Categorization failed")
        cat_result = {"category_slug": None, "confident": False, "needs_description": False}

    extracted["category_slug"] = cat_result.get("category_slug")
    extracted["category_name"] = CATEGORY_NAME_BY_SLUG.get(
        cat_result.get("category_slug"), None
    )
    extracted["needs_description"] = cat_result.get("needs_description", False)

    if not extracted.get("description") and extracted.get("category_hint"):
        extracted["description"] = extracted["category_hint"]

    extracted["telegram_image_id"] = telegram_image_id
    if tmp_s3_key:
        extracted["tmp_s3_key"] = tmp_s3_key

    # 4. Missing critical fields
    if extracted.get("amount") is None:
        state["state"] = "awaiting_missing_field"
        state["missing_field"] = "amount"
        state["pending_txn"] = extracted
        await update.message.reply_text("What is the transaction amount?")
        return

    if extracted.get("currency") is None:
        state["state"] = "awaiting_missing_field"
        state["missing_field"] = "currency"
        state["pending_txn"] = extracted
        await update.message.reply_text(
            "What currency? (PEN for soles, USD for dollars)"
        )
        return

    if extracted.get("payment_method_id") is None:
        state["state"] = "awaiting_missing_field"
        state["missing_field"] = "payment_method"
        state["pending_txn"] = extracted
        await update.message.reply_text(
            "Which payment method did you use? "
            "(e.g. sapphire, amex, yape, cash)"
        )
        return

    # 5. Category confidence
    if not cat_result.get("confident") or extracted.get("category_slug") is None:
        state["state"] = "awaiting_category_select"
        state["pending_txn"] = extracted
        await update.message.reply_text(
            "I am not sure about the category. Please choose one:",
            reply_markup=category_keyboard(),
        )
        return

    # 6. Needs description
    if extracted.get("needs_description") and not extracted.get("description"):
        state["state"] = "awaiting_missing_field"
        state["missing_field"] = "description"
        state["pending_txn"] = extracted
        await update.message.reply_text(
            'The category is "Other". Please provide a brief description:'
        )
        return

    # 7. Default date
    if not extracted.get("date"):
        extracted["date"] = date.today().isoformat()

    # 8. Present confirmation
    state["state"] = "awaiting_confirmation"
    state["pending_txn"] = extracted

    await update.message.reply_text(
        format_transaction_summary(extracted),
        reply_markup=confirmation_keyboard(),
    )


async def _handle_edit_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
) -> None:
    field = state.get("edit_field")
    value = update.message.text.strip()
    txn = state["pending_txn"]

    if field == "merchant":
        txn["merchant"] = value
    elif field == "amount":
        try:
            txn["amount"] = float(value)
        except ValueError:
            await update.message.reply_text("Please enter a valid number.")
            return
    elif field == "date":
        txn["date"] = value
    elif field == "category":
        state["state"] = "awaiting_category_select"
        await update.message.reply_text(
            "Choose a category:", reply_markup=category_keyboard()
        )
        return
    elif field == "payment_method":
        pm = db.resolve_payment_method(value)
        if pm:
            txn["payment_method_id"] = pm["id"]
            txn["payment_method_name"] = pm["name"]
        else:
            await update.message.reply_text(
                "Payment method not recognized. Try: sapphire, amex, yape, cash"
            )
            return

    state["state"] = "awaiting_confirmation"
    await update.message.reply_text(
        format_transaction_summary(txn),
        reply_markup=confirmation_keyboard(),
    )


async def _handle_missing_field(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
) -> None:
    field = state.get("missing_field")
    text = update.message.text if update.message else None
    if not text:
        await update.message.reply_text(
            "Please reply with a text value for this field."
        )
        return
    value = text.strip()
    txn = state["pending_txn"]

    if field == "amount":
        try:
            txn["amount"] = float(value)
        except ValueError:
            await update.message.reply_text("Please enter a valid number.")
            return
    elif field == "currency":
        upper = value.upper()
        if upper not in ("PEN", "USD"):
            await update.message.reply_text("Please enter PEN or USD.")
            return
        txn["currency"] = upper
    elif field == "payment_method":
        pm = db.resolve_payment_method(value)
        if pm:
            txn["payment_method_id"] = pm["id"]
            txn["payment_method_name"] = pm["name"]
        else:
            await update.message.reply_text(
                "Payment method not recognized. Try: sapphire, amex, yape, cash"
            )
            return
    elif field == "description":
        txn["description"] = value

    if txn.get("amount") is None:
        state["missing_field"] = "amount"
        await update.message.reply_text("What is the transaction amount?")
        return

    if txn.get("currency") is None:
        state["missing_field"] = "currency"
        await update.message.reply_text(
            "What currency? (PEN for soles, USD for dollars)"
        )
        return

    if txn.get("payment_method_id") is None:
        state["missing_field"] = "payment_method"
        await update.message.reply_text(
            "Which payment method did you use? "
            "(e.g. sapphire, amex, yape, cash)"
        )
        return

    if txn.get("category_slug") is None:
        state["state"] = "awaiting_category_select"
        await update.message.reply_text(
            "Please choose a category:", reply_markup=category_keyboard()
        )
        return

    if txn.get("needs_description") and not txn.get("description"):
        state["missing_field"] = "description"
        await update.message.reply_text(
            'The category is "Other". Please provide a brief description:'
        )
        return

    if not txn.get("date"):
        txn["date"] = date.today().isoformat()

    state["state"] = "awaiting_confirmation"
    await update.message.reply_text(
        format_transaction_summary(txn),
        reply_markup=confirmation_keyboard(),
    )


# ---------------------------------------------------------------------------
# Edit / query intents
# ---------------------------------------------------------------------------

async def _handle_edit_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    user_id: int,
    text: str,
) -> None:
    recent = db.list_recent_transactions(limit=1)
    if not recent:
        await update.message.reply_text("No recent transactions to edit.")
        return

    target = recent[0]
    try:
        result = tx_editor.parse_edit_request(text, target)
    except Exception:
        logger.exception("tx_editor failed")
        await update.message.reply_text(
            "I couldn't parse that edit. Try something like "
            "'change amount to 25'."
        )
        return

    field = result.get("field")
    new_value = result.get("new_value")

    if not result.get("confident") or not field or new_value is None:
        await update.message.reply_text(
            f"{format_transaction_summary(target)}\n\n"
            "I wasn't sure what to change. Reply with something like "
            "'change amount to 25'."
        )
        return

    # --- Validate + coerce per field ----------------------------------------
    if field == "amount":
        try:
            new_value = float(new_value)
        except (TypeError, ValueError):
            await update.message.reply_text(
                "I couldn't read that as a number. Try 'change amount to 25'."
            )
            return

    elif field == "date":
        try:
            coerced = _coerce_txn_date(new_value)
            new_value = coerced.isoformat()
        except (TypeError, ValueError):
            await update.message.reply_text(
                "I couldn't read that date. Use ISO format (YYYY-MM-DD)."
            )
            return

    elif field == "category_slug":
        slug = str(new_value)
        if slug not in CATEGORY_NAME_BY_SLUG:
            state["state"] = "awaiting_edit_category_select"
            state["edit_target"] = {
                "id": int(target["id"]),
                "old_summary": format_transaction_summary(target),
            }
            await update.message.reply_text(
                "Choose a category:",
                reply_markup=category_keyboard(prefix="editcat"),
            )
            return

    elif field == "payment_method_id":
        pm = db.resolve_payment_method(str(new_value))
        if pm is None:
            await update.message.reply_text(
                "Payment method not recognized. Try: sapphire, amex, yape, cash."
            )
            return
        new_value = int(pm["id"])

    # --- Stage confirmation -------------------------------------------------
    old_value = target.get(field)
    state["state"] = "awaiting_edit_target_confirm"
    state["edit_target"] = {
        "id": int(target["id"]),
        "field": field,
        "new_value": new_value,
        "old_value": old_value,
        "old_summary": format_transaction_summary(target),
    }

    await update.message.reply_text(
        f"Update transaction #{int(target['id'])}: change {field} "
        f"from {old_value!r} to {new_value!r}?",
        reply_markup=yes_no_keyboard("editconfirm"),
    )


async def _handle_query_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    user_id: int,
    text: str,
) -> None:
    await update.message.reply_text("Thinking…")

    try:
        result = query_agent.answer_query(text)
    except Exception:
        logger.exception("query_agent failed")
        await update.message.reply_text(
            "I couldn't answer that. Try rephrasing or use `?` prefix."
        )
        return

    answer = result.get("answer")
    if not answer or result.get("error"):
        await update.message.reply_text(
            "I couldn't answer that. Try rephrasing or use `?` prefix."
        )
        return

    source_ids = result.get("source_txn_ids") or []
    reply = str(answer)
    if source_ids:
        reply += "\n\nBased on: " + ", ".join(f"#{i}" for i in source_ids)
    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# Callback dispatch
# ---------------------------------------------------------------------------

async def _handle_callback_inner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    user_id: int,
) -> None:
    query = update.callback_query
    data = query.data
    txn = state.get("pending_txn")

    if data == "txn_confirm":
        if not txn:
            await query.edit_message_text("No pending transaction found.")
            return
        duplicates = db.check_duplicate_transaction(
            amount=txn["amount"],
            date_val=_coerce_txn_date(txn["date"]),
            payment_method_id=txn["payment_method_id"],
        )
        if duplicates:
            state["state"] = "awaiting_duplicate_confirm"
            dup_info = "\n".join(
                f"  - {d.get('merchant', '?')} on {d.get('date')} "
                f"for {d.get('amount')}"
                for d in duplicates
            )
            await query.edit_message_text(
                f"Possible duplicate(s) found:\n{dup_info}\n\n"
                "Is this a new, distinct transaction?",
                reply_markup=yes_no_keyboard("dup"),
            )
            return
        await _save_transaction(query, state, user_id)
        return

    if data == "txn_edit":
        state["state"] = "awaiting_edit_field"
        await query.edit_message_text(
            "Which field do you want to edit?",
            reply_markup=edit_field_keyboard(),
        )
        return

    if data == "txn_cancel":
        if txn and txn.get("tmp_s3_key"):
            s3_store.delete_tmp_image(txn["tmp_s3_key"])
        _mark_cleared(state)
        await query.edit_message_text("❌ Transaction cancelled.")
        return

    if data.startswith("edit_"):
        field = data.removeprefix("edit_")
        if field == "category":
            state["state"] = "awaiting_category_select"
            await query.edit_message_text(
                "Choose a category:", reply_markup=category_keyboard()
            )
            return
        state["state"] = "awaiting_edit_value"
        state["edit_field"] = field
        await query.edit_message_text(f"Enter the new value for {field}:")
        return

    if data.startswith("cat_"):
        slug = data.removeprefix("cat_")
        if txn is None:
            await query.edit_message_text("No pending transaction.")
            return
        txn["category_slug"] = slug
        txn["category_name"] = CATEGORY_NAME_BY_SLUG.get(slug, slug)
        txn["needs_description"] = slug == "other"

        if txn["needs_description"] and not txn.get("description"):
            state["state"] = "awaiting_missing_field"
            state["missing_field"] = "description"
            await query.edit_message_text(
                'Category set to "Other". Please provide a brief description:'
            )
            return

        if not txn.get("date"):
            txn["date"] = date.today().isoformat()

        state["state"] = "awaiting_confirmation"
        await query.edit_message_text(
            format_transaction_summary(txn),
            reply_markup=confirmation_keyboard(),
        )
        return

    if data == "dup_yes":
        await _save_transaction(query, state, user_id)
        return

    if data == "dup_no":
        if txn and txn.get("tmp_s3_key"):
            s3_store.delete_tmp_image(txn["tmp_s3_key"])
        _mark_cleared(state)
        await query.edit_message_text("❌ Transaction cancelled (duplicate).")
        return

    if data == "editconfirm_yes":
        await _apply_confirmed_edit(query, state)
        return

    if data == "editconfirm_no":
        _mark_cleared(state)
        await query.edit_message_text("❌ Edit cancelled.")
        return

    if data.startswith("editcat_"):
        slug = data.removeprefix("editcat_")
        if slug not in CATEGORY_NAME_BY_SLUG:
            await query.edit_message_text("Unknown category.")
            return
        target_stub = state.get("edit_target") or {}
        target_id = target_stub.get("id")
        if target_id is None:
            await query.edit_message_text("No edit in progress.")
            return
        category = db.get_category_by_slug(slug)
        if category is None:
            await query.edit_message_text("Unknown category.")
            return

        # Re-fetch current target for accurate old_summary + old_value.
        current = db._get_transaction(int(target_id))
        if current is None:
            await query.edit_message_text("Transaction not found.")
            _mark_cleared(state)
            return
        current.pop("_sk", None)

        state["state"] = "awaiting_edit_target_confirm"
        state["edit_target"] = {
            "id": int(target_id),
            "field": "category_slug",
            "new_value": slug,
            "old_value": current.get("category_slug"),
            "old_summary": format_transaction_summary(current),
            "category_id": int(category["id"]),
            "category_name": category["name"],
        }
        await query.edit_message_text(
            f"Update transaction #{int(target_id)}: change category "
            f"from {current.get('category_slug')!r} to {slug!r}?",
            reply_markup=yes_no_keyboard("editconfirm"),
        )
        return

    if data.startswith("recon_match_"):
        suffix = data.removeprefix("recon_match_")
        recon = state.get("recon")
        if not recon:
            await query.edit_message_text("No reconciliation in progress.")
            return
        line = recon["current_line"]
        candidates = recon["current_candidates"]

        if suffix == "none":
            db.update_statement_line_status(line["id"], "pending")
            await query.edit_message_text(
                "No match selected. Would you like to add this as a new "
                "transaction or skip it?",
                reply_markup=add_skip_keyboard(),
            )
            return

        idx = int(suffix)
        if 0 <= idx < len(candidates):
            matched_txn = candidates[idx]
            db.save_reconciliation_match(
                statement_line_id=line["id"],
                transaction_id=int(matched_txn["id"]),
                verdict=str(matched_txn.get("verdict") or "uncertain").strip().lower(),
                confirmed_by="user",
            )
            await query.edit_message_text(
                f"✅ Matched statement line to transaction #{matched_txn['id']}."
            )
        else:
            await query.edit_message_text("Invalid selection.")
            return

        await _advance_reconciliation(query, state, user_id)
        return

    if data == "recon_add":
        recon = state.get("recon")
        if recon and recon.get("current_line"):
            line = recon["current_line"]
            db.update_statement_line_status(line["id"], "added")
            await query.edit_message_text(
                f"Statement line added (date={line['date']}, "
                f"amount={line['amount']}, desc={line['description']}). "
                "You can refine the details later."
            )
        await _advance_reconciliation(query, state, user_id)
        return

    if data == "recon_skip":
        recon = state.get("recon")
        if recon and recon.get("current_line"):
            db.update_statement_line_status(recon["current_line"]["id"], "skipped")
            await query.edit_message_text("⏭️ Skipped.")
        await _advance_reconciliation(query, state, user_id)
        return


# ---------------------------------------------------------------------------
# Save confirmed transaction
# ---------------------------------------------------------------------------

async def _save_transaction(query, state: dict, user_id: int) -> None:
    txn = state.get("pending_txn")
    if not txn:
        await query.edit_message_text("No pending transaction to save.")
        _mark_cleared(state)
        return

    category_id = db.get_category_id_by_slug(txn["category_slug"])
    if category_id is None:
        await query.edit_message_text(
            "Could not resolve category. Please choose a category and try again."
        )
        return

    saved_row = db.save_transaction(
        amount=txn["amount"],
        currency=txn["currency"],
        date_val=_coerce_txn_date(txn["date"]),
        merchant=txn.get("merchant"),
        description=txn.get("description"),
        category_id=category_id,
        payment_method_id=txn["payment_method_id"],
        telegram_image_id=txn.get("telegram_image_id"),
    )
    txn_id = int(saved_row["id"])

    tmp_s3_key = txn.get("tmp_s3_key")
    if tmp_s3_key:
        try:
            final_key = s3_store.finalize_image(
                tmp_s3_key,
                txn_id=txn_id,
                txn_date_iso=saved_row["date"],
            )
            db.update_transaction_image_path(txn_id, final_key)
        except Exception:
            logger.exception("Failed to finalize image for txn %s", txn_id)

    _mark_cleared(state)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"✅ Transaction saved! (ID: {txn_id})")


# ---------------------------------------------------------------------------
# Apply confirmed edit
# ---------------------------------------------------------------------------

async def _apply_confirmed_edit(query, state: dict) -> None:
    edit_target = state.get("edit_target")
    if not edit_target:
        await query.edit_message_text("No edit in progress.")
        _mark_cleared(state)
        return

    txn_id = int(edit_target["id"])
    field = edit_target.get("field")
    new_value = edit_target.get("new_value")

    if not field:
        await query.edit_message_text("No field to update.")
        _mark_cleared(state)
        return

    fields_to_update: dict = {field: new_value}
    if field == "category_slug":
        if edit_target.get("category_id") is not None:
            fields_to_update["category_id"] = int(edit_target["category_id"])
        if edit_target.get("category_name"):
            fields_to_update["category_name"] = edit_target["category_name"]
        else:
            category = db.get_category_by_slug(str(new_value))
            if category is not None:
                fields_to_update["category_id"] = int(category["id"])
                fields_to_update["category_name"] = category["name"]

    if field == "payment_method_id":
        pm_name = None
        try:
            resp = db._ref_cache.get("payment_methods_by_id", {}).get(int(new_value))
            if resp:
                pm_name = resp.get("name")
        except Exception:
            pm_name = None
        if pm_name:
            fields_to_update["payment_method_name"] = pm_name

    try:
        updated = db.update_transaction_fields(txn_id, fields_to_update)
    except KeyError:
        await query.edit_message_text("Transaction not found.")
        _mark_cleared(state)
        return
    except Exception:
        logger.exception("Edit failed for txn %s", txn_id)
        await query.edit_message_text(
            "Something went wrong applying that edit. Please try again."
        )
        _mark_cleared(state)
        return

    _mark_cleared(state)
    await query.edit_message_text(
        f"{format_transaction_summary(updated)}\n\n✅ Updated."
    )


# ---------------------------------------------------------------------------
# PDF statement upload + reconciliation
# ---------------------------------------------------------------------------

async def _handle_document_inner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    user_id: int,
) -> None:
    document = update.message.document
    if not document.file_name or not document.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("Please upload a PDF bank statement.")
        return

    caption = update.message.caption or ""
    account_id, billing_period = resolve_account_from_caption(caption)

    if account_id is None:
        await update.message.reply_text(
            "Please include the account name in the caption "
            "(e.g. 'Sapphire statement April 2026')."
        )
        return

    await update.message.reply_text("Processing statement PDF...")

    file = await context.bot.get_file(document.file_id)
    pdf_bytes = bytes(await file.download_as_bytearray())

    pdf_s3_key: str | None = None
    try:
        pdf_s3_key = s3_store.upload_statement_pdf(
            account_id=account_id,
            billing_period=billing_period,
            pdf_bytes=pdf_bytes,
        )
    except Exception:
        logger.exception("Failed to archive statement PDF to S3")

    try:
        lines = statement_parser.parse_statement_pdf(pdf_bytes)
    except Exception:
        logger.exception("Statement parsing failed")
        await update.message.reply_text(
            "Failed to parse the statement PDF. Please check the file and "
            "try again."
        )
        return

    if not lines:
        await update.message.reply_text("No transaction lines found in the PDF.")
        return

    try:
        db.save_statement_lines(
            account_id,
            billing_period,
            lines,
            pdf_s3_key=pdf_s3_key,
        )
    except Exception:
        logger.exception("Failed to save statement lines")
        await update.message.reply_text(
            "Error saving statement data. Some lines may already exist."
        )
        return

    await update.message.reply_text(
        f"Extracted {len(lines)} lines from statement. Starting reconciliation..."
    )

    await run_reconciliation(account_id, billing_period, update, state, user_id)


# ---------------------------------------------------------------------------
# Reconciliation orchestration (batched LLM calls)
# ---------------------------------------------------------------------------

async def run_reconciliation(
    account_id: int,
    billing_period: str,
    update: Update,
    state: dict,
    user_id: int,
) -> None:
    """Drive the auto-match loop and hand Telegram users the review queue."""
    result = reconciliation_agent.auto_reconcile(
        account_id=account_id,
        billing_period=billing_period,
    )

    auto_matched = result["auto_matched"]
    needs_review = result["needs_review"]
    unmatched_entries = result["unmatched"]

    unmatched_lines = [entry["line"] for entry in unmatched_entries]

    summary = (
        f"Reconciliation complete:\n"
        f"  ✅ Auto-matched: {len(auto_matched)}\n"
        f"  🔍 Need review: {len(needs_review)}\n"
        f"  ❓ Unmatched: {len(unmatched_lines)}"
    )
    await update.message.reply_text(summary)

    if needs_review or unmatched_lines:
        state["state"] = "reconciliation_review"
        state["recon"] = {
            "review_queue": needs_review,
            "unmatched_queue": unmatched_lines,
            "current_line": None,
            "current_candidates": None,
        }
        await _send_next_review_item(update.message, state, user_id)


async def _send_next_review_item(reply_target, state: dict, user_id: int) -> None:
    """Present the next review or unmatched item. ``reply_target`` must support
    ``reply_text`` (an Update.message or a CallbackQuery.message)."""
    recon = state.get("recon", {})
    review_queue = recon.get("review_queue", [])
    unmatched_queue = recon.get("unmatched_queue", [])

    if review_queue:
        item = review_queue.pop(0)
        line = item["line"]
        candidates = item["candidates"]
        recon["current_line"] = line
        recon["current_candidates"] = candidates

        msg = (
            f"📋 Statement line:\n"
            f"  Date: {line['date']}\n"
            f"  Description: {line['description']}\n"
            f"  Amount: {line['amount']}\n\n"
            "Possible matches:\n"
        )
        for i, c in enumerate(candidates):
            msg += (
                f"  #{i + 1}: {c.get('merchant', '?')} on {c.get('date')} "
                f"[{c.get('verdict', 'uncertain')}] - {c.get('reason', '')}\n"
            )
        msg += "\nWhich one matches?"

        await reply_target.reply_text(
            msg, reply_markup=reconciliation_candidates_keyboard(candidates)
        )
        return

    if unmatched_queue:
        line = unmatched_queue.pop(0)
        recon["current_line"] = line
        recon["current_candidates"] = []

        await reply_target.reply_text(
            f"❓ No match found for statement line:\n"
            f"  Date: {line['date']}\n"
            f"  Description: {line['description']}\n"
            f"  Amount: {line['amount']}\n\n"
            "Add as a new transaction or skip?",
            reply_markup=add_skip_keyboard(),
        )
        return

    _mark_cleared(state)
    await reply_target.reply_text("Reconciliation review complete!")


async def _advance_reconciliation(query, state: dict, user_id: int) -> None:
    await _send_next_review_item(query.message, state, user_id)
