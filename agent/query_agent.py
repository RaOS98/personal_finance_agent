"""Natural-language query agent over logged transactions.

Tool-use loop over Amazon Bedrock (Claude Haiku 4.5) with four primitives
that wrap DynamoDB reads. The agent decides which tools to call, executes
them, and returns a JSON envelope ``{answer, source_txn_ids}`` so the
handler can render a citation trailer without a second LLM call.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config as BotoConfig

import config
from db import dynamo as db


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a personal-finance analyst answering questions about the user's logged transactions. Use the tools to fetch data from the database — do not invent numbers.

Available tools:
- list_recent_transactions(limit): fetch the N most recent transactions.
- query_transactions(date_from, date_to, category_slug?, payment_method_alias?): fetch transactions in a date range, optionally filtered by category or payment method. Dates are ISO YYYY-MM-DD.
- aggregate_by_category(date_from, date_to): group transactions in a date range by category and sum amounts. Returns per-currency totals.
- get_today(): returns today's ISO date. Use this to resolve phrases like "this month", "last week", "yesterday".

Guidance:
- Call get_today first when the user's question involves a relative time ("this month", "today", "last week").
- Dates must be ISO YYYY-MM-DD.
- Assume currency PEN unless the user explicitly says USD or dollars.
- If a tool returns {"truncated": true}, the full result exceeded the 50-row cap — communicate the limitation in your answer.
- Keep your final answer concise (1–3 sentences). Use actual figures from tool output.
- List the transaction ids your answer is based on in source_txn_ids so the user can verify.

Your final response (after all tool calls) MUST be ONLY a JSON object with exactly these keys: answer (string), source_txn_ids (list of integers). No other text."""


_bedrock = boto3.client(
    "bedrock-runtime",
    region_name=config.BEDROCK_REGION,
    config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
)


_ROW_CAP = 50


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "toolSpec": {
            "name": "list_recent_transactions",
            "description": "Fetch the N most recent transactions (newest first). Capped at 50.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "How many recent transactions to return (1–50).",
                        },
                    },
                    "required": ["limit"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "query_transactions",
            "description": "Fetch transactions in an inclusive date range, optionally filtered by category_slug or payment_method_alias. Capped at 50 rows.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string", "description": "ISO YYYY-MM-DD."},
                        "date_to": {"type": "string", "description": "ISO YYYY-MM-DD."},
                        "category_slug": {
                            "type": "string",
                            "description": "Optional category slug (e.g. food_dining).",
                        },
                        "payment_method_alias": {
                            "type": "string",
                            "description": "Optional payment method alias (e.g. yape, amex).",
                        },
                    },
                    "required": ["date_from", "date_to"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "aggregate_by_category",
            "description": "Group transactions in a date range by category_slug and sum amounts per currency.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string", "description": "ISO YYYY-MM-DD."},
                        "date_to": {"type": "string", "description": "ISO YYYY-MM-DD."},
                    },
                    "required": ["date_from", "date_to"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_today",
            "description": "Return today's date as ISO YYYY-MM-DD. Use to resolve relative time phrases.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
]


def _query_transactions(
    date_from: str,
    date_to: str,
    category_slug: str | None = None,
    payment_method_alias: str | None = None,
) -> dict[str, Any]:
    # SK is "{date_iso}#{txn_id:08d}" — BETWEEN on the end needs a high-sort suffix.
    sk_lo = date_from
    sk_hi = f"{date_to}#zzzzzzzz"
    resp = db._table.query(
        KeyConditionExpression=Key("PK").eq("TXN") & Key("SK").between(sk_lo, sk_hi),
    )
    items = [db._normalize_item(i) for i in resp.get("Items", [])]

    if payment_method_alias:
        pm = db.resolve_payment_method(payment_method_alias)
        if pm is None:
            return {"transactions": [], "truncated": False, "note": f"Unknown payment method: {payment_method_alias}"}
        pm_id = int(pm["id"])
        items = [i for i in items if i.get("payment_method_id") == pm_id]

    if category_slug:
        items = [i for i in items if i.get("category_slug") == category_slug]

    total = len(items)
    truncated = total > _ROW_CAP
    items = items[:_ROW_CAP]
    return {"transactions": items, "truncated": truncated, "total_matched": total}


