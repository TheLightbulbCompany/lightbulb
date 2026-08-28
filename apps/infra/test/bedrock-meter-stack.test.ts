import { legacyTablePrefix, tablePrefix } from "../lib/table-names";
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

  test("each instance is configured purely via env vars (tables, DLQ, secret, cluster)", () => {
    for (const meterEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Runtime: "python3.12",
        Environment: {
          Variables: Match.objectLike({
            METER_ENV: meterEnv,
            AIP_OWNERS_TABLE: `isol8-${meterEnv}-bedrock-aip-owners`,
            BILLING_TABLE: `isol8-${meterEnv}-billing-accounts`,
            CONTAINERS_TABLE: `isol8-${meterEnv}-containers`,
            GATE_TOKENS_TABLE: `isol8-${meterEnv}-bedrock-gate-tokens`,
            ECS_CLUSTER_PREFIX: `isol8-${meterEnv}-container-`,
            INVOCATION_DEDUP_TABLE: Match.anyValue(),
            DLQ_URL: Match.anyValue(),
            METRONOME_API_KEY_SECRET_ARN: Match.anyValue(),
          }),
        },
      });
    }
  });

  test("one per-requestId dedup table per instance, TTL'd (the replay guard)", () => {
    // A DynamoDB ADD has no dedup. CW Logs retries whole batches and DLQ
    // redrives replay them, so an unguarded counter double-counts and
    // false-locks-out real users — this table is what makes the counter
    // at-most-once.
    template.resourceCountIs("AWS::DynamoDB::Table", 2);
    for (const meterEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        TableName: `${legacyTablePrefix(meterEnv)}bedrock-invocation-dedup`,
        KeySchema: [{ AttributeName: "request_id", KeyType: "HASH" }],
        BillingMode: "PAY_PER_REQUEST",
        TimeToLiveSpecification: { AttributeName: "ttl", Enabled: true },
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
    // Both prefixes, both envs: the legacy entries are what keep the meter
    // working against the tables it still reads until the reader flip.
    expect(policies).toContain("table/lightbulb-dev-bedrock-aip-owners");
    expect(policies).toContain("table/lightbulb-prod-bedrock-aip-owners");
    expect(policies).toContain("table/isol8-dev-bedrock-aip-owners");
    expect(policies).toContain("table/isol8-prod-bedrock-aip-owners");
  });

  test("kms grant covers BOTH Decrypt and GenerateDataKey, ViaService-confined", () => {
    // The per-env secrets AND the aip-owners/billing/containers tables are
    // CMK-encrypted with each env's auth-stack key; the dev CMK ARN is
    // unknowable from this prod-stage stack, so the grant is resource-* but
    // condition-scoped to kms:ViaService. DynamoDB MUST be in the list —
    // without it the attribution GetItem dies with KMS AccessDenied
    // (live-verified).
    //
    // GenerateDataKey is what makes the WRITES work. A Decrypt-only role
    // GetItems happily and fails every UpdateItem — the trial counter would
    // pass every test and fail only in the deployed environment.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ["kms:Decrypt", "kms:GenerateDataKey"],
            Condition: {
              StringEquals: {
                "kms:ViaService": [
                  "secretsmanager.us-east-1.amazonaws.com",
                  "dynamodb.us-east-1.amazonaws.com",
                ],
              },
            },
          }),
        ]),
      },
    });
  });

  test("each instance can write ONLY its own env's counter + revoked flag", () => {
    // UpdateItem on the env's billing-accounts (the daily counter) and
    // containers (the bedrock_revoked flag the daily restore reads) — and
    // nothing else. No cross-env reach, no PutItem/DeleteItem on either.
    for (const meterEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "dynamodb:UpdateItem",
              Effect: "Allow",
              // Spans BOTH prefixes during the table rename: these grants are
              // literal ARNs, so unlike grantReadWriteData they do NOT follow a
              // tableName change. Dropping the legacy half here would AccessDeny
              // the meter against the tables it is still reading.
              Resource: [
                `arn:aws:dynamodb:us-east-1:877352799272:table/lightbulb-${meterEnv}-billing-accounts`,
                `arn:aws:dynamodb:us-east-1:877352799272:table/isol8-${meterEnv}-billing-accounts`,
                `arn:aws:dynamodb:us-east-1:877352799272:table/lightbulb-${meterEnv}-containers`,
                `arn:aws:dynamodb:us-east-1:877352799272:table/isol8-${meterEnv}-containers`,
              ],
            }),
          ]),
        },
      });
    }
  });

  test("the dedup table grant is PutItem only (a claim must not be rollback-able)", () => {
    const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));
    expect(policies).toContain("dynamodb:PutItem");
    expect(policies).not.toContain("dynamodb:DeleteItem");
    expect(policies).not.toContain("dynamodb:BatchWriteItem");
  });

  test("THE KILL: iam:DeleteRolePolicy scoped to the per-owner role prefix", () => {
    // The container holds its own AWS credentials and calls Bedrock directly,
    // so deleting the per-owner role's scoped inline policy is the only thing
    // that stops a free owner's spend. Scoped to the owner-role name prefix:
    // this Lambda can strip Bedrock from a per-owner task role and NOTHING
    // else (never the backend's role, never its own).
    for (const meterEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "iam:DeleteRolePolicy",
              Effect: "Allow",
              Resource: `arn:aws:iam::877352799272:role/isol8-${meterEnv}-owner-bedrock-*`,
            }),
          ]),
        },
      });
    }
    const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));
    // Revoke only — the meter must never be able to mint or widen a policy.
    expect(policies).not.toContain("iam:PutRolePolicy");
    expect(policies).not.toContain("iam:AttachRolePolicy");
  });

  test("THE KILL: ecs:UpdateService scoped to the env's per-user services", () => {
    // The IAM revoke is ~5-15s eventually consistent and does not kill an
    // already-admitted streaming call — desiredCount=0 is the instant stop.
    // Scoped to the env's cluster + the per-user openclaw-* service name, so
    // the meter can never touch the backend's own service.
    for (const meterEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "ecs:UpdateService",
              Effect: "Allow",
              Resource: `arn:aws:ecs:us-east-1:877352799272:service/isol8-${meterEnv}-container-*/openclaw-*`,
            }),
          ]),
        },
      });
    }
    const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));
    expect(policies).toContain("ecs:ListClusters");
    // No destructive ECS verbs: the kill scales to zero, it never deletes.
    expect(policies).not.toContain("ecs:DeleteService");
    expect(policies).not.toContain("ecs:StopTask");
  });

  test("THE KILL: deny-flag half — GetItem/UpdateItem on containers + gate-tokens", () => {
    // The third kill step flips denied=true on the owner's bedrock-gate token
    // row (the gateway's kill switch). It reads the containers row for the
    // rotation pointer (bedrock_gate_token_hash) then writes the gate-tokens
    // row — GetItem + UpdateItem, scoped to the env's two tables. The
    // gate-tokens table is owned by the bedrock-gate stack (ARN-by-convention).
    for (const meterEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: ["dynamodb:GetItem", "dynamodb:UpdateItem"],
              Effect: "Allow",
              // Spans BOTH prefixes during the table rename: these grants are
              // literal ARNs, so unlike grantReadWriteData they do NOT follow a
              // tableName change. Dropping the legacy half here would AccessDeny
              // the meter against the tables it is still reading.
              Resource: [
                `arn:aws:dynamodb:us-east-1:877352799272:table/lightbulb-${meterEnv}-containers`,
                `arn:aws:dynamodb:us-east-1:877352799272:table/isol8-${meterEnv}-containers`,
                `arn:aws:dynamodb:us-east-1:877352799272:table/lightbulb-${meterEnv}-bedrock-gate-tokens`,
                `arn:aws:dynamodb:us-east-1:877352799272:table/isol8-${meterEnv}-bedrock-gate-tokens`,
              ],
            }),
          ]),
        },
      });
    }
    // The gate-tokens table lives in the gate stack — this stack only names it,
    // never creates it (still exactly the two dedup tables).
    template.resourceCountIs("AWS::DynamoDB::Table", 2);
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
    // Each filter OR's the per-owner role AND the gateway role for its env
    // (both carry the env segment, so the dev/prod split stays exact). The `?`
    // prefixes make the terms optional (OR); the old required
    // "application-inference-profile" term is dropped because CloudWatch
    // ignores `?` whenever any required term is present in the same pattern.
    template.resourceCountIs("AWS::Logs::SubscriptionFilter", 2);
    template.hasResourceProperties("AWS::Logs::SubscriptionFilter", {
      FilterPattern:
        '?"isol8-dev-owner-bedrock" ?"isol8-dev-bedrock-gate"',
    });
    template.hasResourceProperties("AWS::Logs::SubscriptionFilter", {
      FilterPattern:
        '?"isol8-prod-owner-bedrock" ?"isol8-prod-bedrock-gate"',
    });
  });

  test("CloudWatch-only: no S3 bucket, no s3 grants", () => {
    template.resourceCountIs("AWS::S3::Bucket", 0);
    const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));
    expect(policies).not.toContain("s3:GetObject");
  });

  test("AwsCustomResource sets logging config with every data modality named", () => {
    // The custom resource encodes the putModelInvocationLoggingConfiguration
    // call (incl. every modality flag) in its Create parameters. CDK serializes
    // the SDK action with its lowercase initial (`put...`); the capital-P
    // spelling only appears in the IAM policy (`bedrock:Put...`), not the CR body.
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

  test("GUARD: text delivery stays ENABLED — disabling it blinds the meter", () => {
    // The modality flags select WHICH MODELS get logged AT ALL, not whether
    // bodies are included ("Data will be logged for all models that support
    // the modalities ... that you choose" — Bedrock user guide). Deselecting
    // Text stops logging Claude/Converse invocations entirely: the meter goes
    // blind, the trial cap never fires, paid usage stops ingesting. The
    // credential exposure from the bodies this forces into the log group is
    // bounded by short retention (asserted below), NOT by disabling delivery.
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

  test("GUARD: invocation log group keeps SHORT retention (credential-bearing bodies)", () => {
    // Bodies carry whatever the agent had in context — live records were
    // observed containing a tenant's Paperclip API key and gateway JWT in
    // plaintext — and text delivery cannot be turned off (above). The meter
    // consumes each record within seconds and DLQ messages embed their own
    // copy, so retention is purely a debugging window. Keep it days, not
    // months.
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: "/isol8/bedrock-invocation",
      RetentionInDays: 3,
    });
  });

  test("alarms per instance: DLQ depth + 10 skip/failure metrics + fn errors, env-scoped dimensions", () => {
    // (1 DLQ + 10 skip/failure-metric + 1 fn-Errors) alarms x 2 instances = 24.
    // Each metric alarm scopes to its instance via the env dimension so dev
    // noise never pages prod (both Lambdas share namespace Isol8 + the
    // service name).
    //
    // The kill_* alarms are the most important ones in the stack: they mean
    // the cap fired and the owner is STILL able to spend. meter.cap_kill and
    // meter.replay_skipped are deliberately NOT alarmed — a trial owner
    // hitting their cap and a CW-Logs batch retry are normal operation.
    template.resourceCountIs("AWS::CloudWatch::Alarm", 24);
    for (const meterEnv of ["dev", "prod"]) {
      for (const metricName of [
        "meter.unattributed",
        "meter.unknown_model",
        "meter.transport_error",
        "meter.parse_failed",
        "meter.counter_error",
        "meter.kill_iam_error",
        "meter.kill_ecs_error",
        "meter.kill_stamp_error",
        "meter.kill_deny_token_error",
        "meter.malformed_record",
      ]) {
        template.hasResourceProperties("AWS::CloudWatch::Alarm", {
          AlarmName: `isol8-${meterEnv}-${metricName.replace(/\./g, "-")}`,
          MetricName: metricName,
          Namespace: "LightbulbAI",
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

  test("per-instance Lambda Errors alarm catches whole-batch failures", () => {
    // A whole-batch failure (cold-start import error, timeout,
    // transport-retry exhaustion) raises out of the handler and never reaches
    // the per-record DLQ path — the function's Errors metric is the only
    // signal, so each instance must alarm on it and the alarm must page.
    const alarms = Object.values(
      template.findResources("AWS::CloudWatch::Alarm"),
    ) as any[];
    for (const meterEnv of ["dev", "prod"]) {
      const errorsAlarm = alarms.find(
        (a: any) =>
          a.Properties.AlarmName ===
          `isol8-${meterEnv}-bedrock-meter-fn-errors`,
      );
      expect(errorsAlarm).toBeDefined();
      expect(errorsAlarm.Properties.MetricName).toBe("Errors");
      expect(errorsAlarm.Properties.Namespace).toBe("AWS/Lambda");
      expect(errorsAlarm.Properties.Statistic).toBe("Sum");
      expect(errorsAlarm.Properties.Threshold).toBe(0);
      expect(errorsAlarm.Properties.ComparisonOperator).toBe(
        "GreaterThanThreshold",
      );
      expect(errorsAlarm.Properties.TreatMissingData).toBe("notBreaching");
      expect(errorsAlarm.Properties.AlarmActions?.length).toBeGreaterThan(0);
    }
  });

  test("logging-config CR depends on BOTH subscription filters (cutover ordering)", () => {
    // When the log group is replaced, the CR repoints Bedrock at the new
    // group. Records delivered before the new group's filters exist are
    // unrecoverable (filters only forward events ingested after creation), so
    // the repoint must be ordered AFTER both filters.
    const json = template.toJSON();
    const filterIds = Object.entries(json.Resources)
      .filter(([, r]: [string, any]) => r.Type === "AWS::Logs::SubscriptionFilter")
      .map(([id]) => id);
    expect(filterIds.length).toBe(2);

    const crEntry = Object.entries(json.Resources).find(
      ([, r]: [string, any]) =>
        r.Type === "Custom::AWS" &&
        JSON.stringify(r).includes("putModelInvocationLoggingConfiguration"),
    ) as [string, any];
    expect(crEntry).toBeDefined();
    const dependsOn: string[] = crEntry[1].DependsOn ?? [];
    for (const filterId of filterIds) {
      expect(dependsOn).toContain(filterId);
    }
  });

  test("kill-failure alarms PAGE; the rest warn; each to its OWN env topic; no topics minted", () => {
    // An alarm with no action only flips a console dot. Each instance's alarms
    // must target its own env's topic so dev pages dev and prod pages prod —
    // and this stack mints NO new topic.
    //
    // THE ROUTING SPLIT IS A SAFETY PROPERTY, not a preference. A
    // meter.kill_*_error means the cap FIRED and the owner is STILL ABLE TO
    // SPEND — the one failure here that burns money in real time. alerts-warn
    // is email-only to alerts@; that is not a response to unbounded spend. So
    // the two kill actions go to alerts-page (SMS + oncall@). Everything else
    // is a revenue-leak / hygiene signal and warns.
    template.resourceCountIs("AWS::SNS::Topic", 0);
    // Alarm names replace only the DOTS in the metric name, so the underscores
    // survive: meter.kill_iam_error -> isol8-{env}-meter-kill_iam_error.
    const PAGING = ["meter-kill_iam_error", "meter-kill_ecs_error", "meter-kill_deny_token_error"];
    const alarms = Object.values(
      template.findResources("AWS::CloudWatch::Alarm"),
    ) as any[];
    let paged = 0;
    for (const alarm of alarms) {
      expect(alarm.Properties.AlarmActions?.length).toBeGreaterThan(0);
      const name: string = alarm.Properties.AlarmName;
      const expectedEnv = name.startsWith("isol8-dev-") ? "dev" : "prod";
      const pages = PAGING.some((m) => name.endsWith(m));
      const tier = pages ? "page" : "warn";
      if (pages) paged++;
      expect(JSON.stringify(alarm.Properties.AlarmActions)).toContain(
        `isol8-${expectedEnv}-alerts-${tier}`,
      );
    }
    // All three kill-failure alarms (iam, ecs, deny-token), both envs. If this
    // drops, a failed kill went silent again.
    expect(paged).toBe(6);
  });
});
