# e14 vote infrastructure (CDK)

Durable vote-ingestion state on AWS. The web app stays on Fly (it serves reads from
the warm SQLite snapshot); this stack provisions SQS (+ DLQ), Aurora Serverless v2
Postgres (reached via the RDS Data API), and the **vote-drain Lambda** that replaces
the always-on Fly `worker` process. See `../plans/pending/aws-cdk-vote-infra.md` for
the full plan and rationale.

## Prerequisites

- AWS CLI authenticated as the **e14-admin** IAM user (default profile).
  Verify: `aws sts get-caller-identity` shows `…:user/e14-admin`.
- Node + CDK CLI (`cdk --version`), Python 3.12.
- Region: **us-east-1** (near the Fly DFW region).

## Setup

```bash
cd infra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Deploy

```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1

cdk bootstrap          # idempotent; first time only per account/region
cdk synth              # render CloudFormation, no AWS changes
cdk deploy             # provision
```

### Stack outputs

`cdk deploy` prints: `SqsQueueUrl`, `SqsDlqUrl`, `AuroraClusterArn`,
`AuroraSecretArn`, `AwsRegion`, `FlyUserName`. These become Fly secrets.

## Fly access key (minted out-of-band)

The CDK stack creates the `e14-vote-fly` IAM user and its permissions but **not**
an access key — minting it outside CloudFormation keeps the secret out of CFN
state. After deploy:

```bash
aws iam create-access-key --user-name e14-vote-fly
```

Feed the result straight into Fly secrets (never commit it):

```bash
fly secrets set \
  AWS_ACCESS_KEY_ID=<AccessKeyId> \
  AWS_SECRET_ACCESS_KEY=<SecretAccessKey> \
  AWS_REGION=us-east-1 \
  SQS_QUEUE_URL=<SqsQueueUrl> \
  AURORA_CLUSTER_ARN=<AuroraClusterArn> \
  AURORA_SECRET_ARN=<AuroraSecretArn>
```

## Vote drain (Lambda) — replaces the Fly worker

The `e14-vote-drain` Lambda (`lambda/handler.py`) is fed by an SQS event source
mapping with partial-batch responses; it bulk-inserts votes into Aurora over the
Data API. It keeps the worker's resilience guarantees (a vote is "done" only after
its insert commits; a DB error redelivers the whole batch; malformed messages go to
the DLQ via redrive) — see `tests/test_vote_lambda.py`. The execution role carries
the same `rds-data` + secret grants natively, so the Lambda needs **no**
`E14_VOTE_AWS_*` keys (those only existed to keep Fly's boto3 off the Tigris keys).

Idle cost ≈ $0 (invoked only when the queue has messages), vs. a 24/7 Fly machine.

### Cutover (do this once, after `cdk deploy` provisions the Lambda)

1. **Deploy** the Lambda + mapping: `cdk deploy` (the stack now includes both).
2. **Verify** it drains: cast a vote on the live site (or send a test SQS message),
   then confirm the row landed and the Lambda ran:
   ```bash
   aws logs tail /aws/lambda/e14-vote-drain --since 5m
   aws rds-data execute-statement --resource-arn <AuroraClusterArn> \
     --secret-arn <AuroraSecretArn> --database e14 \
     --sql 'select count(*) from flags'
   ```
3. **Retire the Fly worker** once the Lambda is confirmed draining: drop the
   `worker` process (and its `[[vm]]` block) from `fly.toml`, then `fly deploy`.
   The web process is unchanged — it still *publishes* to SQS.

Until step 3 both consumers drain the same queue; that is safe (SQS delivers each
message to one consumer, and the inserts are idempotent), but you want exactly one
in steady state so backlog/age metrics stay meaningful.

## Verify

```bash
aws sqs get-queue-attributes --queue-url <SqsQueueUrl> --attribute-names All
aws rds-data execute-statement \
  --resource-arn <AuroraClusterArn> --secret-arn <AuroraSecretArn> \
  --database e14 --sql 'select 1'
```

## Teardown

```bash
cdk destroy
```

Aurora uses `removalPolicy=SNAPSHOT`, so destroy leaves a final snapshot. Before
the election, flip `deletion_protection=True` in `e14_infra/vote_stack.py`.
