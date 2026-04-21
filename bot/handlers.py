import logging
import os
import re
import shutil
from datetime import datetime, date

from telegram import Update
from telegram.ext import ContextTypes

import config
from agent import extractor, categorizer, reconciler, statement_parser
from db import queries
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
# State management
# ---------------------------------------------------------------------------

user_states: dict = {}


def _get_state(user_id: int) -> dict:
    if user_id not in user_states:
        user_states[user_id] = {}
    return user_states[user_id]


def _clear_state(user_id: int) -> None:
    user_states.pop(user_id, None)


def _is_allowed(user_id: int) -> bool:
    return user_id == config.ALLOWED_USER_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CATEGORY_NAME_BY_SLUG = {slug: name for name, slug in CATEGORIES}


def _coerce_txn_date(value: date | datetime | str) -> date:
    """Normalize pending_txn date (ISO string or datetime) to a date for DB calls."""
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


# Maps keywords in the user caption to (account_id, base_billing_period).
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
    """Return (account_id, billing_period) from a user-provided caption.

    Keyword matching determines the account.  A month/year pattern in the
    caption (e.g. "April 2026", "abril 2026", "04/2026") determines the
    billing period; if absent, the current month is used.
    """
    if not caption:
        return None, ""

    lower = caption.lower()

    account_id = None
    for keyword, aid in _ACCOUNT_KEYWORDS.items():
        if keyword in lower:
            account_id = aid
            break

    # Try to extract billing period ------------------------------------------
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
        "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
        "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    }

    billing_period = ""

    # Pattern: "month_name year" (English or Spanish)
    for name, num in month_names.items():
        if name in lower:
            year_match = re.search(r"20\d{2}", lower)
            year = int(year_match.group()) if year_match else datetime.now().year
            billing_period = f"{year}-{num:02d}"
            break

    # Pattern: MM/YYYY or MM-YYYY
    if not billing_period:
        m = re.search(r"(\d{1,2})[/-](20\d{2})", lower)
        if m:
            billing_period = f"{m.group(2)}-{int(m.group(1)):02d}"

    # Fallback to current month
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
# Main message handler (text + photo)
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    state = _get_state(user_id)

    # --- Route to sub-handlers when inside a stateful conversation ----------
    current = state.get("state")

    if current == "awaiting_edit_value":
        await _handle_edit_value(update, context, state)
        return

    if current == "awaiting_missing_field":
        await _handle_missing_field(update, context, state)
        return

    # --- New transaction from text or photo ---------------------------------
    text = update.message.text or update.message.caption or ""
    image_bytes = None
    telegram_image_id = None

    if update.message.photo:
        photo = update.message.photo[-1]  # highest resolution
        telegram_image_id = photo.file_id
        photo_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await photo_file.download_as_bytearray())

    # 1. Extract transaction data --------------------------------------------
    try:
        extracted = extractor.extract_transaction(image_bytes, text)
    except Exception:
        logger.exception("Extraction failed")
        await update.message.reply_text(
            "I could not process that input. Please try again or describe "
            "the transaction in text."
        )
        return

    if extracted is None:
        await update.message.reply_text(
            "I could not extract transaction details. Please try again."
        )
        return

    # 2. Resolve payment method ----------------------------------------------
    alias = extracted.get("payment_method_alias")
    pm = None
    if alias:
        pm = queries.resolve_payment_method(alias)

    if pm:
        extracted["payment_method_id"] = pm["id"]
        extracted["payment_method_name"] = pm["name"]
    else:
        extracted["payment_method_id"] = None
        extracted["payment_method_name"] = None

    # 3. Categorize -----------------------------------------------------------
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

    # Attach image metadata
    extracted["telegram_image_id"] = telegram_image_id
    if image_bytes:
        tmp_path = os.path.join(config.IMAGE_STORAGE_DIR, f"tmp_{user_id}.jpg")
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
        extracted["tmp_image_path"] = tmp_path

    # 4. Handle missing critical fields --------------------------------------
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

    # 5. Category confidence check -------------------------------------------
    if not cat_result.get("confident") or extracted.get("category_slug") is None:
        state["state"] = "awaiting_category_select"
        state["pending_txn"] = extracted
        await update.message.reply_text(
            "I am not sure about the category. Please choose one:",
            reply_markup=category_keyboard(),
        )
        return

    # 6. Needs description? --------------------------------------------------
    if extracted.get("needs_description") and not extracted.get("description"):
        state["state"] = "awaiting_missing_field"
        state["missing_field"] = "description"
        state["pending_txn"] = extracted
        await update.message.reply_text(
            'The category is "Other". Please provide a brief description:'
        )
        return

    # 7. Default date to today if missing ------------------------------------
    if not extracted.get("date"):
        extracted["date"] = date.today().isoformat()

    # 8. Present confirmation ------------------------------------------------
    state["state"] = "awaiting_confirmation"
    state["pending_txn"] = extracted

    await update.message.reply_text(
        format_transaction_summary(extracted),
        reply_markup=confirmation_keyboard(),
    )


