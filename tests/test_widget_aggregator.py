"""Unit tests for api.aggregator.

Run via: python -m unittest tests.test_widget_aggregator
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from api import aggregator


def _txn(
    amount: float,
    *,
    currency: str = "PEN",
    date_iso: str = "2026-04-15",
    slug: str = "food_dining",
    status: str = "unreconciled",
    txn_id: int = 1,
) -> dict:
    return {
        "id": txn_id,
        "amount": amount,
        "currency": currency,
        "date": date_iso,
        "category_slug": slug,
        "reconciliation_status": status,
    }


class BuildSummaryTests(unittest.TestCase):
    def test_empty_month_returns_zeros(self) -> None:
        with patch.object(aggregator.db, "list_transactions_in_month", return_value=[]):
            result = aggregator.build_summary(date(2026, 4, 28))
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["period"], {"year": 2026, "month": 4})
        self.assertEqual(
            result["totals"],
            {
                "month_pen": 0.0,
                "month_usd": 0.0,
                "today_pen": 0.0,
                "today_usd": 0.0,
            },
        )
        self.assertEqual(result["by_category_pen"], {})
        self.assertEqual(result["by_category_usd"], {})
        self.assertEqual(result["unreconciled_count"], 0)
        self.assertEqual(result["txn_count"], 0)

    def test_pen_and_usd_buckets(self) -> None:
        items = [
            _txn(100.0, currency="PEN", slug="groceries"),
            _txn(50.0, currency="PEN", slug="food_dining"),
            _txn(30.0, currency="USD", slug="shopping"),
        ]
        with patch.object(aggregator.db, "list_transactions_in_month", return_value=items):
            result = aggregator.build_summary(date(2026, 4, 28))
        self.assertEqual(result["totals"]["month_pen"], 150.0)
        self.assertEqual(result["totals"]["month_usd"], 30.0)
        self.assertEqual(
            result["by_category_pen"], {"groceries": 100.0, "food_dining": 50.0}
        )
        self.assertEqual(result["by_category_usd"], {"shopping": 30.0})
        self.assertEqual(result["txn_count"], 3)

    def test_today_split(self) -> None:
        items = [
            _txn(80.0, date_iso="2026-04-28"),
            _txn(120.0, date_iso="2026-04-15"),
            _txn(40.0, date_iso="2026-04-28", currency="USD"),
        ]
        with patch.object(aggregator.db, "list_transactions_in_month", return_value=items):
            result = aggregator.build_summary(date(2026, 4, 28))
        self.assertEqual(result["totals"]["month_pen"], 200.0)
        self.assertEqual(result["totals"]["today_pen"], 80.0)
        self.assertEqual(result["totals"]["month_usd"], 40.0)
        self.assertEqual(result["totals"]["today_usd"], 40.0)

    def test_unreconciled_count(self) -> None:
        items = [
            _txn(100.0, status="unreconciled"),
            _txn(50.0, status="reconciled"),
            _txn(20.0, status="unreconciled"),
        ]
        with patch.object(aggregator.db, "list_transactions_in_month", return_value=items):
            result = aggregator.build_summary(date(2026, 4, 28))
        self.assertEqual(result["unreconciled_count"], 2)

    def test_missing_currency_defaults_to_pen(self) -> None:
        items = [{"amount": 50.0, "date": "2026-04-15", "category_slug": "groceries"}]
        with patch.object(aggregator.db, "list_transactions_in_month", return_value=items):
            result = aggregator.build_summary(date(2026, 4, 28))
        self.assertEqual(result["totals"]["month_pen"], 50.0)
        self.assertEqual(result["totals"]["month_usd"], 0.0)

    def test_missing_category_slug_uses_uncategorized(self) -> None:
        items = [{"amount": 50.0, "currency": "PEN", "date": "2026-04-15"}]
        with patch.object(aggregator.db, "list_transactions_in_month", return_value=items):
            result = aggregator.build_summary(date(2026, 4, 28))
        self.assertEqual(result["by_category_pen"], {"uncategorized": 50.0})

    def test_passes_year_and_month_to_db_helper(self) -> None:
        with patch.object(
            aggregator.db, "list_transactions_in_month", return_value=[]
        ) as mock_db:
            aggregator.build_summary(date(2025, 11, 5))
        mock_db.assert_called_once_with(2025, 11)

    def test_as_of_is_utc_z_suffixed(self) -> None:
        with patch.object(aggregator.db, "list_transactions_in_month", return_value=[]):
            result = aggregator.build_summary(date(2026, 4, 28))
        self.assertTrue(result["as_of"].endswith("Z"))
        # Format: YYYY-MM-DDTHH:MM:SSZ → 20 chars
        self.assertEqual(len(result["as_of"]), 20)

    def test_decimal_amounts_are_summed_correctly(self) -> None:
        from decimal import Decimal

        items = [
            _txn(Decimal("12.50"), slug="groceries"),
            _txn(Decimal("7.50"), slug="groceries"),
        ]
        with patch.object(aggregator.db, "list_transactions_in_month", return_value=items):
            result = aggregator.build_summary(date(2026, 4, 28))
        self.assertEqual(result["totals"]["month_pen"], 20.0)
        self.assertEqual(result["by_category_pen"]["groceries"], 20.0)


if __name__ == "__main__":
    unittest.main()
