#!/usr/bin/env python3
"""CDK app entrypoint for the e14 vote-ingestion infrastructure.

Compute stays on Fly; this stack provisions only the durable AWS state:
SQS (+ DLQ) to absorb votes, and Aurora Serverless v2 Postgres (reached via
the RDS Data API) to replace community.sqlite. See plans/pending/aws-cdk-vote-infra.md.
"""
import os

import aws_cdk as cdk

from e14_infra.vote_stack import VoteStack

app = cdk.App()

# Region is pinned near the Fly DFW region. Account comes from the active
# credentials (the e14-admin IAM user) at synth/deploy time.
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

# R1 (first round) — the LIVE stack. Unchanged id/prefix/exports so `cdk deploy E14VoteStack`
# never replaces the running cluster.
VoteStack(app, "E14VoteStack", round="r1", env=env)

# R2 (runoff) — a SEPARATE, single-round stack: its own Aurora + SQS + drain Lambda under the
# "e14-vote-r2-" prefix, reusing infra/schema.sql unchanged (no election_round column). Synthesized
# always but deployed only on demand: `cdk deploy E14VoteStackR2`. Aurora Serverless v2 scale-to-
# zero keeps it cheap before the runoff; freeze/retire R1's stack after baking its verdicts.
VoteStack(app, "E14VoteStackR2", round="r2", env=env)

app.synth()
