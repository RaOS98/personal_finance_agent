"""Unit tests for agent.query_agent.

Run via: python -m unittest tests.test_query_agent
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from agent import query_agent


def _final_message(payload: dict) -> dict:
    return {
        "stopReason": "end_turn",
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": json.dumps(payload)}],
            }
        },
    }


def _tool_use_message(name: str, tool_input: dict, tool_use_id: str = "tu_1") -> dict:
    return {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}}
                ],
            }
        },
    }


class AnswerQueryTests(unittest.TestCase):
    def test_direct_answer_no_tools(self) -> None:
        with patch.object(query_agent, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = _final_message(
                {"answer": "S/. 312.50", "source_txn_ids": [42, 47, 51]}
            )
            result = query_agent.answer_query("how much on food this month?")
        self.assertEqual(result["answer"], "S/. 312.50")
        self.assertEqual(result["source_txn_ids"], [42, 47, 51])
        self.assertNotIn("error", result)

    def test_tool_use_then_final(self) -> None:
        with patch.object(query_agent, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.side_effect = [
                _tool_use_message(
                    "get_today", {}, tool_use_id="tu_today"
                ),
                _final_message(
                    {"answer": "Today is 2026-04-24.", "source_txn_ids": []}
                ),
            ]
            result = query_agent.answer_query("what's today?")
        self.assertIn("2026", result["answer"] or "")
        self.assertEqual(mock_bedrock.converse.call_count, 2)

    def test_iteration_cap_returns_error(self) -> None:
        # Always return tool_use → loop runs the full 4 iterations and
        # exits with the cap-exceeded error.
        with patch.object(query_agent, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = _tool_use_message(
                "get_today", {}, tool_use_id="tu_loop"
            )
            result = query_agent.answer_query("loop forever")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["source_txn_ids"], [])
        self.assertIn("error", result)
        self.assertIn("4 iterations", result["error"])

    def test_bedrock_failure_returns_error(self) -> None:
        with patch.object(query_agent, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.side_effect = Exception("throttled")
            result = query_agent.answer_query("anything")
        self.assertIsNone(result["answer"])
        self.assertIn("error", result)

    def test_final_text_not_json_falls_back_to_text(self) -> None:
        with patch.object(query_agent, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = {
                "stopReason": "end_turn",
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": "I have no idea."}],
                    }
                },
            }
            result = query_agent.answer_query("?")
        self.assertEqual(result["answer"], "I have no idea.")
        self.assertEqual(result["source_txn_ids"], [])
        self.assertIn("error", result)

    def test_source_txn_ids_coerced_and_filtered(self) -> None:
        # String digits are coerced; non-digit strings are dropped.
        with patch.object(query_agent, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = _final_message(
                {"answer": "ok", "source_txn_ids": ["42", 47, "not-a-num"]}
            )
            result = query_agent.answer_query("?")
        self.assertEqual(result["source_txn_ids"], [42, 47])


class ExecuteToolTests(unittest.TestCase):
    def test_get_today_returns_iso_date(self) -> None:
        result = query_agent._execute_tool(
            {"name": "get_today", "input": {}, "toolUseId": "tu_t"}
        )
        content = result["toolResult"]["content"][0]["json"]
        self.assertIn("today", content)
        # ISO YYYY-MM-DD shape.
        self.assertRegex(content["today"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(result["toolResult"]["status"], "success")

    def test_unknown_tool_returns_error(self) -> None:
        result = query_agent._execute_tool(
            {"name": "no_such_tool", "input": {}, "toolUseId": "tu_x"}
        )
        self.assertEqual(result["toolResult"]["status"], "error")

    def test_list_recent_transactions_invokes_db(self) -> None:
        with patch.object(query_agent.db, "list_recent_transactions") as mock_list:
            mock_list.return_value = [{"id": 1}, {"id": 2}]
            result = query_agent._execute_tool(
                {
                    "name": "list_recent_transactions",
                    "input": {"limit": 5},
                    "toolUseId": "tu_l",
                }
            )
        mock_list.assert_called_once_with(limit=5)
        content = result["toolResult"]["content"][0]["json"]
        self.assertEqual(len(content["transactions"]), 2)

    def test_list_recent_transactions_caps_limit_at_50(self) -> None:
        with patch.object(query_agent.db, "list_recent_transactions") as mock_list:
            mock_list.return_value = []
            query_agent._execute_tool(
                {
                    "name": "list_recent_transactions",
                    "input": {"limit": 9999},
                    "toolUseId": "tu_l",
                }
            )
        mock_list.assert_called_once_with(limit=50)

    def test_tool_exception_returns_error_envelope(self) -> None:
        with patch.object(query_agent.db, "list_recent_transactions") as mock_list:
            mock_list.side_effect = RuntimeError("dynamo down")
            result = query_agent._execute_tool(
                {
                    "name": "list_recent_transactions",
                    "input": {"limit": 5},
                    "toolUseId": "tu_l",
                }
            )
        self.assertEqual(result["toolResult"]["status"], "error")


class QueryHelpersTests(unittest.TestCase):
    def test_query_transactions_filters_by_payment_method(self) -> None:
        items = [
            {"id": 1, "payment_method_id": 3, "category_slug": "food_dining"},
            {"id": 2, "payment_method_id": 7, "category_slug": "food_dining"},
        ]

        with patch.object(query_agent.db, "_table") as mock_table, \
             patch.object(query_agent.db, "_normalize_item", side_effect=lambda x: x), \
             patch.object(query_agent.db, "resolve_payment_method") as mock_resolve:
            mock_table.query.return_value = {"Items": items}
            mock_resolve.return_value = {"id": 3}
            result = query_agent._query_transactions(
                date_from="2026-04-01",
                date_to="2026-04-30",
                payment_method_alias="yape",
            )
        self.assertEqual(len(result["transactions"]), 1)
        self.assertEqual(result["transactions"][0]["id"], 1)
        self.assertEqual(result["total_matched"], 1)

    def test_query_transactions_unknown_payment_method_returns_note(self) -> None:
        with patch.object(query_agent.db, "_table") as mock_table, \
             patch.object(query_agent.db, "_normalize_item", side_effect=lambda x: x), \
             patch.object(query_agent.db, "resolve_payment_method", return_value=None):
            mock_table.query.return_value = {"Items": []}
            result = query_agent._query_transactions(
                date_from="2026-04-01",
                date_to="2026-04-30",
                payment_method_alias="bogus",
            )
        self.assertEqual(result["transactions"], [])
        self.assertIn("note", result)

    def test_query_transactions_filters_by_category(self) -> None:
        items = [
            {"id": 1, "category_slug": "food_dining"},
            {"id": 2, "category_slug": "groceries"},
            {"id": 3, "category_slug": "food_dining"},
        ]
        with patch.object(query_agent.db, "_table") as mock_table, \
             patch.object(query_agent.db, "_normalize_item", side_effect=lambda x: x):
            mock_table.query.return_value = {"Items": items}
            result = query_agent._query_transactions(
                date_from="2026-04-01",
                date_to="2026-04-30",
                category_slug="food_dining",
            )
        self.assertEqual({t["id"] for t in result["transactions"]}, {1, 3})

    def test_query_transactions_truncated_flag(self) -> None:
        items = [{"id": i, "category_slug": "food_dining"} for i in range(60)]
        with patch.object(query_agent.db, "_table") as mock_table, \
             patch.object(query_agent.db, "_normalize_item", side_effect=lambda x: x):
            mock_table.query.return_value = {"Items": items}
            result = query_agent._query_transactions(
                date_from="2026-04-01",
                date_to="2026-04-30",
            )
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["transactions"]), 50)
        self.assertEqual(result["total_matched"], 60)

    def test_aggregate_by_category_sums_per_currency(self) -> None:
        items = [
            {"category_slug": "food_dining", "currency": "PEN", "amount": 30.0},
            {"category_slug": "food_dining", "currency": "PEN", "amount": 15.0},
            {"category_slug": "groceries", "currency": "PEN", "amount": 100.0},
            {"category_slug": "food_dining", "currency": "USD", "amount": 10.0},
        ]
        with patch.object(query_agent.db, "_table") as mock_table, \
             patch.object(query_agent.db, "_normalize_item", side_effect=lambda x: x):
            mock_table.query.return_value = {"Items": items}
            result = query_agent._aggregate_by_category(
                date_from="2026-04-01",
                date_to="2026-04-30",
            )
        self.assertEqual(result["transaction_count"], 4)
        self.assertEqual(result["totals_by_category"]["food_dining"]["PEN"], 45.0)
        self.assertEqual(result["totals_by_category"]["food_dining"]["USD"], 10.0)
        self.assertEqual(result["totals_by_category"]["groceries"]["PEN"], 100.0)


class JsonParsingTests(unittest.TestCase):
    def test_fenced_json_is_stripped(self) -> None:
        text = "```json\n" + json.dumps({"answer": "x", "source_txn_ids": []}) + "\n```"
        parsed = query_agent._parse_json(text)
        self.assertEqual(parsed["answer"], "x")


if __name__ == "__main__":
    unittest.main()