def _aggregate_by_category(date_from: str, date_to: str) -> dict[str, Any]:
    sk_lo = date_from
    sk_hi = f"{date_to}#zzzzzzzz"
    resp = db._table.query(
        KeyConditionExpression=Key("PK").eq("TXN") & Key("SK").between(sk_lo, sk_hi),
    )
    items = [db._normalize_item(i) for i in resp.get("Items", [])]

    totals: dict[str, dict[str, float]] = {}
    for i in items:
        slug = i.get("category_slug") or "unknown"
        currency = i.get("currency") or "UNKNOWN"
        amount = float(i.get("amount") or 0)
        totals.setdefault(slug, {}).setdefault(currency, 0.0)
        totals[slug][currency] += amount

    return {"totals_by_category": totals, "transaction_count": len(items)}


def _execute_tool(tool_use: dict[str, Any]) -> dict[str, Any]:
    name = tool_use.get("name")
    tool_input = tool_use.get("input") or {}
    tool_use_id = tool_use.get("toolUseId")

    try:
        if name == "list_recent_transactions":
            limit = max(1, min(_ROW_CAP, int(tool_input.get("limit", 10))))
            txns = db.list_recent_transactions(limit=limit)
            content = {"transactions": txns}
        elif name == "query_transactions":
            content = _query_transactions(
                date_from=tool_input["date_from"],
                date_to=tool_input["date_to"],
                category_slug=tool_input.get("category_slug"),
                payment_method_alias=tool_input.get("payment_method_alias"),
            )
        elif name == "aggregate_by_category":
            content = _aggregate_by_category(
                date_from=tool_input["date_from"],
                date_to=tool_input["date_to"],
            )
        elif name == "get_today":
            content = {"today": date.today().isoformat()}
        else:
            return {
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "content": [{"text": f"Unknown tool: {name}"}],
                    "status": "error",
                }
            }
    except Exception as e:
        logger.exception("Tool %s failed", name)
        return {
            "toolResult": {
                "toolUseId": tool_use_id,
                "content": [{"text": f"Tool error: {e}"}],
                "status": "error",
            }
        }

    return {
        "toolResult": {
            "toolUseId": tool_use_id,
            "content": [{"json": content}],
            "status": "success",
        }
    }


def _parse_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


def _extract_final_text(assistant_msg: dict) -> str:
    for block in assistant_msg.get("content", []):
        if "text" in block:
            return block["text"]
    return ""


def answer_query(text: str) -> dict:
    """Run the tool-use loop and return the final answer envelope.

    Returns a dict with keys: answer (str or None), source_txn_ids (list[int]).
    On failure, returns those with an additional "error" key.
    """
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"text": text}]}
    ]

    system = [
        {"text": SYSTEM_PROMPT},
        {"cachePoint": {"type": "default"}},
    ]

    try:
        for _ in range(4):
            response = _bedrock.converse(
                modelId=config.CLASSIFIER_MODEL_ID,
                system=system,
                messages=messages,
                toolConfig={"tools": TOOL_SPECS},
                inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
            )
            stop_reason = response.get("stopReason")
            assistant_msg = response["output"]["message"]
            messages.append(assistant_msg)

            if stop_reason == "tool_use":
                tool_uses = [
                    b["toolUse"] for b in assistant_msg.get("content", [])
                    if "toolUse" in b
                ]
                if not tool_uses:
                    break
                tool_results = [_execute_tool(tu) for tu in tool_uses]
                messages.append({"role": "user", "content": tool_results})
                continue

            final_text = _extract_final_text(assistant_msg)
            try:
                parsed = _parse_json(final_text)
            except json.JSONDecodeError:
                return {
                    "answer": final_text or None,
                    "source_txn_ids": [],
                    "error": "Final response was not JSON",
                }
            return {
                "answer": parsed.get("answer"),
                "source_txn_ids": [int(i) for i in parsed.get("source_txn_ids", []) if isinstance(i, (int, float, str)) and str(i).lstrip("-").isdigit()],
            }

        return {
            "answer": None,
            "source_txn_ids": [],
            "error": "Tool-use loop exceeded 4 iterations",
        }
    except Exception as e:
        logger.exception("query_agent Bedrock call failed")
        return {
            "answer": None,
            "source_txn_ids": [],
            "error": f"Bedrock request failed: {e}",
        }
