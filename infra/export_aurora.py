#!/usr/bin/env python3
"""Portable backup of the e14 vote Aurora DB via the RDS Data API.

The cluster lives in ISOLATED subnets and is reachable ONLY through the RDS Data API, so
``pg_dump``/``psql`` cannot connect to it directly — this is the portable export. It dumps
every table in the ``public`` schema to ``backup/<table>.jsonl`` (one JSON object per row),
using the Data API's native JSON record formatting and paging so arbitrarily large tables
stay under the Data API's ~1 MiB per-call result cap.

Pair it with a real cluster snapshot for a restorable copy (``cdk destroy`` also auto-creates
a final snapshot because the cluster's RemovalPolicy is SNAPSHOT):

    aws rds create-db-cluster-snapshot \
        --db-cluster-identifier <id> \
        --db-cluster-snapshot-identifier e14-final-$(date +%Y%m%d) --region us-east-1

Env (the same values cdk/Fly use):
  AURORA_CLUSTER_ARN   required — from `cdk` outputs / CloudFormation stack E14VoteStack
  AURORA_SECRET_ARN    required — same source
  AURORA_DATABASE      default 'e14'
  AWS_REGION           default 'us-east-1'
  EXPORT_DIR           default 'backup'
  EXPORT_PAGE_SIZE     default 200  (lower it if a page trips the Data API's 1 MiB cap)

Usage:
  pip install boto3
  export AWS_PROFILE=e14-admin           # or however you auth the e14-admin IAM user
  export AURORA_CLUSTER_ARN=... AURORA_SECRET_ARN=...
  python3 infra/export_aurora.py
"""
import json
import os
import pathlib
import sys

import boto3

REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
CLUSTER = os.environ.get("AURORA_CLUSTER_ARN")
SECRET = os.environ.get("AURORA_SECRET_ARN")
DATABASE = os.environ.get("AURORA_DATABASE", "e14")
PAGE = int(os.environ.get("EXPORT_PAGE_SIZE", "200"))
OUT = pathlib.Path(os.environ.get("EXPORT_DIR", "backup"))


def main() -> None:
    if not CLUSTER or not SECRET:
        sys.exit(
            "Missing AURORA_CLUSTER_ARN / AURORA_SECRET_ARN. Read them from the stack outputs:\n"
            "  aws cloudformation describe-stacks --stack-name E14VoteStack --region us-east-1 "
            '--query "Stacks[0].Outputs" --output table'
        )
    rds = boto3.client("rds-data", region_name=REGION)

    def rows(sql: str) -> list[dict]:
        res = rds.execute_statement(
            resourceArn=CLUSTER, secretArn=SECRET, database=DATABASE,
            sql=sql, formatRecordsAs="JSON",
        )
        return json.loads(res.get("formattedRecords") or "[]")

    OUT.mkdir(parents=True, exist_ok=True)
    tables = [r["tablename"] for r in rows(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    print(f"Region {REGION} · database {DATABASE} · {len(tables)} tables: {', '.join(tables)}")

    manifest: dict[str, int] = {}
    for t in tables:
        n, off = 0, 0
        with (OUT / f"{t}.jsonl").open("w", encoding="utf-8") as fh:
            while True:
                batch = rows(f'SELECT * FROM "{t}" ORDER BY 1 LIMIT {PAGE} OFFSET {off}')
                for row in batch:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += len(batch)
                off += PAGE
                if len(batch) < PAGE:
                    break
        manifest[t] = n
        print(f"  {t:18s} {n:>10,} rows -> {OUT}/{t}.jsonl")

    (OUT / "_manifest.json").write_text(json.dumps(manifest, indent=2))
    total = sum(manifest.values())
    print(f"\nDone. {total:,} rows across {len(manifest)} tables written to {OUT}/")
    print("Verify the row counts above look right BEFORE you run any destroy step.")


if __name__ == "__main__":
    main()
