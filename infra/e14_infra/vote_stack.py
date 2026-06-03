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
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cw,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_rds as rds,
    aws_sqs as sqs,
)
from constructs import Construct

PREFIX = "e14-vote-"

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
DLQ_MAX_RECEIVE = 5


class VoteStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        vpc = self._vpc()
        cluster, secret = self._aurora(vpc)
        queue, dlq = self._queues()
        self._fly_user(queue, cluster, secret)
        self._alarms(queue, dlq, cluster)

        CfnOutput(self, "SqsQueueUrl", value=queue.queue_url, export_name="E14SqsQueueUrl")
        CfnOutput(self, "SqsDlqUrl", value=dlq.queue_url)
        CfnOutput(self, "AuroraClusterArn", value=cluster.cluster_arn, export_name="E14AuroraClusterArn")
        CfnOutput(self, "AuroraSecretArn", value=secret.secret_arn, export_name="E14AuroraSecretArn")
        CfnOutput(self, "AwsRegion", value=self.region)

    # --- VPC -----------------------------------------------------------------
    def _vpc(self) -> ec2.Vpc:
        # Isolated subnets only: the Data API is the access path, so no internet
        # egress (and no NAT gateway cost) is required.
        return ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"{PREFIX}vpc",
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
            cluster_identifier=f"{PREFIX}cluster",
            credentials=rds.Credentials.from_generated_secret(
                "e14admin", secret_name=f"{PREFIX}db-credentials"
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
            queue_name=f"{PREFIX}events-dlq",
            retention_period=QUEUE_RETENTION,
        )
        queue = sqs.Queue(
            self,
            "VoteQueue",
            queue_name=f"{PREFIX}events",
            visibility_timeout=QUEUE_VISIBILITY,
            retention_period=QUEUE_RETENTION,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=DLQ_MAX_RECEIVE, queue=dlq),
        )
        return queue, dlq

    # --- IAM user for Fly ----------------------------------------------------
    def _fly_user(self, queue: sqs.Queue, cluster: rds.DatabaseCluster, secret) -> None:
        # No access key here on purpose: minting it out-of-band (see README)
        # keeps the secret out of CloudFormation state. CDK only grants the perms.
        user = iam.User(self, "FlyUser", user_name=f"{PREFIX}fly")

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

    # --- CloudWatch alarms (nice-to-have) ------------------------------------
    def _alarms(self, queue: sqs.Queue, dlq: sqs.Queue, cluster: rds.DatabaseCluster) -> None:
        cw.Alarm(
            self,
            "DlqNotEmpty",
            alarm_name=f"{PREFIX}dlq-not-empty",
            metric=dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(1), statistic="Maximum"
            ),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        cw.Alarm(
            self,
            "QueueBacklogAge",
            alarm_name=f"{PREFIX}queue-age-high",
            metric=queue.metric_approximate_age_of_oldest_message(
                period=Duration.minutes(1), statistic="Maximum"
            ),
            threshold=Duration.minutes(5).to_seconds(),
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        cw.Alarm(
            self,
            "AuroraCapacityHigh",
            alarm_name=f"{PREFIX}aurora-acu-high",
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
