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

VoteStack(app, "E14VoteStack", env=env)

app.synth()
