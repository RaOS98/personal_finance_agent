from telegram import InlineKeyboardButton, InlineKeyboardMarkup


CATEGORIES = [
    ("Food & Dining", "food_dining"),
    ("Groceries", "groceries"),
    ("Transportation", "transportation"),
    ("Housing", "housing"),
    ("Utilities", "utilities"),
    ("Health", "health"),
    ("Personal Care", "personal_care"),
    ("Entertainment", "entertainment"),
    ("Shopping", "shopping"),
    ("Education", "education"),
    ("Work", "work"),
    ("Other", "other"),
]


def confirmation_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data="txn_confirm"),
            InlineKeyboardButton("✏️ Edit", callback_data="txn_edit"),
            InlineKeyboardButton("❌ Cancel", callback_data="txn_cancel"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


def edit_field_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("Merchant", callback_data="edit_merchant")],
        [InlineKeyboardButton("Amount", callback_data="edit_amount")],
        [InlineKeyboardButton("Date", callback_data="edit_date")],
        [InlineKeyboardButton("Category", callback_data="edit_category")],
        [InlineKeyboardButton("Payment Method", callback_data="edit_payment_method")],
    ]
    return InlineKeyboardMarkup(buttons)


def category_keyboard(prefix: str = "cat") -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"{prefix}_{slug}")]
        for name, slug in CATEGORIES
    ]
    return InlineKeyboardMarkup(buttons)


def yes_no_keyboard(prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"{prefix}_yes"),
            InlineKeyboardButton("❌ No", callback_data=f"{prefix}_no"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


def reconciliation_candidates_keyboard(candidates: list) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"#{i + 1}", callback_data=f"recon_match_{i}")]
        for i in range(len(candidates))
    ]
    buttons.append(
        [InlineKeyboardButton("None of these", callback_data="recon_match_none")]
    )
    return InlineKeyboardMarkup(buttons)


def add_skip_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✅ Add", callback_data="recon_add"),
            InlineKeyboardButton("⏭️ Skip", callback_data="recon_skip"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)
