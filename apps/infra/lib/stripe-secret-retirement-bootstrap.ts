import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

/** Standalone, temporary retirement role; never expands the general CI deployer. */
export class StripeSecretRetirementBootstrapStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);
    const role = new iam.Role(this, "RetirementRole", {
      roleName: "lightbulb-retire-stripe-secrets",
      description: "Main-only exact obsolete Stripe webhook secret retirement; no value reads",
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
    const secrets = ["dev", "prod"].flatMap(env => [
      "stripe_connect_webhook_secret", "stripe_marketplace_webhook_secret",
    ].map(name => this.formatArn({
      service: "secretsmanager", resource: "secret", resourceName: `lightbulb/${env}/${name}-??????`,
      arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
    })));
    const policy = new iam.Policy(this, "RetirementPermissions", {
      statements: [
        new iam.PolicyStatement({
          sid: "DescribeExactRetiredSecrets", actions: ["secretsmanager:DescribeSecret"], resources: secrets,
        }),
        new iam.PolicyStatement({
          sid: "RecoverablyDeleteExactRetiredSecrets", actions: ["secretsmanager:DeleteSecret"], resources: secrets,
          conditions: { NumericEquals: { "secretsmanager:RecoveryWindowInDays": 30 } },
        }),
        new iam.PolicyStatement({
          sid: "InspectDeployedConsumers", actions: [
            "cloudformation:ListStacks", "ecs:ListClusters", "ecs:ListServices", "ecs:DescribeServices",
            "ecs:ListTasks", "ecs:DescribeTasks", "ecs:DescribeTaskDefinition", "lambda:ListFunctions",
            "lambda:GetFunctionConfiguration",
          ], resources: ["*"],
          conditions: { StringEquals: { "aws:RequestedRegion": ["us-east-1", "us-west-2"] } },
        }),
        new iam.PolicyStatement({
          sid: "InspectPlatformTemplates", actions: ["cloudformation:GetTemplate"],
          resources: ["arn:aws:cloudformation:us-east-1:877352799272:stack/lightbulb-*/*"],
        }),
      ],
    });
    policy.attachToRole(role);
  }
}
