import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import { Template, Match } from "aws-cdk-lib/assertions";
import {
  BedrockMeterStack,
  BedrockMeterStackProps,
} from "../lib/stacks/bedrock-meter-stack";

describe("BedrockMeterStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const env = { account: "877352799272", region: "us-east-1" };
    const support = new cdk.Stack(app, "Support", { env });
    const vpc = new ec2.Vpc(support, "Vpc");
    const alb = new elbv2.ApplicationLoadBalancer(support, "Alb", {
      vpc,
      internetFacing: false,
    });

    const props: BedrockMeterStackProps = {
      env,
      envName: "prod",
      vpc,
      alb,
      albSecurityGroup: alb.connections.securityGroups[0],
      meterTokenSecretName: "isol8/prod/bedrock_meter_token",
      meterKmsKeyArn: "arn:aws:kms:us-east-1:877352799272:key/abc-123",
      fanoutRegions: ["us-east-1"],
    };

    const stack = new BedrockMeterStack(app, "BedrockMeterStack", props);
    template = Template.fromStack(stack);
  });

  test("creates the meter Lambda with ALB_DNS_NAME env", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      Runtime: "python3.12",
      Environment: {
        Variables: Match.objectLike({
          ALB_DNS_NAME: Match.anyValue(),
        }),
      },
    });
  });

  test("creates an SQS DLQ", () => {
    template.resourceCountIs("AWS::SQS::Queue", 1);
  });

  test("Lambda SG has egress to ALB on port 80", () => {
    template.hasResourceProperties("AWS::EC2::SecurityGroupEgress", {
      FromPort: 80,
      ToPort: 80,
    });
  });
});
