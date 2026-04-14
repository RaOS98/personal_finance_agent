import re
from io import BytesIO

import pdfplumber


# Matches DD/MM/YYYY or DD/MM formats
DATE_PATTERN = re.compile(r"^(\d{2}/\d{2}/\d{4}|\d{2}/\d{2})$")


def _normalize_date(raw_date: str) -> str:
    """Convert DD/MM/YYYY or DD/MM to YYYY-MM-DD format.

    For DD/MM (without year), assumes the current year.
    """
    parts = raw_date.split("/")
    if len(parts) == 3:
        day, month, year = parts
    elif len(parts) == 2:
        day, month = parts
        from datetime import date

        year = str(date.today().year)
    else:
        raise ValueError(f"Unexpected date format: {raw_date}")

    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def _parse_amount(value: str) -> float | None:
    """Parse an amount string to float, handling common formats.

    BCP statements may use formats like: 1,234.56 or 1.234,56 or just 123.45
    """
    if not value:
        return None

    text = value.strip()
    # Remove currency symbols and whitespace
    text = re.sub(r"[S/.$\s]", "", text)

    if not text:
        return None

    # Handle thousand separators: if comma is before dot, commas are thousands
    # e.g., 1,234.56
    if "," in text and "." in text:
        if text.rindex(",") < text.rindex("."):
            text = text.replace(",", "")
        else:
            # e.g., 1.234,56 — dot is thousands, comma is decimal
            text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        # Could be thousands (1,234) or decimal (123,45)
        # If exactly 2 digits after comma, treat as decimal
        after_comma = text.split(",")[-1]
        if len(after_comma) == 2:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    try:
        return float(text)
    except ValueError:
        return None


def parse_statement_pdf(pdf_source: str | bytes) -> list[dict]:
    """Parse a BCP bank statement PDF and extract transaction lines.

    Args:
        pdf_source: Filesystem path to the PDF, or raw PDF bytes (e.g. from Telegram).

    Returns:
        A list of dicts, each with keys: date (YYYY-MM-DD), description (str),
        amount (float). Only debit/charge lines (positive amounts) are included.

    Raises:
        ValueError: If the PDF cannot be parsed or contains no valid data.
    """
    try:
        if isinstance(pdf_source, bytes):
            pdf = pdfplumber.open(BytesIO(pdf_source))
        else:
            pdf = pdfplumber.open(pdf_source)
    except Exception as e:
        raise ValueError(f"Could not open PDF file: {e}")

    transactions = []

    try:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue

            for table in tables:
                for row in table:
                    if not row or not row[0]:
                        continue

                    first_cell = row[0].strip() if row[0] else ""

                    if not DATE_PATTERN.match(first_cell):
                        continue

                    try:
                        date_str = _normalize_date(first_cell)
                    except ValueError:
                        continue

                    # Extract description from the second column (or combined
                    # middle columns)
                    description_parts = []
                    amount = None

                    # Walk columns after the date: collect text as description
                    # until we find a parseable amount in the later columns
                    for cell in row[1:]:
                        cell_text = cell.strip() if cell else ""
                        if not cell_text:
                            continue

                        parsed = _parse_amount(cell_text)
                        if parsed is not None:
                            # Take the last valid amount in the row (typically
                            # the debit or charge column)
                            amount = parsed
                        else:
                            description_parts.append(cell_text)

                    if amount is None or amount <= 0:
                        continue

                    description = " ".join(description_parts).strip()
                    if not description:
                        description = "Unknown"

                    transactions.append(
                        {
                            "date": date_str,
                            "description": description,
                            "amount": amount,
                        }
                    )
    finally:
        pdf.close()

    if not transactions:
        raise ValueError(
            "No transaction lines found in the PDF. The file may not be a "
            "supported BCP statement format, or the table structure is unexpected."
        )

    return transactions
