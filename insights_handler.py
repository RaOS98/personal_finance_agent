"""Scheduled Lambda: weekly spending digest to Telegram (no Bedrock)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime

from zoneinfo import ZoneInfo

import config
from api import aggregator, insights
from db import dynamo as db


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _send_telegram(chat_id: int, text: str) -> None:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    data = json.loads(raw)
    if not data.get("ok"):
        raise RuntimeError(str(data))


def handler(_event: object, _context: object) -> dict:
    uid = config.ALLOWED_USER_ID
    if not uid:
        logger.warning("ALLOWED_USER_ID not set; skipping")
        return {"ok": False, "error": "ALLOWED_USER_ID not set"}

    tz = ZoneInfo(config.INSIGHTS_TIMEZONE)
    today = datetime.now(tz).date()
    iso_cal = today.isocalendar()
    digest_key = f"{iso_cal.year}-W{iso_cal.week:02d}"

    if db.load_last_insight_digest_key(uid) == digest_key:
        logger.info("Digest %s already sent", digest_key)
        return {"ok": True, "skipped": "already_sent", "digest_key": digest_key}

    summary = aggregator.build_summary(today)
    week_pen = insights.rolling_week_pen_by_category(today, 7)
    msg = insights.format_insights_message(today, summary, week_pen)
    if not msg:
        logger.info("No transactions this month; not sending")
        return {"ok": True, "skipped": "no_txns"}

    try:
        _send_telegram(uid, msg)
    except (urllib.error.URLError, OSError, RuntimeError, ValueError):
        logger.exception("Telegram sendMessage failed")
        raise

    db.save_last_insight_digest_key(uid, digest_key)
    logger.info("Sent digest %s", digest_key)
    return {"ok": True, "sent": digest_key}
