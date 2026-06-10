import * as cdk from "aws-cdk-lib";
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
    const props: BedrockMeterStackProps = {
      env,
      envName: "prod",
    };
    const stack = new BedrockMeterStack(app, "BedrockMeterStack", props);
    template = Template.fromStack(stack);
  });

  /** The two meter Lambda resources, identified by their METER_ENV env var
   *  (the CR-provider and log-retention helper fns also use index.handler). */
  function meterFunctions(): any[] {
    return Object.values(
      template.findResources("AWS::Lambda::Function"),
    ).filter(
      (fn: any) => fn.Properties?.Environment?.Variables?.METER_ENV != null,
    );
  }

  test("creates TWO meter function instances from one codebase", () => {
    const fns = meterFunctions();
    expect(fns.length).toBe(2);
    const envs = fns
      .map((fn: any) => fn.Properties.Environment.Variables.METER_ENV)
      .sort();
    expect(envs).toEqual(["dev", "prod"]);
  });

  test("each instance is configured purely via env vars (table, DLQ, secret)", () => {
    for (const meterEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Runtime: "python3.12",
        Environment: {
          Variables: Match.objectLike({
            METER_ENV: meterEnv,
            AIP_OWNERS_TABLE: `isol8-${meterEnv}-bedrock-aip-owners`,
            DLQ_URL: Match.anyValue(),
            METRONOME_API_KEY_SECRET_ARN: Match.anyValue(),
          }),
        },
      });
    }
  });

  test("no VPC anywhere: zero security groups, no VpcConfig on either fn", () => {
    // The VPC existed only for the deleted internal-ALB hop; Metronome, DDB,
    // SQS and Secrets Manager are all public HTTPS endpoints.
    template.resourceCountIs("AWS::EC2::SecurityGroup", 0);
    for (const fn of meterFunctions()) {
      expect(fn.Properties.VpcConfig).toBeUndefined();
    }
  });

  test("no fixed function names (a fixed name would CREATE-collide on replace)", () => {
    for (const fn of meterFunctions()) {
      expect(fn.Properties.FunctionName).toBeUndefined();
    }
  });

  test("creates one SQS DLQ per instance", () => {
    template.resourceCountIs("AWS::SQS::Queue", 2);
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "isol8-dev-bedrock-meter-dlq",
    });
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "isol8-prod-bedrock-meter-dlq",
    });
  });

  test("DLQs are WIRED as each Lambda's async DLQ (not just present)", () => {
    for (const fn of meterFunctions()) {
      expect(fn.Properties.DeadLetterConfig?.TargetArn).toBeDefined();
    }
  });

  test("Lambda roles can SendMessage to a DLQ (synchronous fail-safe path)", () => {
    // The load-bearing durability is the Lambda code's per-record
    // sqs:SendMessage to its DLQ (CW Logs invokes synchronously, so the async
    // DLQ does not cover per-record failures).
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

  test("each instance can GetItem its own env's aip-owners table only", () => {
    const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));
    expect(policies).toContain("table/isol8-dev-bedrock-aip-owners");
    expect(policies).toContain("table/isol8-prod-bedrock-aip-owners");
  });

  test("kms:Decrypt is confined to SecretsManager-mediated decryption", () => {
    // The per-env secrets are CMK-encrypted with each env's auth-stack key;
    // the dev CMK ARN is unknowable from this prod-stage stack, so the grant
    // is resource-* but condition-scoped to kms:ViaService.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "kms:Decrypt",
            Condition: {
              StringEquals: {
                "kms:ViaService": "secretsmanager.us-east-1.amazonaws.com",
              },
            },
          }),
        ]),
      },
    });
  });

  test("per-env Metronome secrets referenced by name; NO bedrock_meter_token anywhere", () => {
    const all = JSON.stringify(template.toJSON());
    expect(all).toContain("isol8/dev/metronome_api_key");
    expect(all).toContain("isol8/prod/metronome_api_key");
    // The meter JWT is deleted outright — no resource may reference it.
    expect(all).not.toContain("bedrock_meter_token");
    expect(all).not.toContain("METER_TOKEN");
    expect(all).not.toContain("ALB_DNS_NAME");
  });

  test("env-neutral invocation log group, DESTROY (re-creatable) not RETAIN", () => {
    // The single group receives BOTH envs' records, so its name carries no
    // env. A RETAIN policy on a fixed-name group orphans it on
    // rollback/delete, and the orphan then blocks every subsequent CREATE
    // with AlreadyExists (this bricked two prod deploys).
    template.hasResource("AWS::Logs::LogGroup", {
      Properties: Match.objectLike({
        LogGroupName: "/isol8/bedrock-invocation",
      }),
      DeletionPolicy: "Delete",
      UpdateReplacePolicy: "Delete",
    });
  });

  test("TWO subscription filters split the envs by identity.arn role marker", () => {
    template.resourceCountIs("AWS::Logs::SubscriptionFilter", 2);
    template.hasResourceProperties("AWS::Logs::SubscriptionFilter", {
      FilterPattern:
        '"application-inference-profile" "isol8-dev-owner-bedrock"',
    });
    template.hasResourceProperties("AWS::Logs::SubscriptionFilter", {
      FilterPattern:
        '"application-inference-profile" "isol8-prod-owner-bedrock"',
    });
  });

  test("CloudWatch-only: no S3 bucket, no s3 grants", () => {
    template.resourceCountIs("AWS::S3::Bucket", 0);
    const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));
    expect(policies).not.toContain("s3:GetObject");
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

  test("home-region only: exactly one Bedrock logging config", () => {
    // Bedrock writes invocation logs only in the SOURCE region (the region of
    // the bedrock-runtime endpoint the caller hits). Every container pins
    // baseUrl to bedrock-runtime.us-east-1, so all logs land here — exactly
    // ONE putModelInvocationLoggingConfiguration CR, no cross-region copies.
    const customResources = template.findResources("Custom::AWS");
    const loggingConfigs = Object.values(customResources).filter((r: any) =>
      JSON.stringify(r).includes("ModelInvocationLoggingConfiguration"),
    );
    expect(loggingConfigs.length).toBe(1);
  });

  test("GUARD: text+embedding stay enabled, image stays disabled in the config", () => {
    const customResources = template.findResources("Custom::AWS");
    const configs = Object.values(customResources).filter((r: any) =>
      JSON.stringify(r).includes("ModelInvocationLoggingConfiguration"),
    );
    expect(configs.length).toBeGreaterThanOrEqual(1);
    for (const cfg of configs) {
      const body = JSON.stringify(cfg).replace(/\\/g, "");
      expect(body).toContain('"textDataDeliveryEnabled":true');
      expect(body).toContain('"embeddingDataDeliveryEnabled":true');
      expect(body).toContain('"imageDataDeliveryEnabled":false');
    }
  });

  test("alarms per instance: DLQ depth + 4 skip metrics, env-scoped dimensions", () => {
    // (1 DLQ + 4 skip-metric) alarms x 2 instances = 10. Each skip-metric
    // alarm scopes to its instance via the env dimension so dev noise never
    // pages prod (both Lambdas share namespace Isol8 + the service name).
    template.resourceCountIs("AWS::CloudWatch::Alarm", 10);
    for (const meterEnv of ["dev", "prod"]) {
      for (const metricName of [
        "meter.unattributed",
        "meter.unknown_model",
        "meter.transport_error",
        "meter.parse_failed",
      ]) {
        template.hasResourceProperties("AWS::CloudWatch::Alarm", {
          AlarmName: `isol8-${meterEnv}-${metricName.replace(/\./g, "-")}`,
          MetricName: metricName,
          Namespace: "Isol8",
          Dimensions: Match.arrayWith([
            Match.objectLike({ Name: "env", Value: meterEnv }),
            Match.objectLike({ Name: "service", Value: "isol8-bedrock-meter" }),
          ]),
          ComparisonOperator: "GreaterThanThreshold",
          Threshold: 0,
        });
      }
      template.hasResourceProperties("AWS::CloudWatch::Alarm", {
        AlarmName: `isol8-${meterEnv}-bedrock-meter-dlq-not-empty`,
        MetricName: "ApproximateNumberOfMessagesVisible",
        Namespace: "AWS/SQS",
      });
    }
    // The truncation path is gone — no truncated-output alarm may survive.
    const alarms = JSON.stringify(template.findResources("AWS::CloudWatch::Alarm"));
    expect(alarms).not.toContain("output_body_truncated");
  });

  test("alarms page to each env's OWN alerts-warn topic; no topics minted", () => {
    // An alarm with no action only flips a console dot. Each instance's
    // alarms must target its env's existing isol8-{env}-alerts-warn topic so
    // dev pages dev and prod pages prod — and this stack mints NO new topic.
    template.resourceCountIs("AWS::SNS::Topic", 0);
    const alarms = Object.values(
      template.findResources("AWS::CloudWatch::Alarm"),
    ) as any[];
    for (const alarm of alarms) {
      expect(alarm.Properties.AlarmActions?.length).toBeGreaterThan(0);
      const name: string = alarm.Properties.AlarmName;
      const expectedEnv = name.startsWith("isol8-dev-") ? "dev" : "prod";
      expect(JSON.stringify(alarm.Properties.AlarmActions)).toContain(
        `isol8-${expectedEnv}-alerts-warn`,
      );
    }
  });
});
