# Teardown runbook — full shutdown to ~$0 (with backup first)

Elections are over. This tears down **everything** and saves a portable backup first.
Run it from a machine that has your real `e14-admin` AWS credentials and `flyctl` login
(the repo sandbox has neither). Do the steps **in order** — back up and verify *before* you
destroy anything.

Cost being eliminated (approx/month): **Aurora Serverless v2 ~$44** (the big one) ·
Fly `e14-poll` ~$5–15 · Tigris crops a few $ · Fly `e14-r1-archive` ~$1–3 · SQS/Lambda ~$0.

Region `us-east-1` · AWS account `388768425935` · live stack `E14VoteStack` (round r1).

---

## 0. Prereqs (once)

```bash
pip install boto3
npm i -g aws-cdk            # or use `npx aws-cdk` below
export AWS_PROFILE=e14-admin            # however you auth the e14-admin IAM user
export AWS_REGION=us-east-1
aws sts get-caller-identity             # expect Account 388768425935
flyctl auth login
```

## 1. Find the Aurora ARNs (stack outputs)

```bash
aws cloudformation describe-stacks --stack-name E14VoteStack \
  --query "Stacks[0].Outputs" --output table
# copy AuroraClusterArn / AuroraSecretArn into env:
export AURORA_CLUSTER_ARN=arn:aws:rds:us-east-1:388768425935:cluster:...
export AURORA_SECRET_ARN=arn:aws:secretsmanager:us-east-1:388768425935:secret:...
```

## 2. BACK UP (do not skip — this is election evidence)

**2a. Restorable cluster snapshot** (also auto-created by `cdk destroy`, but take one now too):
```bash
CID=$(aws rds describe-db-clusters \
  --query "DBClusters[?contains(DBClusterIdentifier,'e14')].DBClusterIdentifier | [0]" --output text)
aws rds create-db-cluster-snapshot --db-cluster-identifier "$CID" \
  --db-cluster-snapshot-identifier "e14-final-$(date +%Y%m%d)"
```

**2b. Portable JSON export** of every table (flags, appeals, field_state, rate_buckets, cid_index):
```bash
python3 infra/export_aurora.py        # writes backup/*.jsonl + backup/_manifest.json
```

**2c. Results DB** (rowid/index snapshot — already mirrored to Tigris under `db/`, grab a copy):
```bash
mkdir -p backup
flyctl ssh console -a e14-poll -C "cat /data/results.sqlite" > backup/results.sqlite
```

**2d. Crops (the evidence images, in Tigris).** These can be large (~1.5M files). Two choices:
- **Keep the bucket** (cheapest way to preserve evidence — a few $/mo) and skip the rest of 2d, **or**
- **Download then delete** using your Tigris keys (separate from AWS):
```bash
aws s3 sync s3://e14-crops ./backup/crops \
  --endpoint-url https://fly.storage.tigris.dev --profile tigris
```

**➡ Verify before continuing:** check `backup/_manifest.json` row counts look right,
`backup/results.sqlite` is non-empty, and the snapshot shows `available`:
```bash
aws rds describe-db-cluster-snapshots \
  --db-cluster-snapshot-identifier "e14-final-$(date +%Y%m%d)" \
  --query "DBClusterSnapshots[0].Status"
```

## 3. Destroy the AWS stack (kills the ~$44/mo Aurora + SQS + Lambda + VPC)

```bash
cd infra
npx aws-cdk list                       # expect E14VoteStack (and maybe E14VoteStackR2)
npx aws-cdk destroy E14VoteStack       # answer 'y'; Aurora leaves a final SNAPSHOT automatically
# if R2 was ever deployed, also: npx aws-cdk destroy E14VoteStackR2
cd ..
# confirm the cluster is gone:
aws rds describe-db-clusters --query "DBClusters[?contains(DBClusterIdentifier,'e14')].DBClusterIdentifier"
```
> If `cdk destroy` can't run, delete the CloudFormation stack instead:
> `aws cloudformation delete-stack --stack-name E14VoteStack`

## 4. Destroy the Fly apps + storage

```bash
flyctl apps destroy e14-poll           # removes machines, the /data volume, and IPs
flyctl apps destroy e14-r1-archive
flyctl storage destroy e14-crops       # the Tigris crops bucket — ONLY after 2d is settled
flyctl apps list                       # expect e14-* gone
```

## 5. Verify ~$0 and clean up the edges

- **Fly:** dashboard → Billing shows no active machines/volumes.
- **AWS:** `aws rds describe-db-clusters` empty; Cost Explorer flattens within a day. The final
  snapshot costs a few cents/mo — keep it as your safety net, or delete once you've confirmed the
  JSON export is good:
  `aws rds delete-db-cluster-snapshot --db-cluster-snapshot-identifier e14-final-YYYYMMDD`
- **Secrets Manager:** the Aurora secret is scheduled for deletion by cdk (recovery window) — nothing to pay.
- **GitHub:** the `Deploy to Fly` workflow is manual-only (no cost); optionally delete the
  `FLY_API_TOKEN` repo secret.
- **Domain** `veeduria-ciudadana-elecciones-colombia-2026.com`: turn off auto-renew at your
  registrar (annual, separate bill — do this whenever).

## Rollback (if you ever need it back)

Restore the cluster from the snapshot (`aws rds restore-db-cluster-from-snapshot`), re-`cdk deploy`,
re-set the Fly secrets, redeploy `e14-poll`. The JSON export in `backup/` reloads with a small
`INSERT` script against `infra/schema.sql`.
