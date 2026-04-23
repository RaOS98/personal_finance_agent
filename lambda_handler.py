"""AWS Lambda entry point for the Telegram webhook.

API Gateway (HTTP API) delivers Telegram updates as POST requests. This
handler deserializes the update, hands it to python-telegram-bot's
``Application.process_update``, and returns 200 so Telegram doesn't retry.

The Application and asyncio event loop are built once on cold start and
reused across warm invocations in the same container.
"""

from __future__ import annotations

import asyncio
import json
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import config
from bot.handlers import (
    start_command,
    handle_message,
    handle_callback,
    handle_document,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


_application: Application | None = None
_initialized: bool = False
_loop: asyncio.AbstractEventLoop | None = None


def _build_application() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .updater(None)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(
        MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), handle_message)
    )
    app.add_handler(CallbackQueryHandler(handle_callback))
    return app


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


async def _ensure_application() -> Application:
    global _application, _initialized
    if _application is None:
        _application = _build_application()
    if not _initialized:
        await _application.initialize()
        _initialized = True
    return _application


async def _process_update(body: str) -> None:
    app = await _ensure_application()
    payload = json.loads(body)
    update = Update.de_json(payload, app.bot)
    if update is None:
        logger.warning("Received non-Update payload, skipping")
        return
    await app.process_update(update)


def _verify_secret(event: dict) -> bool:
    if not config.TELEGRAM_WEBHOOK_SECRET:
        return True
    headers = event.get("headers") or {}
    # API Gateway lowercases headers for HTTP API.
    provided = (
        headers.get("x-telegram-bot-api-secret-token")
        or headers.get("X-Telegram-Bot-Api-Secret-Token")
    )
    return provided == config.TELEGRAM_WEBHOOK_SECRET


def handler(event, context):
    if not _verify_secret(event):
        logger.warning("Webhook secret mismatch")
        return {"statusCode": 401, "body": "Unauthorized"}

    body = event.get("body") or "{}"

    try:
        loop = _get_loop()
        loop.run_until_complete(_process_update(body))
    except Exception:
        # Log and swallow — returning non-200 would trigger Telegram retries.
        logger.exception("Unhandled error processing update")

    return {"statusCode": 200, "body": "OK"}
