"""Unit tests for db.dynamo functions added in the routing branch.

Covers list_recent_transactions and update_transaction_fields (both the
plain-update path and the TransactWriteItems key-rewrite path).

Run via: python -m unittest tests.test_dynamo
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from db import dynamo


def _full_txn() -> dict:
    """A representative existing transaction with all fields populated.

    Includes the private "_sk" key returned by ``_get_transaction``.
    """
    return {
        "id": 42,
        "_sk": "2026-04-13#00000042",
        "amount": Decimal("30.00"),
        "amount_cents": 3000,
        "currency": "PEN",
        "date": "2026-04-13",
        "merchant": "Wong",
        "description": None,
        "category_id": 2,
        "category_slug": "groceries",
        "category_name": "Groceries",
        "payment_method_id": 1,
        "payment_method_name": "BCP Visa Infinite Sapphire",
        "account_id": 5,
        "telegram_image_id": None,
        "image_path": None,
        "reconciliation_status": "unreconciled",
        "created_at": "2026-04-13T12:00:00Z",
    }


class ListRecentTransactionsTests(unittest.TestCase):
    def test_calls_query_descending_with_limit(self) -> None:
        with patch.object(dynamo, "_table") as mock_table:
            mock_table.query.return_value = {
                "Items": [
                    {"PK": "TXN", "SK": "2026-04-13#00000042", "id": 42, "amount": Decimal("30")},
                    {"PK": "TXN", "SK": "2026-04-12#00000041", "id": 41, "amount": Decimal("12.50")},
                ]
            }
            result = dynamo.list_recent_transactions(5)

        call_kwargs = mock_table.query.call_args.kwargs
        self.assertFalse(call_kwargs["ScanIndexForward"])
        self.assertEqual(call_kwargs["Limit"], 5)
        self.assertEqual(len(result), 2)
        # Internal keys are stripped by _normalize_item.
        self.assertNotIn("PK", result[0])
        self.assertNotIn("SK", result[0])
        self.assertEqual(result[0]["id"], 42)
        # Decimal whole numbers normalize to int.
        self.assertEqual(result[0]["amount"], 30)
        self.assertEqual(result[1]["amount"], 12.5)

    def test_empty_result_returns_empty_list(self) -> None:
        with patch.object(dynamo, "_table") as mock_table:
            mock_table.query.return_value = {"Items": []}
            result = dynamo.list_recent_transactions(10)
        self.assertEqual(result, [])

    def test_limit_coerced_to_int(self) -> None:
        with patch.object(dynamo, "_table") as mock_table:
            mock_table.query.return_value = {"Items": []}
            dynamo.list_recent_transactions("3")  # type: ignore[arg-type]
        self.assertEqual(mock_table.query.call_args.kwargs["Limit"], 3)


class UpdateTransactionFieldsValidationTests(unittest.TestCase):
    def test_missing_transaction_raises_key_error(self) -> None:
        with patch.object(dynamo, "_get_transaction", return_value=None):
            with self.assertRaises(KeyError):
                dynamo.update_transaction_fields(999, {"merchant": "x"})

    def test_disallowed_field_raises_value_error(self) -> None:
        with patch.object(dynamo, "_get_transaction", return_value=_full_txn()):
            with self.assertRaises(ValueError) as ctx:
                dynamo.update_transaction_fields(42, {"id": 99})
            self.assertIn("id", str(ctx.exception))

    def test_multiple_disallowed_fields_listed(self) -> None:
        with patch.object(dynamo, "_get_transaction", return_value=_full_txn()):
            with self.assertRaises(ValueError) as ctx:
                dynamo.update_transaction_fields(
                    42, {"id": 1, "created_at": "x"}
                )
            msg = str(ctx.exception)
            self.assertIn("id", msg)
            self.assertIn("created_at", msg)


class UpdateTransactionFieldsNonKeyEditTests(unittest.TestCase):
    """Edits to non-key fields go through plain UpdateItem."""

    def test_merchant_edit_uses_update_item(self) -> None:
        before = _full_txn()
        after = {**before, "merchant": "Plaza Vea"}

        with patch.object(dynamo, "_get_transaction", side_effect=[before, after]), \
             patch.object(dynamo, "_table") as mock_table:
            result = dynamo.update_transaction_fields(42, {"merchant": "Plaza Vea"})

        mock_table.update_item.assert_called_once()
        # Key-rewrite path not taken.
        mock_table.meta.client.transact_write_items.assert_not_called()

        update_kwargs = mock_table.update_item.call_args.kwargs
        self.assertEqual(update_kwargs["Key"], {"PK": "TXN", "SK": "2026-04-13#00000042"})
        self.assertIn("SET", update_kwargs["UpdateExpression"])
        # Placeholders must be used so DynamoDB reserved words don't bite.
        self.assertEqual(update_kwargs["ExpressionAttributeNames"], {"#f_merchant": "merchant"})
        self.assertEqual(update_kwargs["ExpressionAttributeValues"], {":v_merchant": "Plaza Vea"})
        # Returned item should not leak the internal "_sk" key.
        self.assertNotIn("_sk", result)

    def test_category_edit_sets_three_fields(self) -> None:
        before = _full_txn()
        after = {**before, "category_id": 1, "category_slug": "food_dining", "category_name": "Food & Dining"}

        with patch.object(dynamo, "_get_transaction", side_effect=[before, after]), \
             patch.object(dynamo, "_table") as mock_table:
            dynamo.update_transaction_fields(
                42,
                {
                    "category_id": 1,
                    "category_slug": "food_dining",
                    "category_name": "Food & Dining",
                },
            )

        update_kwargs = mock_table.update_item.call_args.kwargs
        self.assertEqual(set(update_kwargs["ExpressionAttributeNames"].values()),
                         {"category_id", "category_slug", "category_name"})

    def test_payment_method_edit_uses_plain_update(self) -> None:
        before = _full_txn()
        after = {**before, "payment_method_id": 3, "payment_method_name": "Yape"}

        with patch.object(dynamo, "_get_transaction", side_effect=[before, after]), \
             patch.object(dynamo, "_table") as mock_table:
            dynamo.update_transaction_fields(
                42,
                {"payment_method_id": 3, "payment_method_name": "Yape"},
            )

        # Note: payment-method changes do not rewrite the key (account_id is
        # part of GSI1PK, but the routing branch implementation does not
        # rebuild the GSI1 key for pm-only edits — only amount/date trigger
        # the transactional rewrite).
        mock_table.update_item.assert_called_once()
        mock_table.meta.client.transact_write_items.assert_not_called()


class UpdateTransactionFieldsKeyRewriteTests(unittest.TestCase):
    """Edits to amount or date require a delete+put under new keys."""

    def test_amount_edit_uses_transact_write(self) -> None:
        before = _full_txn()
        with patch.object(dynamo, "_get_transaction", return_value=before), \
             patch.object(dynamo, "_account_id_for_payment_method", return_value=5), \
             patch.object(dynamo, "_load_reference"), \
             patch.object(dynamo, "_table") as mock_table:
            result = dynamo.update_transaction_fields(42, {"amount": 25.0})

        # Key-rewrite path: low-level transact_write_items used; plain
        # UpdateItem is NOT used.
        mock_table.update_item.assert_not_called()
        mock_table.meta.client.transact_write_items.assert_called_once()

        items = mock_table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(items), 2)
        self.assertIn("Delete", items[0])
        self.assertIn("Put", items[1])

        # Delete targets the OLD SK.
        delete_key = items[0]["Delete"]["Key"]
        self.assertEqual(delete_key["PK"]["S"], "TXN")
        self.assertEqual(delete_key["SK"]["S"], "2026-04-13#00000042")

        # Put writes under the SAME SK (date didn't change) but new
        # amount_cents / GSI1PK.
        put_item = items[1]["Put"]["Item"]
        self.assertEqual(put_item["amount"]["N"], "25.00")
        self.assertEqual(put_item["amount_cents"]["N"], "2500")
        self.assertEqual(put_item["GSI1PK"]["S"], "AMT#5#2500")
        self.assertEqual(put_item["id"]["N"], "42")
        # Preserved fields.
        self.assertEqual(put_item["created_at"]["S"], "2026-04-13T12:00:00Z")
        self.assertEqual(put_item["reconciliation_status"]["S"], "unreconciled")

        # Returned dict has the new amount.
        self.assertEqual(result["amount"], 25)
        self.assertEqual(result["amount_cents"], 2500)

    def test_date_edit_rewrites_sk_and_gsis(self) -> None:
        before = _full_txn()
        with patch.object(dynamo, "_get_transaction", return_value=before), \
             patch.object(dynamo, "_account_id_for_payment_method", return_value=5), \
             patch.object(dynamo, "_load_reference"), \
             patch.object(dynamo, "_table") as mock_table:
            result = dynamo.update_transaction_fields(42, {"date": "2026-04-20"})

        items = mock_table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
        put_item = items[1]["Put"]["Item"]
        self.assertEqual(put_item["SK"]["S"], "2026-04-20#00000042")
        self.assertEqual(put_item["GSI1SK"]["S"], "2026-04-20#00000042")
        self.assertEqual(put_item["GSI2SK"]["S"], "2026-04-20#00000042")
        self.assertEqual(put_item["date"]["S"], "2026-04-20")
        self.assertEqual(result["date"], "2026-04-20")

    def test_amount_and_date_combined(self) -> None:
        before = _full_txn()
        with patch.object(dynamo, "_get_transaction", return_value=before), \
             patch.object(dynamo, "_account_id_for_payment_method", return_value=5), \
             patch.object(dynamo, "_load_reference"), \
             patch.object(dynamo, "_table") as mock_table:
            dynamo.update_transaction_fields(
                42, {"amount": 99.99, "date": "2026-04-20"}
            )

        put_item = mock_table.meta.client.transact_write_items.call_args.kwargs[
            "TransactItems"
        ][1]["Put"]["Item"]
        self.assertEqual(put_item["SK"]["S"], "2026-04-20#00000042")
        self.assertEqual(put_item["GSI1PK"]["S"], "AMT#5#9999")

    def test_image_path_and_telegram_id_preserved(self) -> None:
        before = _full_txn()
        before["image_path"] = "txn/42/receipt.jpg"
        before["telegram_image_id"] = "AgACAgIAAxk..."

        with patch.object(dynamo, "_get_transaction", return_value=before), \
             patch.object(dynamo, "_account_id_for_payment_method", return_value=5), \
             patch.object(dynamo, "_load_reference"), \
             patch.object(dynamo, "_table") as mock_table:
            dynamo.update_transaction_fields(42, {"amount": 25.0})

        put_item = mock_table.meta.client.transact_write_items.call_args.kwargs[
            "TransactItems"
        ][1]["Put"]["Item"]
        self.assertEqual(put_item["image_path"]["S"], "txn/42/receipt.jpg")
        self.assertEqual(put_item["telegram_image_id"]["S"], "AgACAgIAAxk...")


if __name__ == "__main__":
    unittest.main()
