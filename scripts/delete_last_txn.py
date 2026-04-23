"""Delete the most recently created transaction (DynamoDB row + S3 receipt image).

Intended for cleaning up test entries while iterating on the bot. Prompts for
confirmation before deleting anything.

Notes:
    * The ``COUNTER`` item is NOT decremented — future transaction IDs will
      skip the deleted one. That's intentional: rolling a counter back can
      cause collisions if anything else has already read the next value.
    * If the transaction has been reconciled, any associated ``MATCH`` rows
      are left in place. Test entries won't have any.

Usage (PowerShell):
    $env:AWS_REGION   = "us-east-1"
    $env:TABLE_NAME   = "finance-agent"
    $env:BUCKET_NAME  = "pfa-receipts-<your-account-id>"
    python -m scripts.delete_last_txn
"""

from __future__ import annotations

import os
import sys

import boto3
from boto3.dynamodb.conditions import Key


def main() -> int:
    region = os.environ.get("AWS_REGION", "us-east-1")
    table_name = os.environ.get("TABLE_NAME", "finance-agent")
    bucket = os.environ.get("BUCKET_NAME")
    if not bucket:
        print("BUCKET_NAME env var is required.", file=sys.stderr)
        return 2

    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    s3 = boto3.client("s3", region_name=region)

    # SK is "{date}#{txn_id:08d}" so descending order gives the newest first.
    resp = table.query(
        KeyConditionExpression=Key("PK").eq("TXN"),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        print("No TXN items found in the table.")
        return 0

    txn = items[0]
    print("About to delete this transaction:")
    print(f"  id:       {txn.get('id')}")
    print(f"  date:     {txn.get('date')}")
    print(f"  merchant: {txn.get('merchant') or '(none)'}")
    print(f"  amount:   {txn.get('amount')} {txn.get('currency')}")
    print(f"  category: {txn.get('category_name')}")
    print(f"  payment:  {txn.get('payment_method_name')}")
    print(f"  image:    {txn.get('image_path') or '(none)'}")
    print(f"  PK / SK:  {txn['PK']}  /  {txn['SK']}")

    if input("\nDelete? [y/N]: ").strip().lower() != "y":
        print("Aborted.")
        return 0

    image_path = txn.get("image_path")
    if image_path:
        s3.delete_object(Bucket=bucket, Key=image_path)
        print(f"  [ok] deleted S3 object  s3://{bucket}/{image_path}")

    table.delete_item(Key={"PK": txn["PK"], "SK": txn["SK"]})
    print(f"  [ok] deleted DynamoDB row PK={txn['PK']} SK={txn['SK']}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