# ---------------------------------------------------------------------------
# Sub-handlers for stateful input
# ---------------------------------------------------------------------------

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
        # Show category keyboard instead
        state["state"] = "awaiting_category_select"
        await update.message.reply_text(
            "Choose a category:", reply_markup=category_keyboard()
        )
        return
    elif field == "payment_method":
        pm = queries.resolve_payment_method(value)
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
    value = update.message.text.strip()
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
        pm = queries.resolve_payment_method(value)
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

    # Check if there are still missing critical fields -----------------------
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

    # Category confidence not yet resolved
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
# Callback query handler
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return

    data = query.data
    state = _get_state(user_id)
    txn = state.get("pending_txn")

    # --- Transaction confirmation -------------------------------------------
    if data == "txn_confirm":
        if not txn:
            await query.edit_message_text("No pending transaction found.")
            return
        # Check for duplicates
        duplicates = queries.check_duplicate_transaction(
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
        # Clean up temp image
        if txn and txn.get("tmp_image_path") and os.path.exists(txn["tmp_image_path"]):
            os.remove(txn["tmp_image_path"])
        _clear_state(user_id)
        await query.edit_message_text("❌ Transaction cancelled.")
        return

    # --- Edit field selection -----------------------------------------------
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

    # --- Category selection -------------------------------------------------
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

    # --- Duplicate confirmation ---------------------------------------------
    if data == "dup_yes":
        await _save_transaction(query, state, user_id)
        return

    if data == "dup_no":
        if txn and txn.get("tmp_image_path") and os.path.exists(txn["tmp_image_path"]):
            os.remove(txn["tmp_image_path"])
        _clear_state(user_id)
        await query.edit_message_text("❌ Transaction cancelled (duplicate).")
        return

    # --- Reconciliation match selection -------------------------------------
    if data.startswith("recon_match_"):
        suffix = data.removeprefix("recon_match_")
        recon = state.get("recon")
        if not recon:
            await query.edit_message_text("No reconciliation in progress.")
            return
        line = recon["current_line"]
        candidates = recon["current_candidates"]

        if suffix == "none":
            # Mark statement line as unmatched
            queries.update_statement_line_status(line["id"], "pending")
            await query.edit_message_text(
                "No match selected. Would you like to add this as a new "
                "transaction or skip it?",
                reply_markup=add_skip_keyboard(),
            )
            return

        idx = int(suffix)
        if 0 <= idx < len(candidates):
            matched_txn = candidates[idx]
            queries.save_reconciliation_match(
                statement_line_id=line["id"],
                transaction_id=matched_txn["id"],
                verdict=str(
                    matched_txn.get("verdict") or "uncertain"
                ).strip().lower(),
                confirmed_by="user",
            )
            await query.edit_message_text(
                f"✅ Matched statement line to transaction #{matched_txn['id']}."
            )
        else:
            await query.edit_message_text("Invalid selection.")
            return

        # Advance to next review item
        await _advance_reconciliation(query, state, user_id, context)
        return

    if data == "recon_add":
        recon = state.get("recon")
        if recon and recon.get("current_line"):
            line = recon["current_line"]
            queries.update_statement_line_status(line["id"], "added")
            # TODO: collect details and save as new transaction
            await query.edit_message_text(
                f"Statement line added (date={line['date']}, "
                f"amount={line['amount']}, desc={line['description']}). "
                "You can refine the details later."
            )
        await _advance_reconciliation(query, state, user_id, context)
        return

    if data == "recon_skip":
        recon = state.get("recon")
        if recon and recon.get("current_line"):
            queries.update_statement_line_status(
                recon["current_line"]["id"], "skipped"
            )
            await query.edit_message_text("⏭️ Skipped.")
        await _advance_reconciliation(query, state, user_id, context)
        return


# ---------------------------------------------------------------------------
# Save confirmed transaction
# ---------------------------------------------------------------------------

async def _save_transaction(query, state: dict, user_id: int) -> None:
    txn = state.get("pending_txn")
    if not txn:
        await query.edit_message_text("No pending transaction to save.")
        _clear_state(user_id)
        return

    # Resolve category_id from slug
    category_id = queries.get_category_id_by_slug(txn["category_slug"])
    if category_id is None:
        await query.edit_message_text(
            "Could not resolve category. Please choose a category and try again."
        )
        return

    saved_row = queries.save_transaction(
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

    # Move temp image to final location
    tmp_path = txn.get("tmp_image_path")
    if tmp_path and os.path.exists(tmp_path):
        final_path = os.path.join(config.IMAGE_STORAGE_DIR, f"txn_{txn_id}.jpg")
        shutil.move(tmp_path, final_path)
        queries.update_transaction_image_path(txn_id, final_path)

    _clear_state(user_id)
    await query.edit_message_text(f"✅ Transaction saved! (ID: {txn_id})")


# ---------------------------------------------------------------------------
# Document handler (PDF statement upload)
# ---------------------------------------------------------------------------

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

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

    # Download the PDF
    file = await context.bot.get_file(document.file_id)
    pdf_bytes = bytes(await file.download_as_bytearray())

    # Parse statement
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

    # Save statement lines
    try:
        queries.save_statement_lines(account_id, billing_period, lines)
    except Exception:
        logger.exception("Failed to save statement lines")
        await update.message.reply_text(
            "Error saving statement data. Some lines may already exist."
        )
        return

    await update.message.reply_text(
        f"Extracted {len(lines)} lines from statement. Starting reconciliation..."
    )

    await run_reconciliation(account_id, billing_period, update, context)


# ---------------------------------------------------------------------------
# Reconciliation orchestration
# ---------------------------------------------------------------------------

def _reconciliation_verdict(row: dict) -> str:
    """Normalize agent verdict for comparisons (see AGENT_SPEC Task 4)."""
    return str(row.get("verdict") or "uncertain").strip().lower()


async def run_reconciliation(
    account_id: int,
    billing_period: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user_id = update.effective_user.id
    state = _get_state(user_id)

    pending_lines = queries.get_pending_statement_lines(account_id, billing_period)

    auto_matched = 0
    needs_review: list[dict] = []
    unmatched: list[dict] = []

    for line in pending_lines:
        # Only process debits (positive amounts) in MVP
        if line["amount"] <= 0:
            queries.update_statement_line_status(line["id"], "skipped")
            continue

        candidates = queries.find_reconciliation_candidates(
            account_id=account_id,
            amount=line["amount"],
            date_val=line["date"],
            tolerance_days=config.RECONCILIATION_DATE_TOLERANCE_DAYS,
        )

        if not candidates:
            unmatched.append(line)
            continue

        # Evaluate each candidate with the AI agent
        verdicts = []
        for candidate in candidates:
            try:
                result = reconciler.evaluate_match(
                    statement_line={
                        "date": str(line["date"]),
                        "description": line["description"],
                        "amount": float(line["amount"]),
                    },
                    candidate_transaction={
                        "date": str(candidate["date"]),
                        "merchant": candidate.get("merchant", ""),
                        "amount": float(candidate["amount"]),
                        "category": candidate.get("category_name", ""),
                    },
                )
                raw_v = result.get("verdict") or "uncertain"
                candidate["verdict"] = str(raw_v).strip().lower()
                candidate["reason"] = result.get("reason", "")
            except Exception:
                logger.exception("Reconciliation evaluation failed")
                candidate["verdict"] = "uncertain"
                candidate["reason"] = "Evaluation error"
            verdicts.append(candidate)

        # Apply verdict logic
        confident = [v for v in verdicts if _reconciliation_verdict(v) == "confident"]
        likely = [v for v in verdicts if _reconciliation_verdict(v) == "likely"]

        if len(confident) == 1:
            # Auto-match
            match = confident[0]
            queries.save_reconciliation_match(
                statement_line_id=line["id"],
                transaction_id=match["id"],
                verdict="confident",
                confirmed_by="auto",
            )
            auto_matched += 1
        elif confident or likely:
            needs_review.append({"line": line, "candidates": verdicts})
        else:
            needs_review.append({"line": line, "candidates": verdicts})

    # Summary
    summary = (
        f"Reconciliation complete:\n"
        f"  ✅ Auto-matched: {auto_matched}\n"
        f"  🔍 Need review: {len(needs_review)}\n"
        f"  ❓ Unmatched: {len(unmatched)}"
    )
    await update.message.reply_text(summary)

    # Store review queue in state
    if needs_review or unmatched:
        state["state"] = "reconciliation_review"
        state["recon"] = {
            "review_queue": needs_review,
            "unmatched_queue": unmatched,
            "current_line": None,
            "current_candidates": None,
        }
        await _send_next_review_item(update, state, user_id, context)


async def _send_next_review_item(
    update: Update,
    state: dict,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
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
                f"[{c['verdict']}] - {c.get('reason', '')}\n"
            )
        msg += "\nWhich one matches?"

        await update.message.reply_text(
            msg, reply_markup=reconciliation_candidates_keyboard(candidates)
        )
        return

    if unmatched_queue:
        line = unmatched_queue.pop(0)
        recon["current_line"] = line
        recon["current_candidates"] = []

        await update.message.reply_text(
            f"❓ No match found for statement line:\n"
            f"  Date: {line['date']}\n"
            f"  Description: {line['description']}\n"
            f"  Amount: {line['amount']}\n\n"
            "Add as a new transaction or skip?",
            reply_markup=add_skip_keyboard(),
        )
        return

    # All done
    _clear_state(user_id)
    await update.message.reply_text("Reconciliation review complete!")


async def _advance_reconciliation(
    query, state: dict, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Move to the next reconciliation review item after a callback action."""
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
                f"[{c['verdict']}] - {c.get('reason', '')}\n"
            )
        msg += "\nWhich one matches?"

        await query.message.reply_text(
            msg, reply_markup=reconciliation_candidates_keyboard(candidates)
        )
        return

    if unmatched_queue:
        line = unmatched_queue.pop(0)
        recon["current_line"] = line
        recon["current_candidates"] = []

        await query.message.reply_text(
            f"❓ No match found for statement line:\n"
            f"  Date: {line['date']}\n"
            f"  Description: {line['description']}\n"
            f"  Amount: {line['amount']}\n\n"
            "Add as a new transaction or skip?",
            reply_markup=add_skip_keyboard(),
        )
        return

    _clear_state(user_id)
    await query.message.reply_text("Reconciliation review complete!")
