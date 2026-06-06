"""E14VoteStack — durable vote-ingestion state on AWS.

Resources (prefix ``e14-vote-``):
  * VPC               — minimal 2-AZ VPC with isolated subnets (Aurora must live
                        in a VPC even when reached via the Data API; no NAT needed).
  * Aurora Serverless v2 PostgreSQL — replaces community.sqlite. Data API enabled,
                        master creds in Secrets Manager, default database ``e14``.
  * SQS standard queue + DLQ — absorbs votes so none are lost when web/worker/DB
                        is down. This is the bulletproofing.
  * IAM user (Fly)    — least-privilege programmatic access for the Fly compute.
                        The access key is minted out-of-band (see README) so its
                        secret never enters CloudFormation state.

Outputs feed the Fly secrets: SQS_QUEUE_URL, AURORA_CLUSTER_ARN, AURORA_SECRET_ARN,
AWS_REGION.
"""
import os

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_lambda_event_sources as les,
    aws_rds as rds,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_sqs as sqs,
)
from constructs import Construct

PREFIX = "e14-vote-"

# Self-contained SQS->Aurora drain handler (boto3-only; see infra/lambda/handler.py).
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")

# Aurora Serverless v2 capacity, in ACUs.
#   min 0.5 -> warm floor: the app is live and public, so scale-to-zero's ~15-50s cold
#             resume would hit real visitors (feed/reads) on every idle period and make
#             the worker spin on DatabaseResumingException. 0.5 ACU ~= $44/mo continuous;
#             drop to 0 only for genuinely quiet stretches with no public traffic.
#   max 8   -> headroom for the 50-500 votes/s spike. Requires a *paid* account plan
#             (free plan caps at 4 ACU and blocks the VPC+Data-API config entirely).
AURORA_MIN_ACU = 0.5
AURORA_MAX_ACU = 8

# SQS visibility timeout must exceed the worker's batch processing time.
QUEUE_VISIBILITY = Duration.seconds(60)
QUEUE_RETENTION = Duration.days(14)
# Receives before a message is moved to the DLQ. Every vote is irreplaceable, so this is
# the tolerance for a *transient* Aurora outage: at 60s visibility, 5 receives = only ~5min
# before a still-valid vote would DLQ. 60 buys ~hours of outage headroom; poison messages
# (which can never commit) just retry more, which is cheap and harmless.
DLQ_MAX_RECEIVE = 60

# Cap how many vote-drain Lambdas SQS runs concurrently. Unbounded fan-out under a 50-500/s
# spike would stampede the RDS Data API (it throttles) -> batch failures -> redelivery ->
# more concurrency, a feedback loop. A cap keeps the queue the smooth buffer it exists to be;
# backlog is recoverable, a throttle storm is not. Tune up from the staging load test.
DRAIN_MAX_CONCURRENCY = 20


class VoteStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, round: str = "r1", **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        # Per-round physical isolation: each round gets its OWN Aurora + SQS + drain Lambda, so a
        # round is single-round by construction (no election_round discriminator column needed) and
        # an R2 deploy/incident can never touch the R1 record. r1 keeps the LEGACY names/exports so
        # the live first-round stack stays byte-identical at the CloudFormation level (a prefix
        # change would REPLACE the running cluster); r2 nests under an "-r2-" prefix + "R2" export
        # suffix. Resource names must be globally unique, hence both the prefix and the export
        # suffix vary by round.
        self.round = (round or "r1").strip().lower()
        self.prefix = PREFIX if self.round == "r1" else f"{PREFIX}{self.round}-"
        self.export_suffix = "" if self.round == "r1" else self.round.upper()

        vpc = self._vpc()
        cluster, secret = self._aurora(vpc)
        queue, dlq = self._queues()
        self._fly_user(queue, cluster, secret)
        self._drain_lambda(queue, cluster, secret)
        self._alarms(queue, dlq, cluster)

        sfx = self.export_suffix
        CfnOutput(self, "SqsQueueUrl", value=queue.queue_url, export_name=f"E14SqsQueueUrl{sfx}")
        CfnOutput(self, "SqsDlqUrl", value=dlq.queue_url)
        CfnOutput(self, "AuroraClusterArn", value=cluster.cluster_arn, export_name=f"E14AuroraClusterArn{sfx}")
        CfnOutput(self, "AuroraSecretArn", value=secret.secret_arn, export_name=f"E14AuroraSecretArn{sfx}")
        CfnOutput(self, "AwsRegion", value=self.region)

    # --- VPC -----------------------------------------------------------------
    def _vpc(self) -> ec2.Vpc:
        # Isolated subnets only: the Data API is the access path, so no internet
        # egress (and no NAT gateway cost) is required.
        return ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"{self.prefix}vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                )
            ],
        )

    # --- Aurora --------------------------------------------------------------
    def _aurora(self, vpc: ec2.Vpc):
        # CDK generates the master secret (username + password) in Secrets Manager.
        cluster = rds.DatabaseCluster(
            self,
            "Aurora",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_4
            ),
            cluster_identifier=f"{self.prefix}cluster",
            credentials=rds.Credentials.from_generated_secret(
                "e14admin", secret_name=f"{self.prefix}db-credentials"
            ),
            default_database_name="e14",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            serverless_v2_min_capacity=AURORA_MIN_ACU,
            serverless_v2_max_capacity=AURORA_MAX_ACU,
            writer=rds.ClusterInstance.serverless_v2("writer"),
            enable_data_api=True,
            storage_encrypted=True,
            # Pre-cutover convenience. Flip deletion_protection=True and
            # removal_policy=SNAPSHOT before the election.
            deletion_protection=False,
            removal_policy=RemovalPolicy.SNAPSHOT,
        )
        return cluster, cluster.secret

    # --- SQS -----------------------------------------------------------------
    def _queues(self):
        dlq = sqs.Queue(
            self,
            "VoteDlq",
            queue_name=f"{self.prefix}events-dlq",
            retention_period=QUEUE_RETENTION,
        )
        queue = sqs.Queue(
            self,
            "VoteQueue",
            queue_name=f"{self.prefix}events",
            visibility_timeout=QUEUE_VISIBILITY,
            retention_period=QUEUE_RETENTION,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=DLQ_MAX_RECEIVE, queue=dlq),
        )
        return queue, dlq

    # --- IAM user for Fly ----------------------------------------------------
    def _fly_user(self, queue: sqs.Queue, cluster: rds.DatabaseCluster, secret) -> None:
        # No access key here on purpose: minting it out-of-band (see README)
        # keeps the secret out of CloudFormation state. CDK only grants the perms.
        user = iam.User(self, "FlyUser", user_name=f"{self.prefix}fly")

        user.add_to_policy(
            iam.PolicyStatement(
                sid="VoteQueueAccess",
                actions=[
                    "sqs:SendMessage",
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                ],
                resources=[queue.queue_arn],
            )
        )
        user.add_to_policy(
            iam.PolicyStatement(
                sid="AuroraDataApi",
                actions=["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"],
                resources=[cluster.cluster_arn],
            )
        )
        user.add_to_policy(
            iam.PolicyStatement(
                sid="DbSecretRead",
                actions=["secretsmanager:GetSecretValue"],
                resources=[secret.secret_arn],
            )
        )

        CfnOutput(self, "FlyUserName", value=user.user_name)

    # --- Lambda vote drain (replaces the Fly worker) -------------------------
    def _drain_lambda(self, queue: sqs.Queue, cluster: rds.DatabaseCluster, secret) -> None:
        """SQS -> Aurora drain as a Lambda, fed by an event source mapping.

        Replaces the always-on Fly ``worker`` process: the mapping only invokes the
        function when the queue has messages, so idle cost is ~$0 (vs. a 24/7 machine).
        Aurora is reached over the Data API, so the function needs no VPC config.
        """
        fn = _lambda.Function(
            self,
            "VoteDrain",
            function_name=f"{self.prefix}drain",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset(_LAMBDA_DIR),
            timeout=Duration.seconds(30),  # must stay < the queue's 60s visibility timeout
            memory_size=256,
            environment={
                "AURORA_CLUSTER_ARN": cluster.cluster_arn,
                "AURORA_SECRET_ARN": secret.secret_arn,
                "AURORA_DATABASE": "e14",
            },
        )
        # Same two grants the Fly user has — the execution role carries them natively.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AuroraDataApi",
                actions=["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"],
                resources=[cluster.cluster_arn],
            )
        )
        secret.grant_read(fn)
        # The event source mapping also grants the role sqs:ReceiveMessage/DeleteMessage/
        # GetQueueAttributes on the queue. report_batch_item_failures wires the
        # partial-batch contract the handler returns (commit-then-delete semantics).
        fn.add_event_source(
            les.SqsEventSource(
                queue,
                batch_size=10,
                max_batching_window=Duration.seconds(5),
                report_batch_item_failures=True,
                max_concurrency=DRAIN_MAX_CONCURRENCY,  # protect Aurora/Data API from a stampede
            )
        )
        CfnOutput(self, "VoteDrainFn", value=fn.function_name)

    # --- CloudWatch alarms + notification ------------------------------------
    def _alarms(self, queue: sqs.Queue, dlq: sqs.Queue, cluster: rds.DatabaseCluster) -> None:
        # An SNS topic so the alarms below actually notify someone (they were previously silent
        # — they'd flip to ALARM in the console but page nobody). Set E14_ALERT_EMAIL at deploy
        # time to get email; a Telegram forwarder Lambda can subscribe to this same topic later.
        topic = sns.Topic(self, "AlertTopic", topic_name=f"{self.prefix}alerts")
        email = os.environ.get("E14_ALERT_EMAIL", "").strip()
        if email:
            topic.add_subscription(subs.EmailSubscription(email))
        CfnOutput(self, "AlertTopicArn", value=topic.topic_arn)

        action = cw_actions.SnsAction(topic)

        def _alarm(cid: str, **kwargs) -> cw.Alarm:
            a = cw.Alarm(self, cid, **kwargs)
            a.add_alarm_action(action)       # notify on ALARM
            a.add_ok_action(action)          # and on recovery (so you know it cleared)
            return a

        _alarm(
            "DlqNotEmpty",
            alarm_name=f"{self.prefix}dlq-not-empty",
            metric=dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(1), statistic="Maximum"
            ),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        _alarm(
            "QueueBacklogAge",
            alarm_name=f"{self.prefix}queue-age-high",
            metric=queue.metric_approximate_age_of_oldest_message(
                period=Duration.minutes(1), statistic="Maximum"
            ),
            threshold=Duration.minutes(5).to_seconds(),
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        _alarm(
            "AuroraCapacityHigh",
            alarm_name=f"{self.prefix}aurora-acu-high",
            metric=cw.Metric(
                namespace="AWS/RDS",
                metric_name="ServerlessDatabaseCapacity",
                dimensions_map={"DBClusterIdentifier": cluster.cluster_identifier},
                period=Duration.minutes(1),
                statistic="Maximum",
            ),
            threshold=AURORA_MAX_ACU * 0.9,
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
