"""Thin S3 helpers for receipt images."""

from __future__ import annotations

import logging

import boto3
from botocore.exceptions import ClientError

import config


logger = logging.getLogger(__name__)


_s3 = boto3.client("s3", region_name=config.AWS_REGION)


def _tmp_key(user_id: int) -> str:
    return f"receipts/tmp/{int(user_id)}.jpg"


def upload_tmp_image(user_id: int, image_bytes: bytes) -> str:
    """Upload the pre-confirmation image under a user-scoped tmp key."""
    key = _tmp_key(user_id)
    _s3.put_object(
        Bucket=config.S3_BUCKET,
        Key=key,
        Body=image_bytes,
        ContentType="image/jpeg",
    )
    return key


def finalize_image(tmp_key: str, txn_id: int, txn_date_iso: str) -> str:
    """Copy the tmp object to its final key and delete the tmp object.

    The final key is partitioned by year/month so S3 prefix listings stay fast.
    """
    year, month = txn_date_iso.split("-")[:2]
    final_key = f"receipts/{year}/{month}/txn_{int(txn_id)}.jpg"
    _s3.copy_object(
        Bucket=config.S3_BUCKET,
        CopySource={"Bucket": config.S3_BUCKET, "Key": tmp_key},
        Key=final_key,
    )
    _s3.delete_object(Bucket=config.S3_BUCKET, Key=tmp_key)
    return final_key


def delete_tmp_image(tmp_key: str) -> None:
    try:
        _s3.delete_object(Bucket=config.S3_BUCKET, Key=tmp_key)
    except ClientError:
        logger.exception("Failed to delete tmp image %s", tmp_key)


def presigned_url(key: str, ttl: int = 3600) -> str:
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.S3_BUCKET, "Key": key},
        ExpiresIn=ttl,
    )
