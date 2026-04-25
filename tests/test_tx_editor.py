"""Unit tests for agent.tx_editor.

Run via: python -m unittest tests.test_tx_editor
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from agent import tx_editor


SAMPLE_TXN = {
    "id": 42,
    "merchant": "Wong",
    "description": None,
    "amount": 30.0,
    "currency": "PEN",
    "date": "2026-04-13",
    "category_slug": "groceries",
    "payment_method_name": "BCP Visa Infinite Sapphire",
}


def _mock_response(payload: dict) -> dict:
    return {
        "output": {
            "message": {
                "content": [{"text": json.dumps(payload)}],
            }
        }
    }


class ParseEditRequestTests(unittest.TestCase):
    def test_amount_edit(self) -> None:
        with patch.object(tx_editor, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = _mock_response(
                {"field": "amount", "new_value": 25.0, "confident": True}
            )
            result = tx_editor.parse_edit_request("change amount to 25", SAMPLE_TXN)
        self.assertEqual(result["field"], "amount")
        self.assertEqual(result["new_value"], 25.0)
        self.assertTrue(result["confident"])

    def test_category_edit(self) -> None:
        with patch.object(tx_editor, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = _mock_response(
                {"field": "category_slug", "new_value": "food_dining", "confident": True}
            )
            result = tx_editor.parse_edit_request(
                "fix the category, it was food", SAMPLE_TXN
            )
        self.assertEqual(result["field"], "category_slug")
        self.assertEqual(result["new_value"], "food_dining")

    def test_payment_method_alias_emitted_verbatim(self) -> None:
        with patch.object(tx_editor, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = _mock_response(
                {"field": "payment_method_id", "new_value": "yape", "confident": True}
            )
            result = tx_editor.parse_edit_request(
                "actualiza el medio de pago a yape", SAMPLE_TXN
            )
        self.assertEqual(result["field"], "payment_method_id")
        self.assertEqual(result["new_value"], "yape")
        self.assertTrue(result["confident"])

    def test_ambiguous_returns_unconfident(self) -> None:
        with patch.object(tx_editor, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = _mock_response(
                {"field": None, "new_value": None, "confident": False}
            )
            result = tx_editor.parse_edit_request("change it", SAMPLE_TXN)
        self.assertIsNone(result["field"])
        self.assertIsNone(result["new_value"])
        self.assertFalse(result["confident"])

    def test_fenced_json_is_parsed(self) -> None:
        fenced = "```json\n" + json.dumps(
            {"field": "merchant", "new_value": "Plaza Vea", "confident": True}
        ) + "\n```"
        with patch.object(tx_editor, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = {
                "output": {"message": {"content": [{"text": fenced}]}}
            }
            result = tx_editor.parse_edit_request(
                "rename merchant to Plaza Vea", SAMPLE_TXN
            )
        self.assertEqual(result["field"], "merchant")
        self.assertEqual(result["new_value"], "Plaza Vea")

    def test_bad_json_then_good_on_retry(self) -> None:
        good = _mock_response(
            {"field": "amount", "new_value": 50.0, "confident": True}
        )
        bad = {"output": {"message": {"content": [{"text": "not json"}]}}}
        with patch.object(tx_editor, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.side_effect = [bad, good]
            result = tx_editor.parse_edit_request("amount 50", SAMPLE_TXN)
        self.assertEqual(result["field"], "amount")
        self.assertEqual(mock_bedrock.converse.call_count, 2)

    def test_two_bad_responses_returns_null_with_error(self) -> None:
        bad = {"output": {"message": {"content": [{"text": "still not json"}]}}}
        with patch.object(tx_editor, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = bad
            result = tx_editor.parse_edit_request("amount 50", SAMPLE_TXN)
        self.assertIsNone(result["field"])
        self.assertFalse(result["confident"])
        self.assertIn("error", result)

    def test_bedrock_failure_returns_null_with_error(self) -> None:
        with patch.object(tx_editor, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.side_effect = Exception("boom")
            result = tx_editor.parse_edit_request("amount 50", SAMPLE_TXN)
        self.assertIsNone(result["field"])
        self.assertFalse(result["confident"])
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
