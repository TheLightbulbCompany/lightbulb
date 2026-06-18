import * as cdk from "aws-cdk-lib";
import * as fis from "aws-cdk-lib/aws-fis";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

export interface FisStackProps extends cdk.StackProps {
  /**
   * Environment name string ("dev"). Named `environment` to match the
   * house convention used by every other stack (ContainerStack,
   * ServiceStack, etc.) — distinct from `cdk.StackProps.env`, which carries
   * the account/region object. Reusing the `env` key here would clash.
   */
  environment: string;
  /** ARN of the ECS cluster running the per-user + backend + Paperclip
   * services. All three run in the same cluster (see container-stack.ts;
   * ServiceStack + PaperclipStack are both handed `container.cluster`). */
  clusterArn: string;
  /**
   * CURATED universal FIS stop-condition alarm ARNs from ObservabilityStack
   * (`observability.fisStopAlarmArns`): P9 (alb-5xx-rate),
   * P10 (apigw-ws-5xx-rate), W10 (chat-error-rate). Rate-based + NOT_BREACHING
   * blast gauges that cannot self-trip on idle. Applied to every NON-container
   * experiment. NOT `pageAlarmArns` — that set is ~12 alarms (over FIS's cap of
   * 5) and includes breach-on-event trip-wires that would abort an experiment
   * for unrelated reasons. This is the fix for the rollback that blocked prod
   * in #917.
   */
  stopAlarmArns: string[];
  /**
   * CURATED container-scenario FIS stop-condition alarm ARNs from
   * ObservabilityStack (`observability.fisContainerStopAlarmArns`): the
   * universal set PLUS P1 (container-error-state). Applied to the container
   * scenarios (S1 container-kill, S2 cpu-stress), which additionally want to
   * abort if a per-user box is driven into a stuck/error state. ≤5 entries.
   */
  containerStopAlarmArns: string[];
  /**
   * ECS service name of the Paperclip Fargate service
   * (`isol8-${env}-paperclip-server`). Used by the S5 `aws:ecs:task`
   * target's `service` parameter to select the Paperclip task.
   */
  paperclipServiceName: string;
  /**
   * ECS service name of the backend FastAPI Fargate service. CDK
   * auto-generates this (ServiceStack does not set an explicit name), so it
   * is threaded through from the stage as `service.service.serviceName`.
   * Used by the S4 `aws:ecs:task` target's `service` parameter.
   */
  backendServiceName: string;
  /**
   * ARN of the dev Paperclip Aurora cluster (`database.paperclipDbCluster
   * .clusterArn`). S6 targets it with `aws:rds:cluster`.
   */
  auroraClusterArn: string;
  /**
   * ARN of the backend FastAPI task role. S3 targets it with
   * `aws:iam:role` to inject API errors into the calls that role makes.
   */
  backendTaskRoleArn: string;
  /** VPC id of the dev VPC. Used as a target parameter / filter scope for
   * the subnet-based network-disruption templates (S3, S7, S8). */
  vpcId: string;
  /** ARNs of the private (PRIVATE_WITH_EGRESS) subnets that run the backend
   * + Paperclip + per-user containers. The `aws:ec2:subnet` network
   * templates target these. */
  privateSubnetArns: string[];
}

/**
 * AWS FIS (Fault Injection Service) experiment templates for Chaos Testing
 * SP2. DEV ONLY — instantiated solely for the dev stage in isol8-stage.ts.
 *
 * Each template injects one real infra fault against dev and relies on a
 * least-privilege execution role plus CloudWatch stop-conditions to bound
 * the blast radius. The role is widened per action below: ECS stop-task +
 * task-cpu-stress (SSM), RDS failover, IAM-role API-error injection, and
 * the network-ACL disrupt-connectivity surface.
 *
 * STOP-CONDITIONS: a deliberately small, CURATED set of rate-based blast
 * gauges (P9 alb-5xx-rate, P10 apigw-ws-5xx-rate, W10 chat-error-rate) is
 * applied to every template; container scenarios additionally watch P1
 * (container-error-state). FIS caps stop-conditions at 5 per template, so the
 * full `pageAlarmArns` set (~12, with breach-on-event trip-wires) is NEVER
 * used here — wiring it is exactly what rolled back the first FIS deploy and
 * blocked prod (#917).
 *
 * SHARED-INFRA WARNING: S3/S4/S6/S7/S8 affect resources the whole dev
 * environment shares (backend task, Aurora, subnets). Run them only in a
 * quiet window per the game-day runbook.
 */
export class FisStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: FisStackProps) {
    super(scope, id, props);

