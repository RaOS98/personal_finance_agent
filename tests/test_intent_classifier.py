"""Unit tests for agent.intent_classifier.

Run via: python -m unittest tests.test_intent_classifier
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from agent import intent_classifier


def _mock_response(intent: str, confident: bool = True) -> dict:
    body = json.dumps({"intent": intent, "confident": confident})
    return {
        "output": {
            "message": {
                "content": [{"text": body}],
            }
        }
    }


class ClassifyIntentTests(unittest.TestCase):
    def test_new_transaction(self) -> None:
        with patch.object(intent_classifier, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = _mock_response("new_transaction")
            result = intent_classifier.classify_intent("starbucks 25 soles yape")
        self.assertEqual(result["intent"], "new_transaction")
        self.assertTrue(result["confident"])

    def test_question_text_maps_to_new_transaction(self) -> None:
        """NL questions are no longer a separate intent; classifier may send new_transaction."""
        with patch.object(intent_classifier, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = _mock_response("new_transaction")
            result = intent_classifier.classify_intent(
                "cuanto gaste en comida este mes"
            )
        self.assertEqual(result["intent"], "new_transaction")
        self.assertTrue(result["confident"])

    def test_edit(self) -> None:
        with patch.object(intent_classifier, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.return_value = _mock_response("edit")
            result = intent_classifier.classify_intent(
                "change the last one to 30 soles"
            )
        self.assertEqual(result["intent"], "edit")
        self.assertTrue(result["confident"])

    def test_bedrock_failure_fails_open_to_new_transaction(self) -> None:
        with patch.object(intent_classifier, "_bedrock") as mock_bedrock:
            mock_bedrock.converse.side_effect = Exception("boom")
            result = intent_classifier.classify_intent("anything")
        self.assertEqual(result["intent"], "new_transaction")
        self.assertFalse(result["confident"])
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
