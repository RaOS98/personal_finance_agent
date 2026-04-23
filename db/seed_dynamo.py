"""One-shot reference-data seeder for the DynamoDB table.

Run locally with AWS credentials:

    python -m db.seed_dynamo

Idempotent: re-running overwrites existing REF items with the canonical values.
"""

from __future__ import annotations

import boto3

import config


CATEGORIES = [
    (1, "Food & Dining", "food_dining"),
    (2, "Groceries", "groceries"),
    (3, "Transportation", "transportation"),
    (4, "Housing", "housing"),
    (5, "Utilities", "utilities"),
    (6, "Health", "health"),
    (7, "Personal Care", "personal_care"),
    (8, "Entertainment", "entertainment"),
    (9, "Shopping", "shopping"),
    (10, "Education", "education"),
    (11, "Work", "work"),
    (12, "Other", "other"),
]

ACCOUNTS = [
    (1, "Visa Infinite Sapphire", "BCP", "PEN", "credit_card"),
    (2, "Amex Platinum", "BCP", "PEN", "credit_card"),
    (3, "Soles Current Account", "BCP", "PEN", "current_account"),
    (4, "Dollars Current Account", "BCP", "USD", "current_account"),
]

PAYMENT_METHODS = [
    (1, "BCP Visa Infinite Sapphire", ["sapphire", "sap", "visa"], 1),
    (2, "BCP Amex Platinum", ["amex", "platinum"], 2),
    (3, "Yape", ["yape"], 3),
    (4, "BCP Transfer (Soles)", ["transfer", "bcp"], 3),
    (5, "BCP Transfer (Dollars)", ["usd", "dollars"], 4),
    (6, "Cash", ["cash", "efectivo"], None),
]


def main() -> None:
    table = boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(
        config.DYNAMODB_TABLE
    )

    with table.batch_writer() as batch:
        for cat_id, name, slug in CATEGORIES:
            batch.put_item(
                Item={
                    "PK": "REF",
                    "SK": f"CATEGORY#{slug}",
                    "kind": "category",
                    "id": cat_id,
                    "name": name,
                    "slug": slug,
                }
            )
        for acc_id, name, bank, currency, acc_type in ACCOUNTS:
            batch.put_item(
                Item={
                    "PK": "REF",
                    "SK": f"ACCOUNT#{acc_id:04d}",
                    "kind": "account",
                    "id": acc_id,
                    "name": name,
                    "bank": bank,
                    "currency": currency,
                    "type": acc_type,
                }
            )
        for pm_id, name, aliases, account_id in PAYMENT_METHODS:
            item = {
                "PK": "REF",
                "SK": f"PM#{pm_id:04d}",
                "kind": "payment_method",
                "id": pm_id,
                "name": name,
                "aliases": aliases,
            }
            if account_id is not None:
                item["account_id"] = account_id
            batch.put_item(Item=item)

    print(
        f"Seeded {len(CATEGORIES)} categories, {len(ACCOUNTS)} accounts, "
        f"{len(PAYMENT_METHODS)} payment methods into {config.DYNAMODB_TABLE}."
    )


if __name__ == "__main__":
    main()
