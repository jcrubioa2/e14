#!/usr/bin/env python3
"""Apply infra/schema.sql to the Aurora cluster via the RDS Data API.

Idempotent: the DDL is all CREATE ... IF NOT EXISTS, so re-running is safe.

Cluster/secret ARNs are read (in order) from CLI args, env vars
(AURORA_CLUSTER_ARN / AURORA_SECRET_ARN), or the E14VoteStack CloudFormation
outputs. Region defaults to us-east-1; database to e14.

Usage:
    python apply_schema.py                 # auto-discover from the stack
    python apply_schema.py --database e14
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import boto3

STACK_NAME = "E14VoteStack"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _stack_outputs(region: str) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    stacks = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def _split_statements(sql: str) -> list[str]:
    """Strip SQL line comments and split into individual statements.

    schema.sql has no semicolons inside string literals, so a plain split on ';'
    after stripping ``-- ...`` comments is sufficient and keeps this dependency-free.
    """
    no_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in no_comments.split(";") if s.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--cluster-arn", default=os.environ.get("AURORA_CLUSTER_ARN"))
    ap.add_argument("--secret-arn", default=os.environ.get("AURORA_SECRET_ARN"))
    ap.add_argument("--database", default=os.environ.get("AURORA_DATABASE", "e14"))
    args = ap.parse_args()

    cluster_arn, secret_arn = args.cluster_arn, args.secret_arn
    if not (cluster_arn and secret_arn):
        outs = _stack_outputs(args.region)
        cluster_arn = cluster_arn or outs.get("AuroraClusterArn")
        secret_arn = secret_arn or outs.get("AuroraSecretArn")
    if not (cluster_arn and secret_arn):
        print("ERROR: could not resolve Aurora cluster/secret ARNs", file=sys.stderr)
        return 1

    statements = _split_statements(SCHEMA_PATH.read_text())
    data = boto3.client("rds-data", region_name=args.region)
    for i, stmt in enumerate(statements, 1):
        head = " ".join(stmt.split())[:70]
        print(f"[{i}/{len(statements)}] {head} ...")
        data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=args.database,
            sql=stmt,
        )
    print(f"OK: applied {len(statements)} statements to {args.database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
