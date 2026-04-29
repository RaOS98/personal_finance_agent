"""Telegram bot handlers (serverless / DynamoDB + S3 backed).

Two state surfaces in DynamoDB:
- ``STATE#<user>/current`` — global single-track flows: stored-tx edit (``!``
  intent) and statement reconciliation. Loaded/persisted by every handler call.
- ``STATE#<user>/PENDING#<id>`` — one per in-flight new-tx flow, so multiple
  receipts can be confirmed/edited in any order. Each callback button carries
  its pending id as ``<action>:<id>``; typed replies are routed via a
  ``STATE#<user>/REPLY#<prompt_msg_id>`` lookup that points back to the right
  pending.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, date

from telegram import Update, ForceReply
from telegram.ext import ContextTypes

import config
import s3_store
from agent import (
    extractor,
    categorizer,
    reconciliation as reconciliation_agent,
    statement_parser,
    intent_classifier,
)
from db import dynamo as db
from bot.keyboards import (
    confirmation_keyboard,
    edit_field_keyboard,
    category_keyboard,
    currency_keyboard,
    back_button_keyboard,
    yes_no_keyboard,
    reconciliation_candidates_keyboard,
    add_skip_keyboard,
    CATEGORIES,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Global state (stored-edit + reconciliation)
# ---------------------------------------------------------------------------

def _load_state(user_id: int) -> dict:
    return db.load_user_state(user_id)


def _persist(user_id: int, state: dict) -> None:
    if state.pop("__cleared__", False):
        db.clear_user_state(user_id)
    elif state:
        db.save_user_state(user_id, state)


def _mark_cleared(state: dict) -> None:
    state.clear()
    state["__cleared__"] = True


async def _clear_prompt_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    msg_id: int | None,
) -> None:
    if not msg_id:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=msg_id, reply_markup=None
        )
    except Exception:
        logger.debug("Could not clear keyboard on msg %s", msg_id)


def _is_allowed(user_id: int) -> bool:
    return user_id == config.ALLOWED_USER_ID


def _parse_callback(data: str) -> tuple[str, int | None]:
    """Split ``action:42`` into ``("action", 42)``. Falls back to ``(data, None)``."""
    if ":" in data:
        action, _, id_str = data.rpartition(":")
        if action and id_str.isdigit():
            return action, int(id_str)
    return data, None


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
# Top-level handlers
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
# Main message flow
# ---------------------------------------------------------------------------

async def _handle_message_inner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    user_id: int,
) -> None:
    # 1. Reply-to routing — typed value for some pending tx
    reply_to = update.message.reply_to_message
    if reply_to and update.message.text:
        lookup = db.load_reply_lookup(user_id, reply_to.message_id)
        if lookup:
            await _handle_reply_value(update, context, user_id, lookup)
            return
        # else: stale prompt, fall through

    current = state.get("state")

    # 2. Stored-edit typed value (single-track on global state)
    if current == "awaiting_stored_edit_value":
        await _handle_stored_edit_value(update, context, state)
        return

    # 3. Intent routing for free-form text
    text = update.message.text or update.message.caption or ""

    if not update.message.photo and text.strip():
        forced_intent: str | None = None
        stripped = text.strip()
        if stripped.startswith("!"):
            forced_intent = "edit"
            text = stripped[1:].strip()

        if forced_intent:
            intent = forced_intent
        else:
            classified = intent_classifier.classify_intent(text)
            intent = classified.get("intent", "new_transaction")

        if intent == "edit":
            await _handle_edit_intent(update, context, state, user_id, text)
            return
        # new_transaction → fall through

    # 4. New-transaction (text or photo) → start a new pending flow
    await _start_new_pending_flow(update, context, user_id, text)


# ---------------------------------------------------------------------------
# Start a new pending flow
# ---------------------------------------------------------------------------

async def _start_new_pending_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
) -> None:
    image_bytes = None
    telegram_image_id = None
    tmp_s3_key = None

    if update.message.photo:
        photo = update.message.photo[-1]
        telegram_image_id = photo.file_id
        photo_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await photo_file.download_as_bytearray())
        tmp_s3_key = s3_store.upload_tmp_image(user_id, image_bytes)

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

    alias = extracted.get("payment_method_alias")
    pm = db.resolve_payment_method(alias) if alias else None
    if pm:
        extracted["payment_method_id"] = pm["id"]
        extracted["payment_method_name"] = pm["name"]
    else:
        extracted["payment_method_id"] = None
        extracted["payment_method_name"] = None

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
    extracted["category_name"] = CATEGORY_NAME_BY_SLUG.get(cat_result.get("category_slug"))
    extracted["needs_description"] = cat_result.get("needs_description", False)
    extracted["confident_category"] = bool(cat_result.get("confident"))

    if not extracted.get("description") and extracted.get("category_hint"):
        extracted["description"] = extracted["category_hint"]

    extracted["telegram_image_id"] = telegram_image_id
    if tmp_s3_key:
        extracted["tmp_s3_key"] = tmp_s3_key

    await _present_step_new(update.message, context, user_id, None, extracted)


# ---------------------------------------------------------------------------
# Step computation + rendering
# ---------------------------------------------------------------------------

def _next_step(txn: dict) -> dict:
    """Return a descriptor of the next step needed."""
    if txn.get("amount") is None:
        return {
            "kind": "force_reply",
            "field": "amount",
            "text": "What is the transaction amount?",
        }
    if txn.get("currency") is None:
        return {
            "kind": "force_reply",
            "field": "currency",
            "text": "What currency? (PEN for soles, USD for dollars)",
        }
    if txn.get("payment_method_id") is None:
        return {
            "kind": "force_reply",
            "field": "payment_method",
            "text": "Which payment method did you use? "
                    "(e.g. sapphire, amex, yape, cash)",
        }
    if not txn.get("confident_category") or txn.get("category_slug") is None:
        return {
            "kind": "category_select",
            "text": "I am not sure about the category. Please choose one:",
        }
    if txn.get("needs_description") and not txn.get("description"):
        return {
            "kind": "force_reply",
            "field": "description",
            "text": 'The category is "Other". Please provide a brief description:',
        }
    if not txn.get("date"):
        txn["date"] = date.today().isoformat()
    return {"kind": "summary"}


async def _present_step_new(
    reply_target,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    pending_id: int | None,
    txn: dict,
) -> int:
    """Present the next step by sending a NEW message (no source to edit).

    Used on initial extraction and after typed replies. Returns the
    (possibly newly-assigned) pending_id.
    """
    chat_id = reply_target.chat_id if hasattr(reply_target, "chat_id") else reply_target.chat.id
    step = _next_step(txn)

    if step["kind"] == "force_reply":
        msg = await reply_target.reply_text(
            step["text"], reply_markup=ForceReply(selective=True)
        )
        new_pid = pending_id if pending_id is not None else msg.message_id
        db.save_pending_transaction(user_id, new_pid, {
            "state": "awaiting_missing_field",
            "missing_field": step["field"],
            "txn": txn,
        })
        db.save_reply_lookup(user_id, msg.message_id, {
            "pending_id": new_pid, "kind": "missing", "field": step["field"],
        })
        return new_pid

    if step["kind"] == "category_select":
        if pending_id is not None:
            kb = category_keyboard(prefix="cat", tx_id=pending_id)
            await reply_target.reply_text(step["text"], reply_markup=kb)
            new_pid = pending_id
        else:
            msg = await reply_target.reply_text(step["text"])
            new_pid = msg.message_id
            kb = category_keyboard(prefix="cat", tx_id=new_pid)
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=msg.message_id, reply_markup=kb,
            )
        db.save_pending_transaction(user_id, new_pid, {
            "state": "awaiting_category_select", "txn": txn,
        })
        return new_pid

    # summary
    summary = format_transaction_summary(txn)
    if pending_id is not None:
        kb = confirmation_keyboard(tx_id=pending_id)
        await reply_target.reply_text(summary, reply_markup=kb)
        new_pid = pending_id
    else:
        msg = await reply_target.reply_text(summary)
        new_pid = msg.message_id
        kb = confirmation_keyboard(tx_id=new_pid)
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=msg.message_id, reply_markup=kb,
        )
    db.save_pending_transaction(user_id, new_pid, {
        "state": "awaiting_confirmation", "txn": txn,
    })
    return new_pid


async def _present_step_after_callback(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    pending_id: int,
    txn: dict,
) -> None:
    """Advance the flow after a callback. Edits the source message in place
    when the next step has a keyboard; for typed prompts, strips the source's
    keyboard and sends a new ForceReply message."""
    step = _next_step(txn)

    if step["kind"] == "force_reply":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Could not strip keyboard on source message")
        msg = await query.message.reply_text(
            step["text"], reply_markup=ForceReply(selective=True)
        )
        db.save_pending_transaction(user_id, pending_id, {
            "state": "awaiting_missing_field",
            "missing_field": step["field"],
            "txn": txn,
        })
        db.save_reply_lookup(user_id, msg.message_id, {
            "pending_id": pending_id, "kind": "missing", "field": step["field"],
        })
        return

    if step["kind"] == "category_select":
        kb = category_keyboard(prefix="cat", tx_id=pending_id)
        await query.edit_message_text(step["text"], reply_markup=kb)
        db.save_pending_transaction(user_id, pending_id, {
            "state": "awaiting_category_select", "txn": txn,
        })
        return

    # summary
    await query.edit_message_text(
        format_transaction_summary(txn),
        reply_markup=confirmation_keyboard(tx_id=pending_id),
    )
    db.save_pending_transaction(user_id, pending_id, {
        "state": "awaiting_confirmation", "txn": txn,
    })


# ---------------------------------------------------------------------------
# Reply-to routing
# ---------------------------------------------------------------------------

async def _handle_reply_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    lookup: dict,
) -> None:
    pending_id = int(lookup["pending_id"])
    kind = lookup.get("kind")
    field = lookup.get("field")

    pending = db.load_pending_transaction(user_id, pending_id)
    if not pending:
        await update.message.reply_text("That transaction has expired.")
        db.delete_reply_lookup(user_id, update.message.reply_to_message.message_id)
        return

    txn = pending.get("txn") or {}
    value = (update.message.text or "").strip()

    error = _apply_value_to_txn(txn, field, value)
    if error:
        await update.message.reply_text(error)
        return

    db.delete_reply_lookup(user_id, update.message.reply_to_message.message_id)

    if kind == "edit":
        # Edit flow: jump straight back to the summary
        kb = confirmation_keyboard(tx_id=pending_id)
        await update.message.reply_text(
            format_transaction_summary(txn), reply_markup=kb
        )
        # Strip keyboard from the prior edit-prompt source if we tracked it.
        prompt_msg_id = pending.get("edit_prompt_msg_id")
        if prompt_msg_id:
            await _clear_prompt_keyboard(
                context, update.effective_chat.id, prompt_msg_id
            )
        db.save_pending_transaction(user_id, pending_id, {
            "state": "awaiting_confirmation", "txn": txn,
        })
        return

    # Missing-field flow: continue to the next step
    await _present_step_new(
        update.message, context, user_id, pending_id, txn
    )


def _apply_value_to_txn(txn: dict, field: str | None, value: str) -> str | None:
    """Apply a typed value to txn in place. Returns an error message string
    if the input is invalid, otherwise None."""
    if field == "merchant":
        txn["merchant"] = value
    elif field == "description":
        txn["description"] = value
    elif field == "amount":
        try:
            txn["amount"] = float(value)
        except ValueError:
            return "Please enter a valid number."
    elif field == "currency":
        upper = value.upper()
        if upper not in ("PEN", "USD"):
            return "Please enter PEN or USD."
        txn["currency"] = upper
    elif field == "date":
        try:
            txn["date"] = _coerce_txn_date(value).isoformat()
        except (TypeError, ValueError):
            return (
                "I couldn't read that date. Use ISO format (YYYY-MM-DD), "
                "e.g. 2026-04-26."
            )
    elif field == "payment_method":
        pm = db.resolve_payment_method(value)
        if not pm:
            return "Payment method not recognized. Try: sapphire, amex, yape, cash"
        txn["payment_method_id"] = pm["id"]
        txn["payment_method_name"] = pm["name"]
    else:
        return "Unknown field; cannot apply."
    return None


# ---------------------------------------------------------------------------
# Stored-edit (saved tx) typed value — single-track on global state
# ---------------------------------------------------------------------------

async def _handle_stored_edit_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
) -> None:
    field = state.get("stored_edit_field")
    value = update.message.text.strip()
    snapshot = state.get("edit_snapshot")
    if snapshot is None:
        await update.message.reply_text("No edit in progress.")
        _mark_cleared(state)
        return

    error = _apply_value_to_txn(snapshot, field, value)
    if error:
        await update.message.reply_text(error)
        return

    await _clear_prompt_keyboard(
        context,
        chat_id=update.effective_chat.id,
        msg_id=state.pop("stored_edit_prompt_msg_id", None),
    )
    state["state"] = "awaiting_stored_edit_confirm"
    state.pop("stored_edit_field", None)
    await update.message.reply_text(
        format_transaction_summary(snapshot),
        reply_markup=confirmation_keyboard(prefix="storededit"),
    )


# ---------------------------------------------------------------------------
# Edit / stored transactions (operate on saved transactions)
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

    target = dict(recent[0])
    target.pop("_sk", None)

    state["state"] = "awaiting_stored_edit_confirm"
    state["edit_snapshot"] = dict(target)
    state["edit_original"] = dict(target)
    state["edit_txn_id"] = int(target["id"])

    await update.message.reply_text(
        format_transaction_summary(target),
        reply_markup=confirmation_keyboard(prefix="storededit"),
    )


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
    action, pending_id = _parse_callback(query.data)

    if pending_id is not None:
        await _dispatch_pending_callback(query, context, user_id, action, pending_id)
        return

    # No id suffix → stored-edit or reconciliation callbacks (single-track).
    await _dispatch_global_callback(query, context, state, user_id, action)


async def _dispatch_pending_callback(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: str,
    pending_id: int,
) -> None:
    pending = db.load_pending_transaction(user_id, pending_id)
    if not pending:
        await query.edit_message_text("That transaction has expired.")
        return

    txn = pending.get("txn") or {}

    if action == "txn_confirm":
        await _confirm_pending(query, user_id, pending_id, pending)
        return

    if action == "txn_edit":
        db.save_pending_transaction(user_id, pending_id, {
            **pending, "state": "awaiting_edit_field",
        })
        await query.edit_message_text(
            "Which field do you want to edit?",
            reply_markup=edit_field_keyboard(tx_id=pending_id),
        )
        return

    if action == "txn_cancel":
        if txn.get("tmp_s3_key"):
            s3_store.delete_tmp_image(txn["tmp_s3_key"])
        db.delete_pending_transaction(user_id, pending_id)
        await query.edit_message_text("❌ Transaction cancelled.")
        return

    if action == "edit_back":
        await query.edit_message_text(
            format_transaction_summary(txn),
            reply_markup=confirmation_keyboard(tx_id=pending_id),
        )
        db.save_pending_transaction(user_id, pending_id, {
            **pending, "state": "awaiting_confirmation",
        })
        return

    if action == "edit_category":
        await query.edit_message_text(
            "Choose a category:",
            reply_markup=category_keyboard(
                prefix="editcat",
                back_data=f"editcat_back:{pending_id}",
                tx_id=pending_id,
            ),
        )
        db.save_pending_transaction(user_id, pending_id, {
            **pending, "state": "awaiting_edit_category",
        })
        return

    if action == "edit_currency":
        await query.edit_message_text(
            "Choose a currency:",
            reply_markup=currency_keyboard(prefix="editcur", tx_id=pending_id),
        )
        db.save_pending_transaction(user_id, pending_id, {
            **pending, "state": "awaiting_edit_currency",
        })
        return

    if action.startswith("edit_"):
        # Free-form value edit: merchant/description/amount/date/payment_method
        field = action.removeprefix("edit_")
        prompt_text = (
            "Enter the new date (YYYY-MM-DD, e.g. 2026-04-26).\n"
            "Reply to this message with the value, or tap Back."
            if field == "date"
            else f"Enter the new value for {field}.\n"
                 "Reply to this message with the value, or tap Back."
        )
        await query.edit_message_text(
            prompt_text,
            reply_markup=back_button_keyboard(f"editvalue_back:{pending_id}"),
        )
        prompt_msg_id = query.message.message_id
        db.save_pending_transaction(user_id, pending_id, {
            **pending,
            "state": "awaiting_edit_value",
            "edit_field": field,
            "edit_prompt_msg_id": prompt_msg_id,
        })
        # Route the user's reply to *this* message back to the right pending.
        db.save_reply_lookup(user_id, prompt_msg_id, {
            "pending_id": pending_id, "kind": "edit", "field": field,
        })
        return

    if action == "editvalue_back":
        # Cancel the in-progress field edit, return to edit-field menu
        prompt_msg_id = pending.get("edit_prompt_msg_id")
        if prompt_msg_id:
            db.delete_reply_lookup(user_id, prompt_msg_id)
        new_pending = {**pending, "state": "awaiting_edit_field"}
        new_pending.pop("edit_field", None)
        new_pending.pop("edit_prompt_msg_id", None)
        await query.edit_message_text(
            "Which field do you want to edit?",
            reply_markup=edit_field_keyboard(tx_id=pending_id),
        )
        db.save_pending_transaction(user_id, pending_id, new_pending)
        return

    if action == "editcur_back":
        await query.edit_message_text(
            "Which field do you want to edit?",
            reply_markup=edit_field_keyboard(tx_id=pending_id),
        )
        db.save_pending_transaction(user_id, pending_id, {
            **pending, "state": "awaiting_edit_field",
        })
        return

    if action.startswith("editcur_"):
        suffix = action.removeprefix("editcur_")
        if suffix not in ("PEN", "USD"):
            await query.edit_message_text("Unknown currency.")
            return
        txn["currency"] = suffix
        await query.edit_message_text(
            format_transaction_summary(txn),
            reply_markup=confirmation_keyboard(tx_id=pending_id),
        )
        db.save_pending_transaction(user_id, pending_id, {
            **pending, "state": "awaiting_confirmation", "txn": txn,
        })
        return

    if action == "editcat_back":
        await query.edit_message_text(
            "Which field do you want to edit?",
            reply_markup=edit_field_keyboard(tx_id=pending_id),
        )
        db.save_pending_transaction(user_id, pending_id, {
            **pending, "state": "awaiting_edit_field",
        })
        return

    if action.startswith("editcat_"):
        slug = action.removeprefix("editcat_")
        if slug not in CATEGORY_NAME_BY_SLUG:
            await query.edit_message_text("Unknown category.")
            return
        txn["category_slug"] = slug
        txn["category_name"] = CATEGORY_NAME_BY_SLUG.get(slug, slug)
        await query.edit_message_text(
            format_transaction_summary(txn),
            reply_markup=confirmation_keyboard(tx_id=pending_id),
        )
        db.save_pending_transaction(user_id, pending_id, {
            **pending, "state": "awaiting_confirmation", "txn": txn,
        })
        return

    if action.startswith("cat_"):
        slug = action.removeprefix("cat_")
        if slug not in CATEGORY_NAME_BY_SLUG:
            await query.edit_message_text("Unknown category.")
            return
        txn["category_slug"] = slug
        txn["category_name"] = CATEGORY_NAME_BY_SLUG.get(slug, slug)
        txn["needs_description"] = slug == "other"
        txn["confident_category"] = True
        await _present_step_after_callback(
            query, context, user_id, pending_id, txn
        )
        return

    if action == "dup_yes":
        # User confirmed it's a distinct transaction; save.
        await _save_pending(query, user_id, pending_id, pending)
        return

    if action == "dup_no":
        if txn.get("tmp_s3_key"):
            s3_store.delete_tmp_image(txn["tmp_s3_key"])
        db.delete_pending_transaction(user_id, pending_id)
        await query.edit_message_text("❌ Transaction cancelled (duplicate).")
        return

    logger.warning("Unhandled pending callback action=%s pid=%s", action, pending_id)


async def _confirm_pending(
    query,
    user_id: int,
    pending_id: int,
    pending: dict,
) -> None:
    txn = pending.get("txn") or {}
    duplicates = db.check_duplicate_transaction(
        amount=txn["amount"],
        date_val=_coerce_txn_date(txn["date"]),
        payment_method_id=txn["payment_method_id"],
    )

    pending_dups = _find_pending_duplicates(user_id, txn, exclude_pending_id=pending_id)

    if duplicates or pending_dups:
        lines = []
        for d in duplicates:
            lines.append(
                f"  - {d.get('merchant', '?')} on {d.get('date')} "
                f"for {d.get('amount')} (saved)"
            )
        for d in pending_dups:
            lines.append(
                f"  - {d.get('merchant', '?')} on {d.get('date')} "
                f"for {d.get('amount')} (pending)"
            )
        await query.edit_message_text(
            "Possible duplicate(s) found:\n" + "\n".join(lines) +
            "\n\nIs this a new, distinct transaction?",
            reply_markup=yes_no_keyboard("dup", tx_id=pending_id),
        )
        db.save_pending_transaction(user_id, pending_id, {
            **pending, "state": "awaiting_duplicate_confirm",
        })
        return

    await _save_pending(query, user_id, pending_id, pending)


def _find_pending_duplicates(
    user_id: int, txn: dict, exclude_pending_id: int,
) -> list[dict]:
    """Scan other PENDING items for amount/date/payment_method match."""
    out: list[dict] = []
    try:
        amount_target = float(txn.get("amount"))
        date_target = txn.get("date")
        pm_target = txn.get("payment_method_id")
    except (TypeError, ValueError):
        return out
    if date_target is None or pm_target is None:
        return out
    for blob in db.list_pending_transactions(user_id):
        if blob.get("__pending_id__") == exclude_pending_id:
            continue
        other = blob.get("txn") or {}
        try:
            if (
                float(other.get("amount")) == amount_target
                and other.get("date") == date_target
                and other.get("payment_method_id") == pm_target
            ):
                out.append(other)
        except (TypeError, ValueError):
            continue
    return out


async def _save_pending(
    query,
    user_id: int,
    pending_id: int,
    pending: dict,
) -> None:
    txn = pending.get("txn") or {}
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

    # Clean up the pending item and any orphaned reply-lookup row.
    prompt_msg_id = pending.get("edit_prompt_msg_id")
    if prompt_msg_id:
        db.delete_reply_lookup(user_id, prompt_msg_id)
    db.delete_pending_transaction(user_id, pending_id)

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"✅ Transaction saved! (ID: {txn_id})")


# ---------------------------------------------------------------------------
# Global (single-track) callback dispatch: stored-edit + reconciliation
# ---------------------------------------------------------------------------

async def _dispatch_global_callback(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    user_id: int,
    data: str,
) -> None:
    if data == "storededit_confirm":
        await _apply_stored_edit(query, state)
        return

    if data == "storededit_edit":
        if state.get("edit_snapshot") is None:
            await query.edit_message_text("No edit in progress.")
            _mark_cleared(state)
            return
        state["state"] = "awaiting_stored_edit_field"
        await query.edit_message_text(
            "Which field do you want to edit?",
            reply_markup=edit_field_keyboard(prefix="storededitfield"),
        )
        return

    if data == "storededit_cancel":
        _mark_cleared(state)
        await query.edit_message_text("❌ Edit cancelled.")
        return

    if data.startswith("storededitfield_"):
        if state.get("edit_snapshot") is None:
            await query.edit_message_text("No edit in progress.")
            _mark_cleared(state)
            return
        field = data.removeprefix("storededitfield_")
        if field == "back":
            state["state"] = "awaiting_stored_edit_confirm"
            state.pop("stored_edit_field", None)
            await query.edit_message_text(
                format_transaction_summary(state["edit_snapshot"]),
                reply_markup=confirmation_keyboard(prefix="storededit"),
            )
            return
        if field == "category":
            state["state"] = "awaiting_stored_edit_category"
            await query.edit_message_text(
                "Choose a category:",
                reply_markup=category_keyboard(
                    prefix="storededitcat", back_data="storededitcat_back"
                ),
            )
            return
        if field == "currency":
            state["state"] = "awaiting_stored_edit_currency"
            await query.edit_message_text(
                "Choose a currency:",
                reply_markup=currency_keyboard(prefix="storededitcur"),
            )
            return
        state["state"] = "awaiting_stored_edit_value"
        state["stored_edit_field"] = field
        prompt_text = (
            "Enter the new date (YYYY-MM-DD, e.g. 2026-04-26):"
            if field == "date"
            else f"Enter the new value for {field}:"
        )
        await query.edit_message_text(
            prompt_text,
            reply_markup=back_button_keyboard("storededitvalue_back"),
        )
        state["stored_edit_prompt_msg_id"] = query.message.message_id
        return

    if data == "storededitvalue_back":
        if state.get("edit_snapshot") is None:
            await query.edit_message_text("No edit in progress.")
            _mark_cleared(state)
            return
        state["state"] = "awaiting_stored_edit_field"
        state.pop("stored_edit_field", None)
        state.pop("stored_edit_prompt_msg_id", None)
        await query.edit_message_text(
            "Which field do you want to edit?",
            reply_markup=edit_field_keyboard(prefix="storededitfield"),
        )
        return

    if data.startswith("storededitcur_"):
        suffix = data.removeprefix("storededitcur_")
        snapshot = state.get("edit_snapshot")
        if suffix == "back":
            if snapshot is None:
                await query.edit_message_text("No edit in progress.")
                _mark_cleared(state)
                return
            state["state"] = "awaiting_stored_edit_field"
            await query.edit_message_text(
                "Which field do you want to edit?",
                reply_markup=edit_field_keyboard(prefix="storededitfield"),
            )
            return
        if snapshot is None:
            await query.edit_message_text("No edit in progress.")
            _mark_cleared(state)
            return
        if suffix not in ("PEN", "USD"):
            await query.edit_message_text("Unknown currency.")
            return
        snapshot["currency"] = suffix
        state["state"] = "awaiting_stored_edit_confirm"
        await query.edit_message_text(
            format_transaction_summary(snapshot),
            reply_markup=confirmation_keyboard(prefix="storededit"),
        )
        return

    if data.startswith("storededitcat_"):
        suffix = data.removeprefix("storededitcat_")
        snapshot = state.get("edit_snapshot")
        if suffix == "back":
            if snapshot is None:
                await query.edit_message_text("No edit in progress.")
                _mark_cleared(state)
                return
            state["state"] = "awaiting_stored_edit_field"
            await query.edit_message_text(
                "Which field do you want to edit?",
                reply_markup=edit_field_keyboard(prefix="storededitfield"),
            )
            return
        if suffix not in CATEGORY_NAME_BY_SLUG:
            await query.edit_message_text("Unknown category.")
            return
        if snapshot is None:
            await query.edit_message_text("No edit in progress.")
            _mark_cleared(state)
            return
        snapshot["category_slug"] = suffix
        snapshot["category_name"] = CATEGORY_NAME_BY_SLUG.get(suffix, suffix)
        state["state"] = "awaiting_stored_edit_confirm"
        await query.edit_message_text(
            format_transaction_summary(snapshot),
            reply_markup=confirmation_keyboard(prefix="storededit"),
        )
        return

    # Reconciliation callbacks (single-track)
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

    logger.warning("Unhandled global callback data=%s", data)


# ---------------------------------------------------------------------------
# Apply confirmed edit on a saved tx
# ---------------------------------------------------------------------------

async def _apply_stored_edit(query, state: dict) -> None:
    snapshot = state.get("edit_snapshot")
    original = state.get("edit_original")
    txn_id = state.get("edit_txn_id")
    if snapshot is None or original is None or txn_id is None:
        await query.edit_message_text("No edit in progress.")
        _mark_cleared(state)
        return

    editable_fields = (
        "merchant",
        "description",
        "amount",
        "currency",
        "date",
        "category_slug",
        "payment_method_id",
    )
    fields_to_update: dict = {}
    for f in editable_fields:
        if snapshot.get(f) != original.get(f):
            fields_to_update[f] = snapshot.get(f)

    if not fields_to_update:
        _mark_cleared(state)
        await query.edit_message_text("No changes.")
        return

    if "category_slug" in fields_to_update:
        slug = fields_to_update["category_slug"]
        category = db.get_category_by_slug(str(slug)) if slug is not None else None
        if category is not None:
            fields_to_update["category_id"] = int(category["id"])
            fields_to_update["category_name"] = category["name"]

    if "payment_method_id" in fields_to_update:
        pm_id = fields_to_update["payment_method_id"]
        pm_name = None
        try:
            if pm_id is not None:
                resp = db._ref_cache.get("payment_methods_by_id", {}).get(int(pm_id))
                if resp:
                    pm_name = resp.get("name")
        except Exception:
            pm_name = None
        if pm_name:
            fields_to_update["payment_method_name"] = pm_name

    try:
        updated = db.update_transaction_fields(int(txn_id), fields_to_update)
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
# Reconciliation orchestration (unchanged)
# ---------------------------------------------------------------------------

async def run_reconciliation(
    account_id: int,
    billing_period: str,
    update: Update,
    state: dict,
    user_id: int,
) -> None:
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
