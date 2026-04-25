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

    BCP statements may use formats like: 1,234.56 or 1.234,56 or just 123.45.
    Negatives may be expressed with a leading "-" or with surrounding
    parentheses (e.g. ``(145.30)`` -> ``-145.30``); a trailing "CR" or
    leading "ABONO " marker is also treated as a credit.
    """
    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    negative = False

    upper = text.upper()
    if upper.endswith("CR") or upper.endswith("CR."):
        negative = True
        text = re.sub(r"(?i)cr\.?$", "", text).strip()
    if upper.startswith("ABONO"):
        negative = True
        text = re.sub(r"(?i)^abono[s]?\s*", "", text).strip()

    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    if text.startswith("-"):
        negative = not negative
        text = text[1:].strip()
    elif text.startswith("+"):
        text = text[1:].strip()

    text = re.sub(r"[S/.$\s]", "", text)

    if not text:
        return None

    if "," in text and "." in text:
        if text.rindex(",") < text.rindex("."):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        after_comma = text.split(",")[-1]
        if len(after_comma) == 2:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    try:
        result = float(text)
    except ValueError:
        return None

    if negative:
        result = -abs(result)
    return result


def parse_statement_pdf(pdf_source: str | bytes) -> list[dict]:
    """Parse a BCP bank statement PDF and extract transaction lines.

    Args:
        pdf_source: Filesystem path to the PDF, or raw PDF bytes (e.g. from Telegram).

    Returns:
        A list of dicts, each with keys: date (YYYY-MM-DD), description (str),
        amount (float). Charges are positive; credits/refunds are negative.
        BCP statements typically render charges in a "Cargos/Consumos" column
        and credits in a separate "Abonos/Pagos" column. When two numeric
        columns appear on the same row, the later (right-most) is treated as
        the credit and its sign is flipped. A single numeric column is kept
        with whatever sign ``_parse_amount`` returned (including parenthesized
        or "CR"-suffixed credits).

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

                    description_parts: list[str] = []
                    numeric_values: list[float] = []

                    for cell in row[1:]:
                        cell_text = cell.strip() if cell else ""
                        if not cell_text:
                            continue

                        parsed = _parse_amount(cell_text)
                        if parsed is not None:
                            numeric_values.append(parsed)
                        else:
                            description_parts.append(cell_text)

                    if not numeric_values:
                        continue

                    if len(numeric_values) == 1:
                        amount = numeric_values[0]
                    else:
                        # Two-column layout: charges first, credits second.
                        # The non-zero column wins; if both are populated
                        # (rare), the credit takes precedence and is negated.
                        debit, credit = numeric_values[0], numeric_values[-1]
                        if credit and abs(credit) > 0:
                            amount = -abs(credit)
                        else:
                            amount = debit

                    if amount == 0:
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
