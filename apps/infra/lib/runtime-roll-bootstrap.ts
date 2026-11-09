import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

/** Manual runtime maintenance, separate from application deployment and without secret reads in CI. */
export class RuntimeRollBootstrapStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);
    for (const environment of ["dev", "prod"]) {
      const cluster = this.formatArn({ service: "ecs", resource: "cluster", resourceName: `lightbulb-${environment}-container` });
      const task = this.formatArn({ service: "ecs", resource: "task", resourceName: `lightbulb-${environment}-container/*` });
      const definition = this.formatArn({ service: "ecs", resource: "task-definition", resourceName: `lightbulb-${environment}-backend:*` });
      const service = this.formatArn({ service: "ecs", resource: "service", resourceName: `lightbulb-${environment}-container/lightbulb-${environment}-backend` });
      const role = new iam.Role(this, `${environment}RollRole`, {
        roleName: `lightbulb-${environment}-runtime-roll`,
        description: "Main-only workflow: one deployed backend task for an idle-safe workspace roll",
        maxSessionDuration: cdk.Duration.hours(1),
        assumedBy: new iam.FederatedPrincipal(
          "arn:aws:iam::877352799272:oidc-provider/token.actions.githubusercontent.com",
          { StringEquals: {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": "repo:TheLightbulbCompany@253404738/lightbulb@1182791203:ref:refs/heads/main",
          } }, "sts:AssumeRoleWithWebIdentity",
        ),
      });
      const requestTags = {
        "aws:RequestTag/ManagedBy": "lightbulb-runtime-roll",
        "aws:RequestTag/RollSession": "${aws:userid}",
      };
      new iam.Policy(this, `${environment}RollPolicy`, { statements: [
        new iam.PolicyStatement({ sid: "ReadOnlyBackendService", actions: ["ecs:DescribeServices"], resources: [service] }),
        // ECS DescribeTaskDefinition has no resource-level authorization. It returns references, not secret values.
        new iam.PolicyStatement({ sid: "ReadTaskDefinition", actions: ["ecs:DescribeTaskDefinition"], resources: ["*"] }),
        new iam.PolicyStatement({
          sid: "LaunchTaggedBackendTask", actions: ["ecs:RunTask"], resources: [definition],
          conditions: {
            ArnEquals: { "ecs:cluster": cluster },
            StringEquals: requestTags,
            "ForAllValues:StringEquals": { "aws:TagKeys": ["ManagedBy", "RollSession"] },
            Bool: { "ecs:enable-execute-command": "false" },
          },
        }),
        new iam.PolicyStatement({
          sid: "TagOnlyOnTaskCreation", actions: ["ecs:TagResource"], resources: [task],
          conditions: {
            StringEquals: { "ecs:CreateAction": "RunTask", ...requestTags },
            "ForAllValues:StringEquals": { "aws:TagKeys": ["ManagedBy", "RollSession"] },
          },
        }),
        new iam.PolicyStatement({
          sid: "ObserveOrStopOnlyThisSessionTask",
          actions: ["ecs:DescribeTasks", "ecs:ListTagsForResource", "ecs:StopTask"], resources: [task],
          conditions: { StringEquals: {
            "aws:ResourceTag/ManagedBy": "lightbulb-runtime-roll",
            "aws:ResourceTag/RollSession": "${aws:userid}",
          } },
        }),
        new iam.PolicyStatement({
          sid: "PassOnlyExistingBackendRoles", actions: ["iam:PassRole"],
          resources: ["backend-task", "backend-task-execution"].map((name) => this.formatArn({
            service: "iam", region: "", resource: "role", resourceName: `lightbulb-${environment}-${name}`,
          })), conditions: { StringEquals: { "iam:PassedToService": "ecs-tasks.amazonaws.com" } },
        }),
        new iam.PolicyStatement({
          sid: "ReadRollOutput", actions: ["logs:GetLogEvents"], resources: [this.formatArn({
            service: "logs", resource: "log-group", resourceName: `/ecs/lightbulb-${environment}:log-stream:backend/backend/*`,
            arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
          })],
        }),
      ] }).attachToRole(role);
    }
  }
}