    // ─────────────────────────────────────────────────────────────────
    // FIS execution role — least privilege, widened per action.
    // ─────────────────────────────────────────────────────────────────
    const fisRole = new iam.Role(this, "FisExecutionRole", {
      assumedBy: new iam.ServicePrincipal("fis.amazonaws.com"),
      description: `isol8-${props.environment} FIS experiment execution role (dev only)`,
    });

    // S1/S4/S5 — ECS stop-task. Scoped to the dev cluster via the
    // ecs:cluster condition. (S5 stops the Paperclip task, S4 the backend
    // task — both live in the same cluster, so one condition covers all.)
    fisRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EcsStopTask",
        actions: ["ecs:DescribeTasks", "ecs:ListTasks", "ecs:StopTask"],
        resources: ["*"],
        conditions: { StringEquals: { "ecs:cluster": props.clusterArn } },
      }),
    );

    // tag:GetResources is required by the ecs:stop-task and
    // rds:failover-db-cluster actions for FIS's resource resolution. It is a
    // read-only, resource-listing permission that does not support
    // resource-level scoping, so it is granted on "*".
    fisRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "FisResourceTagging",
        actions: ["tag:GetResources"],
        resources: ["*"],
      }),
    );

    // S2 — aws:ecs:task-cpu-stress injects via SSM RunCommand against the
    // SSM-managed ECS task (ECS Exec channel). The action itself needs the
    // SSM RunCommand surface (SendCommand/ListCommands/CancelCommand) plus
    // ecs:DescribeTasks (already granted above). The ssmmessages:* channel
    // actions + ecs:ExecuteCommand are what make the per-user task
    // SSM-managed in the first place (the prereq Task 3 enables
    // enableExecuteCommand on the per-user service). Grant both surfaces.
    fisRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EcsTaskCpuStressSsm",
        actions: [
          "ssm:SendCommand",
          "ssm:ListCommands",
          "ssm:CancelCommand",
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
          "ecs:ExecuteCommand",
        ],
        resources: ["*"],
      }),
    );

    // S6 — aws:rds:failover-db-cluster on the dev Paperclip Aurora cluster.
    fisRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "RdsFailover",
        actions: ["rds:FailoverDBCluster", "rds:DescribeDBClusters"],
        resources: [props.auroraClusterArn],
      }),
    );

    // S3 — aws:fis:inject-api-throttle-error against the backend task role.
    // NOTE: the FIS api-error injection actions only support service
    // namespaces `ec2` and `kinesis` — NOT dynamodb (verified against the
    // FIS actions reference). The DynamoDB *data-plane* throttle is
    // therefore implemented as a network-ACL disruption (S3 template below,
    // scope=dynamodb) rather than this API-error injection. This permission
    // is granted so the role can ALSO drive an ec2-namespace api-throttle
    // experiment if one is added later; it is the documented perm for the
    // inject-api-*-error family.
    fisRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "FisInjectApiError",
        actions: [
          "fis:InjectApiThrottleError",
          "fis:InjectApiUnavailableError",
          "fis:InjectApiInternalError",
        ],
        resources: ["*"],
      }),
    );

    // S3/S7/S8 — aws:network:disrupt-connectivity clones the subnet's NACL
    // and adds deny rules (tagged managedByFIS=true) for the experiment's
    // duration, then restores. These are the exact permissions the action
    // requires (FIS actions reference).
    fisRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "NetworkDisruptConnectivity",
        actions: [
          "ec2:CreateNetworkAcl",
          "ec2:CreateNetworkAclEntry",
          "ec2:CreateTags",
          "ec2:DeleteNetworkAcl",
          "ec2:DescribeManagedPrefixLists",
          "ec2:DescribeNetworkAcls",
          "ec2:DescribeSubnets",
          "ec2:DescribeVpcs",
          "ec2:GetManagedPrefixListEntries",
          "ec2:ReplaceNetworkAclAssociation",
        ],
        resources: ["*"],
      }),
    );

    // ─────────────────────────────────────────────────────────────────
    // Stop-conditions — CURATED, ≤5 per template (FIS cap).
    //
    // `universalStop`   = P9/P10/W10 (rate-based blast gauges).
    // `containerStop`   = universal + P1 (container-error-state).
    //
    // Each is at most a handful of alarms; both stay well under the FIS cap
    // of 5. FIS requires at least one stop-condition, so we fall back to the
    // explicit "none" source if a curated list is somehow empty.
    // ─────────────────────────────────────────────────────────────────
    const toStopConditions = (
      arns: string[],
    ): fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty[] => {
      const conditions = arns.map((arn) => ({
        source: "aws:cloudwatch:alarm",
        value: arn,
      }));
      return conditions.length ? conditions : [{ source: "none" }];
    };
    const universalStop = toStopConditions(props.stopAlarmArns);
    const containerStop = toStopConditions(props.containerStopAlarmArns);

    // ─────────────────────────────────────────────────────────────────
    // Scenario 1 — kill one per-user OpenClaw container task mid-chat to
    // exercise gateway reconnect. CONTAINER scenario → containerStop.
    // ─────────────────────────────────────────────────────────────────
    new fis.CfnExperimentTemplate(this, "S1ContainerKill", {
      description:
        "SP2-S1: stop one per-user OpenClaw task to test gateway reconnect",
      roleArn: fisRole.roleArn,
      stopConditions: containerStop,
      tags: {
        Name: `isol8-${props.environment}-fis-s1-container-kill`,
        "isol8:chaos": "sp2",
      },
      targets: {
        // PREREQUISITE (Phase B / game-day): the dedicated chaos test
        // container's ECS task MUST be tagged isol8:owner=CHAOS_TEST_USER
        // at the ECS level for this selector to match. The per-user
        // create_service in ecs_manager.py does NOT currently propagate
        // owner tags onto its tasks, so the chaos test service must be
        // tagged manually (or via the game-day runbook) before running S1.
        // Until then this target resolves to zero tasks and the experiment
        // is a no-op. The ClusterArn filter below additionally scopes the
        // selection to the dev cluster at the target level (not just via
        // the IAM ecs:cluster condition).
        userTask: {
          resourceType: "aws:ecs:task",
          selectionMode: "COUNT(1)",
          // Resource tags select per-user tasks; the dedicated chaos test
          // user's task carries isol8:owner=CHAOS_TEST_USER.
          resourceTags: { "isol8:owner": "CHAOS_TEST_USER" },
          filters: [{ path: "ClusterArn", values: [props.clusterArn] }],
        },
      },
      actions: {
        stopTask: {
          actionId: "aws:ecs:stop-task",
          targets: { Tasks: "userTask" },
        },
      },
    });

    // ─────────────────────────────────────────────────────────────────
    // Scenario 2 — CPU-stress the per-user container to 100% for 5 minutes
    // to reproduce (or rule out) the healthcheck crash-loop on the
    // 0.5-vCPU box. Injects via SSM RunCommand (enableExecuteCommand on the
    // per-user service is the prereq). CONTAINER scenario → containerStop.
    // ─────────────────────────────────────────────────────────────────
    new fis.CfnExperimentTemplate(this, "S2CpuStress", {
      description:
        "SP2-S2: CPU-stress the per-user OpenClaw task to 100% for 5m (crash-loop probe)",
      roleArn: fisRole.roleArn,
      stopConditions: containerStop,
      tags: {
        Name: `isol8-${props.environment}-fis-s2-cpu-stress`,
        "isol8:chaos": "sp2",
      },
      targets: {
        // Same selector as S1 — the dedicated chaos test user's task. Same
        // game-day tagging prerequisite applies.
        userTask: {
          resourceType: "aws:ecs:task",
          selectionMode: "COUNT(1)",
          resourceTags: { "isol8:owner": "CHAOS_TEST_USER" },
          filters: [{ path: "ClusterArn", values: [props.clusterArn] }],
        },
      },
      actions: {
        cpuStress: {
          actionId: "aws:ecs:task-cpu-stress",
          parameters: { duration: "PT5M", percent: "100" },
          targets: { Tasks: "userTask" },
        },
      },
    });

    // ─────────────────────────────────────────────────────────────────
    // Scenario 5 — kill the Paperclip Fargate task to test Teams BFF
    // recovery. Targets by ECS service parameter (deterministic; no task
    // tagging needed — the service publishes its tasks to FIS via the
    // DescribeTasks-with-service resolution). → universalStop.
    // ─────────────────────────────────────────────────────────────────
    new fis.CfnExperimentTemplate(this, "S5PaperclipKill", {
      description:
        "SP2-S5: stop the Paperclip Fargate task to test Teams BFF recovery",
      roleArn: fisRole.roleArn,
      stopConditions: universalStop,
      tags: {
        Name: `isol8-${props.environment}-fis-s5-paperclip-kill`,
        "isol8:chaos": "sp2",
      },
      targets: {
        paperclipTask: {
          resourceType: "aws:ecs:task",
          selectionMode: "COUNT(1)",
          parameters: {
            cluster: props.clusterArn,
            service: props.paperclipServiceName,
          },
        },
      },
      actions: {
        stopTask: {
          actionId: "aws:ecs:stop-task",
          targets: { Tasks: "paperclipTask" },
        },
      },
    });

    // ─────────────────────────────────────────────────────────────────
    // Scenario 6 — force an Aurora failover on the dev Paperclip cluster to
    // test that provisioning + /teams reconnect to the new writer.
    // Recovery, not zero-disruption, is the assertion (failover is
    // disruptive by design). → universalStop.
    // ─────────────────────────────────────────────────────────────────
    new fis.CfnExperimentTemplate(this, "S6AuroraFailover", {
      description:
        "SP2-S6: failover the dev Paperclip Aurora cluster to test writer-flip reconnect",
      roleArn: fisRole.roleArn,
      stopConditions: universalStop,
      tags: {
        Name: `isol8-${props.environment}-fis-s6-aurora-failover`,
        "isol8:chaos": "sp2",
      },
      targets: {
        auroraCluster: {
          resourceType: "aws:rds:cluster",
          selectionMode: "ALL",
          resourceArns: [props.auroraClusterArn],
        },
      },
      actions: {
        failover: {
          actionId: "aws:rds:failover-db-cluster",
          targets: { Clusters: "auroraCluster" },
        },
      },
    });

    // ─────────────────────────────────────────────────────────────────
    // Scenario 7 — backend↔Paperclip network partition.
    //
    // CONCERN / TODO(gameday): aws:network:disrupt-connectivity operates at
    // the SUBNET level (it clones the subnet NACL). The backend, Paperclip,
    // AND per-user containers all run in the SAME PRIVATE_WITH_EGRESS
    // subnets (see network-stack.ts subnetConfiguration + the FargateService
    // vpcSubnets in service-stack.ts / paperclip-stack.ts). A subnet-scoped
    // NACL deny therefore CANNOT selectively cut only the backend→Paperclip
    // path — `scope: all`/`vpc` would also sever the backend↔per-user
    // gateway and DDB/Bedrock egress, which is a different blast radius than
    // S7 intends.
    //
    // There is no FIS primitive that cuts a single intra-subnet service→
    // service path by SG/Cloud-Map name. The faithful S7 fault would be
    // either (a) a deliberately separate subnet for Paperclip so it can be
    // NACL-isolated, or (b) an SG-rule swap done out-of-band by the
    // game-day operator. Until that scope is decided, this template targets
    // the private subnets with scope=all as a PLACEHOLDER so it synthesizes
    // and is runnable, but the operator MUST confirm the intended blast
    // radius before running it. See the report's CONCERNS.
    // → universalStop.
    new fis.CfnExperimentTemplate(this, "S7BackendPaperclipPartition", {
      description:
        "SP2-S7: backend<->Paperclip network partition (3m) — see TODO(gameday) on target scope",
      roleArn: fisRole.roleArn,
      stopConditions: universalStop,
      tags: {
        Name: `isol8-${props.environment}-fis-s7-backend-paperclip-partition`,
        "isol8:chaos": "sp2",
      },
      targets: {
        // TODO(gameday): confirm target scope against dev VPC. These are the
        // shared private subnets; scope=all here severs MORE than just the
        // backend↔Paperclip path. Narrow before running (dedicated Paperclip
        // subnet, or operator-driven SG swap).
        backendPaperclipSubnets: {
          resourceType: "aws:ec2:subnet",
          selectionMode: "ALL",
          resourceArns: props.privateSubnetArns,
          parameters: { vpc: props.vpcId },
        },
      },
      actions: {
        disrupt: {
          actionId: "aws:network:disrupt-connectivity",
          parameters: { scope: "all", duration: "PT3M" },
          targets: { Subnets: "backendPaperclipSubnets" },
        },
      },
    });

    // ─────────────────────────────────────────────────────────────────
    // Scenario 3 — DynamoDB data-plane throttle / unavailable.
    //
    // Implemented as aws:network:disrupt-connectivity with scope=dynamodb
    // (denies traffic to the regional DynamoDB endpoint from the target
    // subnets) — NOT aws:fis:inject-api-throttle-error, because that action
    // only supports the `ec2` and `kinesis` service namespaces (verified
    // against the FIS actions reference; dynamodb is unsupported). The
    // network-ACL scope=dynamodb is the documented FIS way to fault
    // DynamoDB reachability and is the plan's own Task-11 fallback. It
    // targets the private subnets that run the backend, so the backend's
    // DDB calls fail for the duration. SHARED-INFRA — quiet window only.
    //
    // STOP-CONDITION NOTE: the curated universal set is P9/P10/W10 — none of
    // which is a DynamoDB alarm — so this scenario does NOT stop-condition on
    // a fault it itself induces. (Wiring P8 dynamodb-throttle-sustained here
    // would self-trip; it is deliberately excluded.) → universalStop.
    // ─────────────────────────────────────────────────────────────────
    new fis.CfnExperimentTemplate(this, "S3DdbThrottle", {
      description:
        "SP2-S3: deny DynamoDB endpoint reachability from backend subnets for 3m (data-plane throttle/unavailable)",
      roleArn: fisRole.roleArn,
      stopConditions: universalStop,
      tags: {
        Name: `isol8-${props.environment}-fis-s3-ddb-throttle`,
        "isol8:chaos": "sp2",
      },
      targets: {
        backendSubnets: {
          resourceType: "aws:ec2:subnet",
          selectionMode: "ALL",
          resourceArns: props.privateSubnetArns,
          parameters: { vpc: props.vpcId },
        },
      },
      actions: {
        disruptDynamodb: {
          actionId: "aws:network:disrupt-connectivity",
          parameters: { scope: "dynamodb", duration: "PT3M" },
          targets: { Subnets: "backendSubnets" },
        },
      },
    });

    // ─────────────────────────────────────────────────────────────────
    // Scenario 4 — kill the backend FastAPI task. Targets by ECS service
    // parameter (the backend service's auto-generated name threaded from
    // the stage). ECS restarts the task; the assertion is recovery within
    // SLO. SHARED-INFRA (full backend outage) — quiet window only.
    // → universalStop.
    // ─────────────────────────────────────────────────────────────────
    new fis.CfnExperimentTemplate(this, "S4BackendKill", {
      description:
        "SP2-S4: stop the backend FastAPI task to test ECS restart + ALB recovery",
      roleArn: fisRole.roleArn,
      stopConditions: universalStop,
      tags: {
        Name: `isol8-${props.environment}-fis-s4-backend-kill`,
        "isol8:chaos": "sp2",
      },
      targets: {
        backendTask: {
          resourceType: "aws:ecs:task",
          selectionMode: "COUNT(1)",
          parameters: {
            cluster: props.clusterArn,
            service: props.backendServiceName,
          },
        },
      },
      actions: {
        stopTask: {
          actionId: "aws:ecs:stop-task",
          targets: { Tasks: "backendTask" },
        },
      },
    });

    // ─────────────────────────────────────────────────────────────────
    // Scenario 8 — billing provider (Stripe/Metronome) egress block.
    //
    // CONCERN / TODO(gameday): aws:network:disrupt-connectivity can deny
    // egress to a specific destination only via scope=prefix-list with a
    // customer-managed prefix list of the destination CIDRs. Isol8 has NO
    // prefix list of Stripe/Metronome endpoints today (Stripe publishes IP
    // ranges that drift; Metronome's are not enumerated in this repo). The
    // only subnet-level alternatives (scope=all / scope=vpc) would block ALL
    // egress, not just billing — wrong blast radius for "billing provider
    // unreachable while everything else stays up".
    //
    // The faithful S8 requires either (a) a customer-managed prefix list
    // populated with Stripe + Metronome CIDRs wired here and referenced via
    // prefixListIdentifier, or (b) an SG egress-rule swap done out-of-band.
    // Until that prefix list exists this template targets the private
    // subnets with scope=all as a PLACEHOLDER (3m) so it synthesizes and is
    // runnable, but the operator MUST narrow it to the billing prefix list
    // before running. See the report's CONCERNS. → universalStop.
    new fis.CfnExperimentTemplate(this, "S8BillingEgressBlock", {
      description:
        "SP2-S8: block billing-provider (Stripe/Metronome) egress (3m) — see TODO(gameday) on prefix-list scope",
      roleArn: fisRole.roleArn,
      stopConditions: universalStop,
      tags: {
        Name: `isol8-${props.environment}-fis-s8-billing-egress-block`,
        "isol8:chaos": "sp2",
      },
      targets: {
        // TODO(gameday): confirm target scope against dev VPC. scope=all on
        // the shared subnets blocks ALL egress, not just billing. Replace
        // with scope=prefix-list + a Stripe/Metronome customer-managed
        // prefix list (prefixListIdentifier) before running.
        billingEgressSubnets: {
          resourceType: "aws:ec2:subnet",
          selectionMode: "ALL",
          resourceArns: props.privateSubnetArns,
          parameters: { vpc: props.vpcId },
        },
      },
      actions: {
        blockEgress: {
          actionId: "aws:network:disrupt-connectivity",
          parameters: { scope: "all", duration: "PT3M" },
          targets: { Subnets: "billingEgressSubnets" },
        },
      },
    });
  }
}
