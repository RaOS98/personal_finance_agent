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


def _suffix(tx_id: int | None) -> str:
    return f":{tx_id}" if tx_id is not None else ""


def confirmation_keyboard(
    prefix: str = "txn", tx_id: int | None = None
) -> InlineKeyboardMarkup:
    s = _suffix(tx_id)
    buttons = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"{prefix}_confirm{s}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"{prefix}_edit{s}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"{prefix}_cancel{s}"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


def edit_field_keyboard(
    prefix: str = "edit", tx_id: int | None = None
) -> InlineKeyboardMarkup:
    s = _suffix(tx_id)
    buttons = [
        [InlineKeyboardButton("Merchant", callback_data=f"{prefix}_merchant{s}")],
        [InlineKeyboardButton("Description", callback_data=f"{prefix}_description{s}")],
        [InlineKeyboardButton("Amount", callback_data=f"{prefix}_amount{s}")],
        [InlineKeyboardButton("Currency", callback_data=f"{prefix}_currency{s}")],
        [InlineKeyboardButton("Date", callback_data=f"{prefix}_date{s}")],
        [InlineKeyboardButton("Category", callback_data=f"{prefix}_category{s}")],
        [InlineKeyboardButton("Payment Method", callback_data=f"{prefix}_payment_method{s}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"{prefix}_back{s}")],
    ]
    return InlineKeyboardMarkup(buttons)


def category_keyboard(
    prefix: str = "cat",
    back_data: str | None = None,
    tx_id: int | None = None,
) -> InlineKeyboardMarkup:
    s = _suffix(tx_id)
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"{prefix}_{slug}{s}")]
        for name, slug in CATEGORIES
    ]
    if back_data:
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data=back_data)])
    return InlineKeyboardMarkup(buttons)


def currency_keyboard(
    prefix: str = "editcur", tx_id: int | None = None
) -> InlineKeyboardMarkup:
    s = _suffix(tx_id)
    buttons = [
        [
            InlineKeyboardButton("PEN", callback_data=f"{prefix}_PEN{s}"),
            InlineKeyboardButton("USD", callback_data=f"{prefix}_USD{s}"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data=f"{prefix}_back{s}")],
    ]
    return InlineKeyboardMarkup(buttons)


def back_button_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Back", callback_data=callback_data)]]
    )


def yes_no_keyboard(prefix: str, tx_id: int | None = None) -> InlineKeyboardMarkup:
    s = _suffix(tx_id)
    buttons = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"{prefix}_yes{s}"),
            InlineKeyboardButton("❌ No", callback_data=f"{prefix}_no{s}"),
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
