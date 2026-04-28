"""AWS Lambda entrypoint for the read-only widget / dashboard API.

API Gateway (HTTP API v2.0 payload) hands every request here. The handler
authenticates against a bearer token in SSM, routes on ``rawPath``, and
emits CORS headers on every response so the same Lambda can later back a
static web dashboard without a second deploy.

The router is intentionally a flat ``if`` chain rather than a Flask-style
framework: there's only one production endpoint, and the cold-start budget
for a 256 MB Lambda is better spent on aggregation than on importing
routing libraries.
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import config
from api import aggregator


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# Peru is UTC-5 year-round (no DST). Hardcoded to avoid pulling tzdata into
# the Lambda image; flip this constant if the user ever moves.
_LOCAL_TZ_OFFSET_HOURS = -5


def _cors_headers() -> dict[str, str]:
    origin = config.WIDGET_ALLOWED_ORIGINS or "*"
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "600",
    }


def _response(
    status: int,
    body: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        **_cors_headers(),
    }
    if body is None:
        body_str = ""
    elif isinstance(body, str):
        body_str = body
    else:
        body_str = json.dumps(body, separators=(",", ":"))
    return {
        "statusCode": status,
        "headers": headers,
        "body": body_str,
    }


def _verify_bearer(event: dict) -> bool:
    """Constant-time compare of ``Authorization: Bearer <token>``.

    Returns False when the configured token is empty (e.g. SSM denied or
    parameter never provisioned) so that a misconfigured deploy fails closed
    rather than serving without auth.
    """
    expected = config.WIDGET_BEARER_TOKEN
    if not expected:
        logger.error("WIDGET_BEARER_TOKEN is empty; refusing request")
        return False
    headers = event.get("headers") or {}
    # API Gateway HTTP API lowercases all header names.
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return False
    provided = auth[len(prefix):].strip()
    return hmac.compare_digest(provided, expected)


def _local_today() -> date:
    now_utc = datetime.now(timezone.utc)
    local = now_utc + timedelta(hours=_LOCAL_TZ_OFFSET_HOURS)
    return local.date()


def _extract_method_path(event: dict) -> tuple[str, str]:
    method = (
        (event.get("requestContext") or {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()
    path = event.get("rawPath") or event.get("path") or ""
    return method, path


def handler(event: dict, _context: Any = None) -> dict[str, Any]:
    method, path = _extract_method_path(event)

    # CORS preflight short-circuits before auth so browsers never see a 401
    # on OPTIONS (which they treat as a hard CORS failure).
    if method == "OPTIONS":
        return _response(204, "")

    if not _verify_bearer(event):
        return _response(401, {"error": "unauthorized"})

    if path == "/widget/summary" and method == "GET":
        try:
            payload = aggregator.build_summary(_local_today())
        except Exception:  # pylint: disable=broad-except
            # Any failure inside aggregation -- DynamoDB throttling,
            # malformed item, etc. -- is surfaced as a generic 500 to
            # keep the widget JSON contract narrow and avoid leaking
            # internal details.
            logger.exception("Failed to build widget summary")
            return _response(500, {"error": "internal_error"})
        return _response(200, payload)

    return _response(404, {"error": "not_found"})
