#!/usr/bin/env python3
"""
Migrate ystocker-daily-summaries and ystocker-subscribers
from us-east-1 to us-west-2.

Usage:
    python migrate_dynamo_to_west2.py
    python migrate_dynamo_to_west2.py --dry-run   # scan only, no writes
"""
import argparse
import sys
import time
import boto3
from boto3.dynamodb.conditions import Attr

SRC_REGION = "us-east-1"
DST_REGION = "us-west-2"

TABLES = [
    {
        "name": "ystocker-daily-summaries",
        "attribute_definitions": [
            {"AttributeName": "date",        "AttributeType": "S"},
            {"AttributeName": "lang_market", "AttributeType": "S"},
        ],
        "key_schema": [
            {"AttributeName": "date",        "KeyType": "HASH"},
            {"AttributeName": "lang_market", "KeyType": "RANGE"},
        ],
    },
    {
        "name": "ystocker-subscribers",
        "attribute_definitions": [
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        "key_schema": [
            {"AttributeName": "email", "KeyType": "HASH"},
        ],
    },
]


def ensure_table(ddb, spec: dict) -> None:
    """Create the table in the destination region if it doesn't exist."""
    tbl_name = spec["name"]
    existing = [t.name for t in ddb.tables.all()]
    if tbl_name in existing:
        print(f"  [ok] table already exists: {tbl_name}")
        return

    print(f"  [+] creating table: {tbl_name}")
    tbl = ddb.create_table(
        TableName=tbl_name,
        AttributeDefinitions=spec["attribute_definitions"],
        KeySchema=spec["key_schema"],
        BillingMode="PAY_PER_REQUEST",
    )
    print(f"  ... waiting for {tbl_name} to become active")
    tbl.wait_until_exists()
    print(f"  [ok] table active: {tbl_name}")


def scan_all(tbl) -> list:
    """Scan every item from a table, handling pagination."""
    items = []
    kwargs = {}
    while True:
        resp = tbl.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def batch_write(tbl, items: list, dry_run: bool) -> None:
    """Write items in batches of 25 (DynamoDB limit)."""
    if dry_run:
        print(f"  [dry-run] would write {len(items)} items")
        return

    batch_size = 25
    for i in range(0, len(items), batch_size):
        chunk = items[i : i + batch_size]
        with tbl.batch_writer() as batch:
            for item in chunk:
                batch.put_item(Item=item)
        print(f"  ... wrote {min(i + batch_size, len(items))}/{len(items)} items")
        time.sleep(0.05)  # gentle throttle


def migrate(dry_run: bool) -> None:
    src_ddb = boto3.resource("dynamodb", region_name=SRC_REGION)
    dst_ddb = boto3.resource("dynamodb", region_name=DST_REGION)

    for spec in TABLES:
        name = spec["name"]
        print(f"\n{'='*60}")
        print(f"Table: {name}")
        print(f"{'='*60}")

        # 1. Create in destination
        if not dry_run:
            ensure_table(dst_ddb, spec)

        # 2. Scan source
        src_tbl = src_ddb.Table(name)
        try:
            src_tbl.load()
        except Exception as exc:
            print(f"  [!] cannot access source table ({SRC_REGION}): {exc}")
            continue

        print(f"  Scanning {name} in {SRC_REGION} ...")
        items = scan_all(src_tbl)
        print(f"  Found {len(items)} items in source")

        if not items:
            print("  Nothing to migrate.")
            continue

        # 3. Write to destination
        dst_tbl = dst_ddb.Table(name)
        print(f"  Writing to {name} in {DST_REGION} ...")
        batch_write(dst_tbl, items, dry_run)

        # 4. Verify
        if not dry_run:
            dst_tbl.reload()
            dst_count = dst_tbl.item_count  # approximate, good enough for sanity check
            print(f"  [ok] migration complete. dst item_count (approx): {dst_count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate DynamoDB tables to us-west-2")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, no writes")
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN — no tables will be created or written to\n")
    else:
        print(f"Migrating from {SRC_REGION} → {DST_REGION}\n")
        confirm = input("Continue? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(0)

    migrate(args.dry_run)
    print("\nDone.")


if __name__ == "__main__":
    main()
