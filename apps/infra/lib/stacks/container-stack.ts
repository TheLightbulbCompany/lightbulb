import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as events from "aws-cdk-lib/aws-events";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import { logGroupPath } from "../table-names";

/** Dev ECS services run on Fargate Spot only (on-demand weight 0: never placed while Spot has
 *  capacity). The cluster enables both providers. Prod stays on on-demand Fargate. */
export const DEV_SPOT: ecs.CapacityProviderStrategy[] = [
  { capacityProvider: "FARGATE_SPOT", weight: 1 },
  { capacityProvider: "FARGATE", weight: 0 },
];

/**
 * The platform ECS cluster the backend Fargate service runs on, plus the
 * EventBridge trail that keeps the cluster's task-state changes past ECS's
 * one-hour window.
 */
export interface ContainerStackProps extends cdk.StackProps {
  environment: string;
  /** Sets no cluster property, but the backend service places its tasks in `cluster.vpc`. */
  vpc: ec2.IVpc;
}

export class ContainerStack extends cdk.Stack {
  public readonly cluster: ecs.Cluster;

  constructor(scope: Construct, id: string, props: ContainerStackProps) {
    super(scope, id, props);

    // Container Insights stays off: AWS/ECS standard metrics plus custom EMF
    // cover the backend's alarms.
    this.cluster = new ecs.Cluster(this, "Cluster", {
      // Explicit so the physical name stops embedding the old stack name; changing
      // it replaces the cluster, which takes the backend down with it.
      clusterName: `lightbulb-${props.environment}-container`,
      vpc: props.vpc,
      containerInsights: false,
      // FARGATE + FARGATE_SPOT capacity providers. Dev's backend runs on Spot (DEV_SPOT).
      enableFargateCapacityProviders: true,
    });

    // ECS task-state-change trail for the PLATFORM cluster. ECS keeps a stopped
    // task's record for ~1 hour, then drops `stoppedReason`/`stopCode` entirely —
    // and this repo has already lost RCA time to exactly that window (the backend
    // freeze and the crash-loop investigations both needed a stop reason that had
    // aged out). EventBridge forwards every transition on this cluster to a log
    // group so the full event JSON stays queryable in Logs Insights for months.
    // ~1 KB an event, well under $1/mo.
    const ecsTaskEventsLogGroup = new logs.LogGroup(
      this,
      "EcsTaskEventsLogGroup",
      {
        logGroupName: logGroupPath(props.environment, "ecs-task-events"),
        // The stage's log-retention aspect caps this at 14 (dev) / 30 (prod) days.
        retention: logs.RetentionDays.THREE_MONTHS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      },
    );

    new events.Rule(this, "EcsTaskStateChangeRule", {
      ruleName: `lightbulb-${props.environment}-ecs-task-state-change`,
      description:
        `Persist ECS Task State Change events for ${this.cluster.clusterName} to a CloudWatch log group, so we can reconstruct task stop reasons after the 1h ECS retention window expires.`,
      eventPattern: {
        source: ["aws.ecs"],
        detailType: ["ECS Task State Change"],
        detail: {
          // Only events from THIS cluster — avoids polluting the log group with
          // other clusters' noise.
          clusterArn: [this.cluster.clusterArn],
        },
      },
      // Raw target instead of targets.CloudWatchLogGroup: the L2 target
      // ships an AwsCustomResource singleton Lambda (auto-named after the
      // retired isol8 stack name) just to write the log-group resource
      // policy. The native AWS::Logs::ResourcePolicy below does the same
      // thing with no helper function.
      targets: [{ bind: () => ({ id: "", arn: ecsTaskEventsLogGroup.logGroupArn }) }],
    });

    new logs.ResourcePolicy(this, "EcsTaskEventsLogPolicy", {
      resourcePolicyName: `lightbulb-${props.environment}-ecs-task-events`,
      policyStatements: [
        new iam.PolicyStatement({
          actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
          principals: [
            new iam.ServicePrincipal("events.amazonaws.com"),
            new iam.ServicePrincipal("delivery.logs.amazonaws.com"),
          ],
          resources: [ecsTaskEventsLogGroup.logGroupArn],
        }),
      ],
    });
  }
}
