"""Non-interactive auto-reconciliation loop.

Both the Telegram bot and the Streamlit dashboard need the same loop body:
walk pending statement lines, ask the reconciler to grade each candidate list,
auto-match confident singletons (with a date-diff tiebreaker for duplicates),
and route the rest to user review. Pulling this out of ``bot/handlers.py``
lets the dashboard reuse it without importing anything Telegram-specific.

The function is synchronous: Bedrock calls dominate the runtime, and neither
caller is reaching for concurrency yet.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable, Iterable

import config
from agent import reconciler
from db import dynamo as db


logger = logging.getLogger(__name__)


ProgressCallback = Callable[[int, int, dict[str, Any] | None], None]


def coerce_date(value: date | datetime | str) -> date:
    """Normalize the assorted date shapes that come out of DynamoDB and the
    statement parser into a plain ``datetime.date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"Unsupported date type: {type(value)!r}")


_coerce_date = coerce_date  # keep the private alias for internal callers


def _verdict(row: dict) -> str:
    return str(row.get("verdict") or "uncertain").strip().lower()


def pick_auto_match(line: dict, confident: list[dict]) -> dict | None:
    """Choose a single auto-confirmable match from the confident candidates.

    - 0 confident -> no auto-match
    - 1 confident -> auto-match that one
    - >1 confident -> tiebreak on smallest absolute date-diff to the line.
      Only auto-match if there's a strict winner; otherwise let the user
      decide.
    """
    if not confident:
        return None
    if len(confident) == 1:
        return confident[0]

    try:
        line_date = _coerce_date(line["date"])
    except (TypeError, ValueError, KeyError):
        return None

    def _diff(candidate: dict) -> int:
        try:
            return abs((_coerce_date(candidate["date"]) - line_date).days)
        except (TypeError, ValueError, KeyError):
            return 10**6

    ranked = sorted(confident, key=_diff)
    if _diff(ranked[0]) < _diff(ranked[1]):
        return ranked[0]
    return None


def _evaluate(line: dict, candidates: list[dict]) -> list[dict]:
    """Call the reconciler LLM for a single line; tolerate failures."""
    try:
        verdicts = reconciler.evaluate_matches(
            statement_line={
                "date": str(line["date"]),
                "description": line.get("description", ""),
                "amount": float(line["amount"]),
            },
            candidates=[
                {
                    "date": str(c["date"]),
                    "merchant": c.get("merchant", ""),
                    "amount": float(c["amount"]),
                    "category": c.get("category_name", ""),
                }
                for c in candidates
            ],
        )
    except Exception:
        logger.exception("Reconciler LLM call failed; marking all uncertain")
        return [
            {"verdict": "uncertain", "reason": "Evaluation error"}
            for _ in candidates
        ]
    return verdicts


def auto_reconcile(
    account_id: int,
    billing_period: str,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Run the auto-match pass over every pending line in a billing period.

    Args:
        account_id: Settlement account whose pending lines should be walked.
        billing_period: "YYYY-MM" billing period to scope the scan to.
        progress_callback: Optional ``fn(done, total, last_result)`` hook.
            ``last_result`` is ``None`` for the initial call, otherwise one of
            ``{"status": "auto_matched" | "needs_review" | "unmatched" |
            "skipped", "line": {...}, ...}``. The dashboard uses this to
            drive ``st.progress``.

    Returns:
        ``{"auto_matched": [{"line", "candidate"}, ...],
           "needs_review": [{"line", "candidates"}, ...],
           "unmatched":    [{"line"}, ...]}``.

        ``auto_matched`` lines have already been persisted via
        :func:`db.save_reconciliation_match`; callers only need to render them.
    """
    pending_lines: Iterable[dict] = db.get_pending_statement_lines(
        account_id, billing_period
    )
    pending_lines = list(pending_lines)
    total = len(pending_lines)

    auto_matched: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    if progress_callback is not None:
        progress_callback(0, total, None)

    for idx, line in enumerate(pending_lines):
        amount = float(line["amount"])
        if amount == 0:
            db.update_statement_line_status(line["id"], "skipped")
            if progress_callback is not None:
                progress_callback(
                    idx + 1,
                    total,
                    {"status": "skipped", "line": line},
                )
            continue

        try:
            line_date = _coerce_date(line["date"])
        except (TypeError, ValueError):
            logger.warning(
                "Skipping statement line %s: invalid date %r",
                line.get("id"),
                line.get("date"),
            )
            if progress_callback is not None:
                progress_callback(
                    idx + 1,
                    total,
                    {"status": "unmatched", "line": line},
                )
            unmatched.append({"line": line})
            continue

        candidates = db.find_reconciliation_candidates(
            account_id=account_id,
            amount=amount,
            date_val=line_date,
            tolerance_days=config.RECONCILIATION_DATE_TOLERANCE_DAYS,
        )

        if not candidates:
            unmatched.append({"line": line})
            if progress_callback is not None:
                progress_callback(
                    idx + 1,
                    total,
                    {"status": "unmatched", "line": line},
                )
            continue

        verdicts = _evaluate(line, candidates)
        for cand, verdict_obj in zip(candidates, verdicts):
            cand["verdict"] = _verdict(verdict_obj)
            cand["reason"] = verdict_obj.get("reason", "")

        confident = [c for c in candidates if _verdict(c) == "confident"]
        auto_pick = pick_auto_match(line, confident)

        if auto_pick is not None:
            db.save_reconciliation_match(
                statement_line_id=line["id"],
                transaction_id=int(auto_pick["id"]),
                verdict="confident",
                confirmed_by="auto",
            )
            auto_matched.append({"line": line, "candidate": auto_pick})
            if progress_callback is not None:
                progress_callback(
                    idx + 1,
                    total,
                    {
                        "status": "auto_matched",
                        "line": line,
                        "candidate": auto_pick,
                    },
                )
        else:
            needs_review.append({"line": line, "candidates": candidates})
            if progress_callback is not None:
                progress_callback(
                    idx + 1,
                    total,
                    {
                        "status": "needs_review",
                        "line": line,
                        "candidates": candidates,
                    },
                )

    return {
        "auto_matched": auto_matched,
        "needs_review": needs_review,
        "unmatched": unmatched,
    }
