# e14 vote infrastructure (CDK)

Durable vote-ingestion state on AWS. Compute stays on Fly; this stack provisions
only SQS (+ DLQ) and Aurora Serverless v2 Postgres (reached via the RDS Data API).
See `../plans/pending/aws-cdk-vote-infra.md` for the full plan and rationale.

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
