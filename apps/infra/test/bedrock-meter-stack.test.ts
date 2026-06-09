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
      // The exact 3-region fan-out the prod stage deploys (Task 11): the home
      // region (us-east-1) is configured by the Task-12 block; the other two
      // get per-region AwsCustomResource logging configs (Task 13).
      fanoutRegions: ["us-east-1", "us-east-2", "us-west-2"],
    };

    const stack = new BedrockMeterStack(app, "BedrockMeterStack", props);
    template = Template.fromStack(stack);
  });

  test("creates the meter Lambda with ALB_DNS_NAME + DLQ_URL env", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      Runtime: "python3.12",
      Environment: {
        Variables: Match.objectLike({
          ALB_DNS_NAME: Match.anyValue(),
          // The synchronous per-record fail-safe sink the Lambda code POSTs to.
          DLQ_URL: Match.anyValue(),
        }),
      },
    });
  });

  test("creates an SQS DLQ", () => {
    template.resourceCountIs("AWS::SQS::Queue", 1);
  });

  test("the DLQ is WIRED as the Lambda async DLQ (not just present)", () => {
    // review I1: resourceCountIs alone passes even on an orphan queue. Assert
    // the function actually references the queue via DeadLetterConfig.TargetArn.
    template.hasResourceProperties("AWS::Lambda::Function", {
      DeadLetterConfig: { TargetArn: Match.anyValue() },
    });
  });

  test("the Lambda role can SendMessage to the DLQ (synchronous fail-safe path)", () => {
    // review C1: the load-bearing durability is the Lambda code's per-record
    // sqs:SendMessage to the DLQ (CW Logs invokes synchronously, so the async
    // DLQ does not cover per-record failures). Assert the grant exists.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["sqs:SendMessage"]),
            Effect: "Allow",
          }),
        ]),
      },
    });
  });

  test("alarms on DLQ depth + skip/failure metrics (review I2)", () => {
    // A silent revenue leak (DLQ'd transport/parse/truncated-output failures, or
    // unattributed/unknown-model skips) must page, not hide. One DLQ-depth
    // alarm + one alarm per skip/failure metric = 6 alarms total.
    template.resourceCountIs("AWS::CloudWatch::Alarm", 6);
    // DLQ-depth alarm.
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "ApproximateNumberOfMessagesVisible",
      Namespace: "AWS/SQS",
      ComparisonOperator: "GreaterThanThreshold",
      Threshold: 0,
    });
    // The unattributed-skip alarm (revenue-leak guard).
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "meter.unattributed",
      Namespace: "Isol8",
      ComparisonOperator: "GreaterThanThreshold",
      Threshold: 0,
    });
    // The truncated-output-body alarm (the >100KB-completion fail-safe's
    // observability — renamed from s3_merge_failed in the CW-only cutover).
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "meter.output_body_truncated",
      Namespace: "Isol8",
    });
  });

  test("Lambda SG has egress to ALB on port 80", () => {
    template.hasResourceProperties("AWS::EC2::SecurityGroupEgress", {
      FromPort: 80,
      ToPort: 80,
    });
  });

  test("creates a Bedrock invocation-log group (CloudWatch destination)", () => {
    // The Bedrock invocation-log group, by name (the Lambda log-retention
    // construct also provisions its own group, so do not count to exactly 1).
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: Match.stringLikeRegexp("/isol8/prod/bedrock-invocation"),
    });
  });

  test("CloudWatch-only: no S3 sink bucket (meter never touches S3)", () => {
    // The meter reads token + cache counts from the inline CloudWatch record;
    // the S3 sink + >100KB body-offload path were removed in the CW-only cutover.
    template.resourceCountIs("AWS::S3::Bucket", 0);
  });

  test("CloudWatch-only: the Lambda role has NO s3:GetObject grant", () => {
    // A surviving s3:GetObject grant would signal a lingering S3-fetch path.
    const policies = template.findResources("AWS::IAM::Policy");
    const json = JSON.stringify(policies);
    expect(json).not.toContain("s3:GetObject");
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

  test("logging-config custom resource is CloudWatch-only (no S3 destination)", () => {
    const customResources = template.findResources("Custom::AWS");
    const cfg = Object.values(customResources).find((r: any) =>
      JSON.stringify(r).includes("putModelInvocationLoggingConfiguration"),
    ) as any;
    expect(cfg).toBeDefined();
    // The Create payload carries CFN refs (log-group name, role ARN are
    // tokens), so CDK emits it as an `Fn::Join`, NOT a plain string. Stringify
    // the whole CR and strip the escaping so the SDK-exact nested key path can
    // be asserted as adjacent substrings. Stripping backslashes is
    // escaping-agnostic.
    const body: string = JSON.stringify(cfg).replace(/\\/g, "");
    expect(body).toContain('"loggingConfig":{"cloudWatchConfig"');
    expect(body).toContain('"textDataDeliveryEnabled":true');
    expect(body).toContain('"embeddingDataDeliveryEnabled":true');
    expect(body).toContain('"imageDataDeliveryEnabled":false');
    // CW-only: no S3 large-data offload and no S3 destination at all.
    expect(body).not.toContain("largeDataDeliveryS3Config");
    expect(body).not.toContain('"s3Config"');
  });

  test("subscription filter is scoped to the AIP-ARN shape", () => {
    template.hasResourceProperties("AWS::Logs::SubscriptionFilter", {
      FilterPattern: Match.stringLikeRegexp("application-inference-profile"),
    });
  });

  test("the meter Lambda is the subscription destination", () => {
    template.resourceCountIs("AWS::Logs::SubscriptionFilter", 1);
  });

  test("enables logging in all 3 fan-out regions", () => {
    // One AwsCustomResource logging config per fan-out region.
    const customResources = template.findResources("Custom::AWS");
    const loggingConfigs = Object.values(customResources).filter((r: any) =>
      JSON.stringify(r).includes("ModelInvocationLoggingConfiguration"),
    );
    expect(loggingConfigs.length).toBe(3);
  });

  test("each fan-out region's custom resource can iam:PassRole its logging role", () => {
    // PutModelInvocationLoggingConfiguration with a cloudWatchConfig.roleArn
    // requires iam:PassRole on that role; without it the per-region deploy
    // fails. Assert the PassRole statement exists, scoped to the per-region
    // logging-role NAME the _regionLoggingConfig roleArn references.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "iam:PassRole",
            Effect: "Allow",
            Resource: Match.stringLikeRegexp(
              "isol8-prod-bedrock-logging-us-east-2",
            ),
          }),
        ]),
      },
    });
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "iam:PassRole",
            Effect: "Allow",
            Resource: Match.stringLikeRegexp(
              "isol8-prod-bedrock-logging-us-west-2",
            ),
          }),
        ]),
      },
    });
  });

  test("GUARD: text+embedding stay enabled, image stays disabled in every config", () => {
    const customResources = template.findResources("Custom::AWS");
    const configs = Object.values(customResources).filter((r: any) =>
      JSON.stringify(r).includes("ModelInvocationLoggingConfiguration"),
    );
    expect(configs.length).toBeGreaterThanOrEqual(1);
    for (const cfg of configs) {
      // The home-region Create body is an `Fn::Join` carrying CFN refs, so its
      // inner JSON quotes are multiply-escaped; the fan-out regions are plain
      // strings. Strip backslashes so the modality assertion is escaping-
      // agnostic — a modality flip would still be caught in either form.
      const body = JSON.stringify(cfg).replace(/\\/g, "");
      expect(body).toContain('"textDataDeliveryEnabled":true');
      expect(body).toContain('"embeddingDataDeliveryEnabled":true');
      expect(body).toContain('"imageDataDeliveryEnabled":false');
    }
  });
});
