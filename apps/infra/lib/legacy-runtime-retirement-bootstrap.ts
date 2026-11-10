import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

/** Standalone retirement role for legacy per-owner AgentCore runtimes; never expands the general CI deployer.
 * IAM itself keeps both pools (`lightbulb_{env}_hermes_pool`) and every non-runtime resource out of reach. */
export class LegacyRuntimeRetirementBootstrapStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, { ...props, analyticsReporting: false });
    const role = new iam.Role(this, "RetirementRole", {
      roleName: "lightbulb-retire-legacy-runtimes",
      description: "Main-only deletion of allowlisted legacy per-owner AgentCore runtime definitions",
      maxSessionDuration: cdk.Duration.hours(1),
      assumedBy: new iam.FederatedPrincipal(
        "arn:aws:iam::877352799272:oidc-provider/token.actions.githubusercontent.com",
        { StringEquals: {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:TheLightbulbCompany@253404738/lightbulb@1182791203:ref:refs/heads/main",
        } },
        "sts:AssumeRoleWithWebIdentity",
      ),
    });
    const envs = ["dev", "prod"];
    const runtimes = envs.map(env => `arn:aws:bedrock-agentcore:us-east-1:877352799272:runtime/lb_${env}_org_*`);
    new iam.Policy(this, "RetirementPermissions", {
      roles: [role],
      statements: [
        new iam.PolicyStatement({
          sid: "LegacyRuntimesOnly",
          actions: ["bedrock-agentcore:GetAgentRuntime", "bedrock-agentcore:DeleteAgentRuntime"],
          resources: runtimes,
        }),
        new iam.PolicyStatement({
          sid: "ReadOwnerAndSessionRows",
          actions: ["dynamodb:GetItem", "dynamodb:Scan"],
          resources: envs.map(env => `arn:aws:dynamodb:us-east-1:877352799272:table/lightbulb-${env}-containers`),
        }),
        new iam.PolicyStatement({
          sid: "ReadRuntimeActivity", actions: ["cloudwatch:GetMetricData"], resources: ["*"],
        }),
      ],
    });
  }
}
