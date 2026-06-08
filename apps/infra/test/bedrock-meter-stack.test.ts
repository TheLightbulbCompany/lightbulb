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

  test("creates a Bedrock invocation-log group + S3 sink", () => {
    // The Bedrock invocation-log group, by name (the Lambda log-retention
    // construct also provisions its own group, so do not count to exactly 1).
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: Match.stringLikeRegexp("/isol8/prod/bedrock-invocation"),
    });
    // The S3 sink bucket for >100KB body offload.
    template.resourceCountIs("AWS::S3::Bucket", 1);
  });

  test("AwsCustomResource sets logging config with text+embedding modalities", () => {
    // The custom resource encodes the putModelInvocationLoggingConfiguration
    // call (incl. both modalities) in its Create parameters. CDK serializes the
    // SDK action with its lowercase initial (`put...`); the capital-P spelling
    // only appears in the IAM policy (`bedrock:Put...`), not the CR body.
    const customResources = template.findResources("Custom::AWS");
    const bodies = JSON.stringify(customResources);
    expect(bodies).toContain("putModelInvocationLoggingConfiguration");
    expect(bodies).toContain("textDataDeliveryEnabled");
    expect(bodies).toContain("embeddingDataDeliveryEnabled");
  });

  test("logging-config custom resource uses the SDK-exact nested key path", () => {
    const customResources = template.findResources("Custom::AWS");
    const cfg = Object.values(customResources).find((r: any) =>
      JSON.stringify(r).includes("putModelInvocationLoggingConfiguration"),
    ) as any;
    expect(cfg).toBeDefined();
    // The Create payload carries CFN refs (log-group name, role ARN, bucket
    // name are tokens), so CDK emits it as an `Fn::Join`, NOT a plain string —
    // the plan's documented fallback. Stringify the whole CR and assert the
    // SDK-exact nested key path appears as adjacent substrings: the top-level
    // wrapper is lowercase `loggingConfig` and the nested keys sit under it.
    const body: string = JSON.stringify(cfg);
    expect(body).toContain('\\"loggingConfig\\":{\\"cloudWatchConfig\\"');
    expect(body).toContain('\\"s3Config\\"');
    expect(body).toContain('\\"textDataDeliveryEnabled\\":true');
    expect(body).toContain('\\"embeddingDataDeliveryEnabled\\":true');
    expect(body).toContain('\\"imageDataDeliveryEnabled\\":false');
  });
});
