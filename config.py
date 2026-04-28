"""Runtime configuration, loaded from environment variables.

Non-secret values come from Lambda env vars (set via template.yaml).
Secrets (Telegram bot token, webhook secret) are fetched from SSM Parameter
Store at cold start — CloudFormation cannot resolve SecureString parameters
as stack parameters, so they must be read at runtime.

For local development, set the same names as env vars to skip the SSM lookup.
"""

from __future__ import annotations

import os
from functools import lru_cache


# AWS data plane
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", AWS_REGION)
DYNAMODB_TABLE = os.environ.get("TABLE_NAME", "finance-agent")
S3_BUCKET = os.environ.get("BUCKET_NAME", "")

# Bedrock model IDs
EXTRACTOR_MODEL_ID = os.environ.get(
    "EXTRACTOR_MODEL_ID",
    "us.anthropic.claude-sonnet-4-6",
)
CLASSIFIER_MODEL_ID = os.environ.get(
    "CLASSIFIER_MODEL_ID",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
RECONCILER_MODEL_ID = os.environ.get(
    "RECONCILER_MODEL_ID",
    CLASSIFIER_MODEL_ID,
)

# Reconciliation
RECONCILIATION_DATE_TOLERANCE_DAYS = int(
    os.environ.get("RECONCILIATION_DATE_TOLERANCE_DAYS", "5")
)

# Bot state TTL in DynamoDB (seconds)
USER_STATE_TTL_SECONDS = int(os.environ.get("USER_STATE_TTL_SECONDS", "3600"))

# Access control
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

# Periodic insights Lambda (IANA zone for wall-clock "today")
INSIGHTS_TIMEZONE = os.environ.get("INSIGHTS_TIMEZONE", "America/Lima")

# Widget API (forward-compat: also serves the future web dashboard)
WIDGET_ALLOWED_ORIGINS = os.environ.get("WIDGET_ALLOWED_ORIGINS", "*")


# ---------------------------------------------------------------------------
# Secrets: env-var first (local dev), SSM fallback (Lambda runtime)
# ---------------------------------------------------------------------------

_SECRET_SSM_PARAMS: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "/pfa/telegram-bot-token",
    "TELEGRAM_WEBHOOK_SECRET": "/pfa/telegram-webhook-secret",
    "WIDGET_BEARER_TOKEN": "/pfa/widget-bearer-token",
}


@lru_cache(maxsize=None)
def _get_secret(env_var: str) -> str:
    """Return a secret from env var, or fetch from SSM if env var is unset.

    Fails soft and returns an empty string when both sources come up empty
    (env var unset + SSM denied / not found). The bot and widget Lambdas
    share this module but have disjoint IAM scopes -- the bot can read the
    Telegram parameters but not the widget token, and the widget Lambda is
    the inverse. Each caller is expected to verify a non-empty value before
    relying on it.
    """
    direct = os.environ.get(env_var)
    if direct:
        return direct
    param_name = _SECRET_SSM_PARAMS.get(env_var)
    if not param_name:
        return ""
    # Lazy import so tests / dashboard that only use non-secret config
    # don't pay the boto3 import cost.
    try:
        import boto3

        ssm = boto3.client("ssm", region_name=AWS_REGION)
        resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
        return resp["Parameter"]["Value"]
    except Exception as exc:
        # Any failure here is non-fatal at import time: cross-function IAM
        # scoping (AccessDenied), missing parameter, no credentials in unit
        # tests, network issues, etc. The caller is expected to verify a
        # non-empty value before relying on it.
        import logging

        logging.getLogger(__name__).warning(
            "Could not load secret %s from SSM (%s): %s",
            env_var,
            param_name,
            exc.__class__.__name__,
        )
        return ""


TELEGRAM_BOT_TOKEN = _get_secret("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = _get_secret("TELEGRAM_WEBHOOK_SECRET")
WIDGET_BEARER_TOKEN = _get_secret("WIDGET_BEARER_TOKEN")
