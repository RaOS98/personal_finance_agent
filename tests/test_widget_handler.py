"""Unit tests for api.widget_handler.

Run via: python -m unittest tests.test_widget_handler
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import config
from api import widget_handler


_VALID_TOKEN = "test-token-not-real"


def _event(
    method: str = "GET",
    path: str = "/widget/summary",
    auth: str | None = None,
    *,
    auth_header_name: str = "authorization",
) -> dict:
    headers: dict[str, str] = {}
    if auth is not None:
        headers[auth_header_name] = auth
    return {
        "version": "2.0",
        "rawPath": path,
        "headers": headers,
        "requestContext": {"http": {"method": method, "path": path}},
    }


def _summary() -> dict:
    return {
        "version": 1,
        "as_of": "2026-04-28T16:32:11Z",
        "period": {"year": 2026, "month": 4},
        "totals": {
            "month_pen": 1240.50,
            "month_usd": 0.0,
            "today_pen": 80.00,
            "today_usd": 0.0,
        },
        "by_category_pen": {"groceries": 380.0, "food_dining": 220.0},
        "by_category_usd": {},
        "unreconciled_count": 3,
        "txn_count": 47,
    }


class HandlerAuthTests(unittest.TestCase):
    def test_missing_authorization_returns_401(self) -> None:
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN):
            resp = widget_handler.handler(_event(), None)
        self.assertEqual(resp["statusCode"], 401)
        body = json.loads(resp["body"])
        self.assertEqual(body, {"error": "unauthorized"})

    def test_wrong_token_returns_401(self) -> None:
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN):
            resp = widget_handler.handler(_event(auth="Bearer wrong-token"), None)
        self.assertEqual(resp["statusCode"], 401)

    def test_missing_bearer_prefix_returns_401(self) -> None:
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN):
            resp = widget_handler.handler(_event(auth=_VALID_TOKEN), None)
        self.assertEqual(resp["statusCode"], 401)

    def test_correct_token_returns_200(self) -> None:
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN), patch.object(
            widget_handler.aggregator, "build_summary", return_value=_summary()
        ):
            resp = widget_handler.handler(_event(auth=f"Bearer {_VALID_TOKEN}"), None)
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["totals"]["month_pen"], 1240.5)

    def test_authorization_header_name_case_insensitive(self) -> None:
        # The bot's Telegram handler tolerates both casings; we mirror that
        # so curl/dev tools that send `Authorization` keep working alongside
        # API Gateway's lowercased pass-through.
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN), patch.object(
            widget_handler.aggregator, "build_summary", return_value=_summary()
        ):
            resp = widget_handler.handler(
                _event(
                    auth=f"Bearer {_VALID_TOKEN}",
                    auth_header_name="Authorization",
                ),
                None,
            )
        self.assertEqual(resp["statusCode"], 200)

    def test_empty_configured_token_refuses_request(self) -> None:
        # Misconfiguration (SSM denied or never provisioned) must fail closed
        # so we never accidentally serve without auth.
        with patch.object(config, "WIDGET_BEARER_TOKEN", ""):
            resp = widget_handler.handler(_event(auth=f"Bearer {_VALID_TOKEN}"), None)
        self.assertEqual(resp["statusCode"], 401)


class HandlerRoutingTests(unittest.TestCase):
    def test_options_preflight_skips_auth(self) -> None:
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN):
            resp = widget_handler.handler(_event(method="OPTIONS"), None)
        self.assertEqual(resp["statusCode"], 204)
        self.assertIn("Access-Control-Allow-Origin", resp["headers"])
        self.assertIn("Access-Control-Allow-Methods", resp["headers"])
        self.assertIn("Access-Control-Allow-Headers", resp["headers"])

    def test_unknown_path_returns_404(self) -> None:
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN):
            resp = widget_handler.handler(
                _event(path="/widget/nope", auth=f"Bearer {_VALID_TOKEN}"), None
            )
        self.assertEqual(resp["statusCode"], 404)

    def test_post_to_summary_returns_404(self) -> None:
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN):
            resp = widget_handler.handler(
                _event(method="POST", auth=f"Bearer {_VALID_TOKEN}"), None
            )
        self.assertEqual(resp["statusCode"], 404)

    def test_aggregator_exception_returns_500(self) -> None:
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN), patch.object(
            widget_handler.aggregator,
            "build_summary",
            side_effect=RuntimeError("boom"),
        ):
            resp = widget_handler.handler(_event(auth=f"Bearer {_VALID_TOKEN}"), None)
        self.assertEqual(resp["statusCode"], 500)
        self.assertEqual(json.loads(resp["body"]), {"error": "internal_error"})


class HandlerResponseShapeTests(unittest.TestCase):
    def test_response_includes_cors_and_no_store(self) -> None:
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN), patch.object(
            widget_handler.aggregator, "build_summary", return_value=_summary()
        ):
            resp = widget_handler.handler(_event(auth=f"Bearer {_VALID_TOKEN}"), None)
        self.assertIn("Access-Control-Allow-Origin", resp["headers"])
        self.assertEqual(resp["headers"]["Cache-Control"], "no-store")
        self.assertEqual(resp["headers"]["Content-Type"], "application/json")

    def test_response_origin_respects_config(self) -> None:
        with patch.object(config, "WIDGET_BEARER_TOKEN", _VALID_TOKEN), patch.object(
            config, "WIDGET_ALLOWED_ORIGINS", "https://dashboard.example.com"
        ), patch.object(
            widget_handler.aggregator, "build_summary", return_value=_summary()
        ):
            resp = widget_handler.handler(_event(auth=f"Bearer {_VALID_TOKEN}"), None)
        self.assertEqual(
            resp["headers"]["Access-Control-Allow-Origin"],
            "https://dashboard.example.com",
        )


if __name__ == "__main__":
    unittest.main()
