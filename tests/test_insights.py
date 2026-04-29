"""Unit tests for api.insights formatting (no DynamoDB / no Bedrock)."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from api import insights


class InsightsFormatTests(unittest.TestCase):
    def test_format_returns_none_when_no_txns(self) -> None:
        summary = {"txn_count": 0, "totals": {}, "by_category_pen": {}}
        self.assertIsNone(
            insights.format_insights_message(
                date(2026, 4, 28), summary, {}
            )
        )

    def test_format_includes_mtd_and_week_lines(self) -> None:
        summary = {
            "txn_count": 12,
            "totals": {
                "month_pen": 100.0,
                "month_usd": 5.0,
                "today_pen": 10.0,
                "today_usd": 0.0,
            },
            "by_category_pen": {"groceries": 60.0, "food_dining": 40.0},
            "unreconciled_count": 2,
        }
        week = {"groceries": 25.0, "transportation": 15.0}
        out = insights.format_insights_message(date(2026, 4, 28), summary, week)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("Week of 2026-04-27", out)
        self.assertIn("Month-to-date (PEN): S/. 100.00", out)
        self.assertIn("USD MTD", out)
        self.assertIn("Top categories MTD", out)
        self.assertIn("groceries", out)
        self.assertIn("Last 7 days", out)
        self.assertIn("Unreconciled txns: 2", out)
        self.assertIn("Logged txns this month: 12", out)

    def test_week_start_monday(self) -> None:
        # Wednesday 2026-04-28 -> week starts Monday 2026-04-27
        self.assertEqual(
            insights.week_start_monday(date(2026, 4, 28)),
            date(2026, 4, 27),
        )

    def test_top_n_categories(self) -> None:
        t = {"a": 1.0, "b": 5.0, "c": 3.0}
        top = insights.top_n_categories(t, 2)
        self.assertEqual(top, [("b", 5.0), ("c", 3.0)])

    @patch.object(insights.db, "list_transactions_between")
    def test_rolling_week_pen_by_category(self, mock_between: MagicMock) -> None:
        mock_between.return_value = [
            {"currency": "PEN", "category_slug": "groceries", "amount": 10},
            {"currency": "PEN", "category_slug": "groceries", "amount": 5},
            {"currency": "USD", "category_slug": "shopping", "amount": 99},
        ]
        today = date(2026, 4, 28)
        pen = insights.rolling_week_pen_by_category(today, 7)
        self.assertEqual(pen, {"groceries": 15.0})
        mock_between.assert_called_once()


if __name__ == "__main__":
    unittest.main()
