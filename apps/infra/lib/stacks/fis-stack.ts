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
  /** ARN of the ECS cluster running the per-user + backend services. */
  clusterArn: string;
  /** Page-tier CloudWatch alarm ARNs from ObservabilityStack — wired in as
   * FIS stop-conditions so a real customer-impacting alarm aborts the run. */
  pageAlarmArns: string[];
}

/**
 * AWS FIS (Fault Injection Service) experiment templates for Chaos Testing
 * SP2. DEV ONLY — instantiated solely for the dev stage in isol8-stage.ts.
 *
 * Each template injects one real infra fault against dev and relies on a
 * least-privilege execution role plus CloudWatch stop-conditions to bound
 * the blast radius. Scenario 1 (container kill) ships here; later scenarios
 * widen the role and add templates.
 */
export class FisStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: FisStackProps) {
    super(scope, id, props);

    // FIS execution role — least privilege. Starts with the ECS-stop-task
    // surface scoped to the dev cluster; later scenarios widen it per added
    // template.
    const fisRole = new iam.Role(this, "FisExecutionRole", {
      assumedBy: new iam.ServicePrincipal("fis.amazonaws.com"),
      description: `isol8-${props.environment} FIS experiment execution role (dev only)`,
    });
    fisRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecs:DescribeTasks", "ecs:ListTasks", "ecs:StopTask"],
        // Scoped by the experiment target selection; the ecs:cluster
        // condition pins the action surface to the dev cluster.
        resources: ["*"],
        conditions: { StringEquals: { "ecs:cluster": props.clusterArn } },
      }),
    );

    // Stop-conditions: any page-tier alarm aborts the experiment. FIS
    // requires at least one stop-condition; when no alarms are wired yet we
    // fall back to the explicit "none" source.
    const stopConditions: fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty[] =
      props.pageAlarmArns.map((arn) => ({
        source: "aws:cloudwatch:alarm",
        value: arn,
      }));

    // Scenario 1 — kill one per-user OpenClaw container task mid-chat to
    // exercise gateway reconnect.
    new fis.CfnExperimentTemplate(this, "S1ContainerKill", {
      description:
        "SP2-S1: stop one per-user OpenClaw task to test gateway reconnect",
      roleArn: fisRole.roleArn,
      stopConditions: stopConditions.length
        ? stopConditions
        : [{ source: "none" }],
      tags: {
        Name: `isol8-${props.environment}-fis-s1-container-kill`,
        "isol8:chaos": "sp2",
      },
      targets: {
        userTask: {
          resourceType: "aws:ecs:task",
          selectionMode: "COUNT(1)",
          // Resource tags select per-user tasks; the dedicated chaos test
          // user's task carries isol8:owner=CHAOS_TEST_USER.
          resourceTags: { "isol8:owner": "CHAOS_TEST_USER" },
        },
      },
      actions: {
        stopTask: {
          actionId: "aws:ecs:stop-task",
          targets: { Tasks: "userTask" },
        },
      },
    });
  }
}
