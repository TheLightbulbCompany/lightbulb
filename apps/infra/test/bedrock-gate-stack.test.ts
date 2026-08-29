import { legacyTablePrefix, tablePrefix } from "../lib/table-names";
import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import {
  BedrockGateStack,
  BedrockGateStackProps,
} from "../lib/stacks/bedrock-gate-stack";

describe("BedrockGateStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const env = { account: "877352799272", region: "us-east-1" };
    const props: BedrockGateStackProps = {
      env,
      envName: "prod",
    };
    const stack = new BedrockGateStack(app, "BedrockGateStack", props);
    template = Template.fromStack(stack);
  });

  /** The two gate Lambda resources, identified by their GATE_ENV env var (the
   *  log-retention helper fn CDK injects also uses a generic handler). */
  function gateFunctions(): any[] {
    return Object.values(
      template.findResources("AWS::Lambda::Function"),
    ).filter(
      (fn: any) => fn.Properties?.Environment?.Variables?.GATE_ENV != null,
    );
  }

  test("creates TWO gate function instances from one codebase (Node 20, arm64)", () => {
    const fns = gateFunctions();
    expect(fns.length).toBe(2);
    const envs = fns
      .map((fn: any) => fn.Properties.Environment.Variables.GATE_ENV)
      .sort();
    expect(envs).toEqual(["dev", "prod"]);
    for (const fn of fns) {
      expect(fn.Properties.Runtime).toBe("nodejs20.x");
      expect(fn.Properties.Architectures).toEqual(["arm64"]);
      expect(fn.Properties.Handler).toBe("handler.handler");
      expect(fn.Properties.MemorySize).toBe(1024);
      expect(fn.Properties.Timeout).toBe(900);
    }
  });

  test("each fn is named isol8-{env}-bedrock-gate with reserved concurrency 100", () => {
    // The function name is stable + env-specific: it is the Lambda session-name
    // segment of the gateway's assumed-role ARN that the meter's env-split
    // subscription filter keys off. Reserved concurrency 100 bounds abuse.
    for (const gateEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: `isol8-${gateEnv}-bedrock-gate`,
        ReservedConcurrentExecutions: 100,
      });
    }
  });

  test("each instance carries the EXACT env vars the handler reads", () => {
    for (const gateEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Environment: {
          Variables: Match.objectLike({
            GATE_ENV: gateEnv,
            // TOKENS_TABLE is a CFN Ref to the in-stack table (its physical
            // name is asserted on the DynamoDB resource below).
            TOKENS_TABLE: Match.anyValue(),
            BILLING_TABLE: `${tablePrefix(gateEnv)}billing-accounts`,
            FAST_GATE_TABLE: `${tablePrefix(gateEnv)}metronome-fast-gate`,
            INVOCATION_DEDUP_TABLE: `${tablePrefix(gateEnv)}bedrock-invocation-dedup`,
            // The gateway enforces the TRUE $3.00 cap (reservation bounds
            // overshoot); trial_cap.py stays 2_600_000 until Phase 5.
            CAP_MICROCENTS: "3000000",
            RESERVE_ESTIMATE_MICROCENTS: "300000",
            DEDUP_TTL_SECONDS: "1296000",
          }),
        },
      });
    }
  });

  test("does NOT set the reserved AWS_REGION env key (Lambda auto-sets it)", () => {
    for (const fn of gateFunctions()) {
      expect(
        fn.Properties.Environment.Variables.AWS_REGION,
      ).toBeUndefined();
    }
  });

  test("both Function URLs are RESPONSE_STREAM, auth NONE (bearer auth is in-app)", () => {
    template.resourceCountIs("AWS::Lambda::Url", 2);
    template.hasResourceProperties("AWS::Lambda::Url", {
      AuthType: "NONE",
      InvokeMode: "RESPONSE_STREAM",
    });
    // Both URLs must stream — no BUFFERED slipping through.
    const urls = Object.values(template.findResources("AWS::Lambda::Url"));
    for (const url of urls) {
      expect((url as any).Properties.InvokeMode).toBe("RESPONSE_STREAM");
      expect((url as any).Properties.AuthType).toBe("NONE");
    }
  });

  test("GUARD: public URL invoke needs BOTH permission statements (Oct 2025 Lambda change)", () => {
    // With only the InvokeFunctionUrl statement addFunctionUrl auto-creates,
    // every request 403s at the front door (live-verified on first deploy).
    // The second statement must be InvokedViaFunctionUrl-conditioned so the
    // public grant stays URL-only — it must NOT open direct API invoke.
    const perms = Object.values(
      template.findResources("AWS::Lambda::Permission"),
    ).map((p: any) => p.Properties);
    const urlPerms = perms.filter(
      (p: any) => p.Action === "lambda:InvokeFunctionUrl" && p.Principal === "*",
    );
    const invokePerms = perms.filter(
      (p: any) =>
        p.Action === "lambda:InvokeFunction" &&
        p.Principal === "*" &&
        p.InvokedViaFunctionUrl === true,
    );
    expect(urlPerms.length).toBe(2); // one per instance
    expect(invokePerms.length).toBe(2); // one per instance
    // No unconditioned public InvokeFunction grant may ever exist.
    const unconditioned = perms.filter(
      (p: any) =>
        p.Action === "lambda:InvokeFunction" &&
        p.Principal === "*" &&
        p.InvokedViaFunctionUrl !== true,
    );
    expect(unconditioned.length).toBe(0);
  });

  test("CfnOutputs the gate URL per env (GateUrlDev / GateUrlProd)", () => {
    const outputs = template.toJSON().Outputs ?? {};
    expect(Object.keys(outputs)).toEqual(
      expect.arrayContaining(["GateUrlDev", "GateUrlProd"]),
    );
  });

  test("publishes each env's gate URL to SSM at /lightbulb/{env}/bedrock-gate-url (backend runtime discovery + break-glass lever)", () => {
    // The backend reads this parameter at provision time — the exact name is
    // the contract. It is also the fleet break-glass lever: value "direct"
    // routes new provisions straight to bedrock-runtime.
    template.resourceCountIs("AWS::SSM::Parameter", 2);
    for (const gateEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Name: `/lightbulb/${gateEnv}/bedrock-gate-url`,
        Type: "String",
        Value: Match.anyValue(),
      });
    }
  });

  test("one gate-private token table per instance (PK token_hash, PITR, RETAIN)", () => {
    // Gate-private state — created here, NOT in the database stack. RETAIN +
    // PITR: a lost token row silently breaks an owner's inference.
    template.resourceCountIs("AWS::DynamoDB::Table", 2);
    for (const gateEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        TableName: `${tablePrefix(gateEnv)}bedrock-gate-tokens`,
        KeySchema: [{ AttributeName: "token_hash", KeyType: "HASH" }],
        BillingMode: "PAY_PER_REQUEST",
        PointInTimeRecoverySpecification: {
          PointInTimeRecoveryEnabled: true,
        },
      });
    }
    template.hasResource("AWS::DynamoDB::Table", {
      Properties: Match.objectLike({
        TableName: "lightbulb-prod-bedrock-gate-tokens",
      }),
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
    });
  });

  test("Invoke on the AIP wildcard is UNCONDITIONED (AWS denies a conditioned profile-resource invoke)", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Allow",
            Action: [
              "bedrock:InvokeModel",
              "bedrock:InvokeModelWithResponseStream",
            ],
            Resource:
              "arn:aws:bedrock:*:877352799272:application-inference-profile/*",
          }),
        ]),
      },
    });
  });

  test("raw FM Invoke is CONDITIONED on ArnLike bedrock:InferenceProfileArn (no AIP bypass)", () => {
    // The gateway can only reach a raw foundation model THROUGH an in-account
    // AIP — otherwise a request could escape per-owner metering attribution.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Allow",
            Action: [
              "bedrock:InvokeModel",
              "bedrock:InvokeModelWithResponseStream",
            ],
            Condition: {
              ArnLike: {
                "bedrock:InferenceProfileArn":
                  "arn:aws:bedrock:*:877352799272:application-inference-profile/*",
              },
            },
            Resource: Match.arrayWith([
              "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6",
            ]),
          }),
        ]),
      },
    });
  });

  test("explicit Deny bedrock:CreateInferenceProfile (no tenant profile escalation)", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Deny",
            Action: "bedrock:CreateInferenceProfile",
            Resource: "*",
          }),
        ]),
      },
    });
  });

  test("read-only GetInferenceProfile granted (trait lookup, fails soft)", () => {
    const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));
    expect(policies).toContain("bedrock:GetInferenceProfile");
  });

  test("DDB Get/Put/Update on exactly the four gate tables per env (transactions need no distinct action)", () => {
    for (const gateEnv of ["dev", "prod"]) {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Effect: "Allow",
              // trialSettle's TransactWriteItems is authorized by these
              // underlying Put/Update permissions — DynamoDB transactions have
              // no distinct dynamodb:TransactWriteItems IAM action.
              Action: [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
              ],
              Resource: Match.arrayWith([
                `arn:aws:dynamodb:us-east-1:877352799272:table/*-${gateEnv}-billing-accounts`,
                `arn:aws:dynamodb:us-east-1:877352799272:table/*-${gateEnv}-metronome-fast-gate`,
                `arn:aws:dynamodb:us-east-1:877352799272:table/*-${gateEnv}-bedrock-invocation-dedup`,
              ]),
            }),
          ]),
        },
      });
    }
  });

  test("kms grant covers Decrypt + GenerateDataKey, ViaService-confined to DynamoDB", () => {
    // billing-accounts AND metronome-fast-gate are CMK-encrypted. Decrypt reads,
    // GenerateDataKey writes — a Decrypt-only role fails every UpdateItem
    // silently in prod. ViaService confines the reach to those tables' CMKs.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ["kms:Decrypt", "kms:GenerateDataKey"],
            Condition: {
              StringEquals: {
                "kms:ViaService": ["dynamodb.us-east-1.amazonaws.com"],
              },
            },
          }),
        ]),
      },
    });
  });

  test("gateway may not widen its own IAM (no PutRolePolicy / AttachRolePolicy)", () => {
    const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));
    expect(policies).not.toContain("iam:PutRolePolicy");
    expect(policies).not.toContain("iam:AttachRolePolicy");
    expect(policies).not.toContain("iam:CreateInferenceProfile");
  });

  test("no VPC anywhere: a Function URL cannot live in a VPC", () => {
    template.resourceCountIs("AWS::EC2::SecurityGroup", 0);
    for (const fn of gateFunctions()) {
      expect(fn.Properties.VpcConfig).toBeUndefined();
    }
  });

  test("per-instance Lambda Errors + Throttles alarms, env-named, wired to a topic", () => {
    const alarms = Object.values(
      template.findResources("AWS::CloudWatch::Alarm"),
    ) as any[];
    for (const gateEnv of ["dev", "prod"]) {
      const errorsAlarm = alarms.find(
        (a: any) =>
          a.Properties.AlarmName === `isol8-${gateEnv}-bedrock-gate-fn-errors`,
      );
      expect(errorsAlarm).toBeDefined();
      expect(errorsAlarm.Properties.MetricName).toBe("Errors");
      expect(errorsAlarm.Properties.Namespace).toBe("AWS/Lambda");
      expect(errorsAlarm.Properties.Threshold).toBe(0);
      expect(errorsAlarm.Properties.ComparisonOperator).toBe(
        "GreaterThanThreshold",
      );
      expect(errorsAlarm.Properties.AlarmActions?.length).toBeGreaterThan(0);

      const throttlesAlarm = alarms.find(
        (a: any) =>
          a.Properties.AlarmName ===
          `isol8-${gateEnv}-bedrock-gate-fn-throttles`,
      );
      expect(throttlesAlarm).toBeDefined();
      expect(throttlesAlarm.Properties.MetricName).toBe("Throttles");
      expect(throttlesAlarm.Properties.Namespace).toBe("AWS/Lambda");
      expect(throttlesAlarm.Properties.AlarmActions?.length).toBeGreaterThan(0);
    }
  });

  test("EMF alarms on gate.error + gate.hold_release_failed PAGE (env-dim, gate namespace)", () => {
    // These two mean the synchronous debit/hold accounting invariant broke.
    for (const gateEnv of ["dev", "prod"]) {
      for (const metricName of ["gate.error", "gate.hold_release_failed"]) {
        template.hasResourceProperties("AWS::CloudWatch::Alarm", {
          AlarmName: `isol8-${gateEnv}-${metricName.replace(/\./g, "-")}`,
          MetricName: metricName,
          Namespace: "LightbulbAI/BedrockGate",
          Dimensions: Match.arrayWith([
            Match.objectLike({ Name: "env", Value: gateEnv }),
          ]),
          ComparisonOperator: "GreaterThanThreshold",
          Threshold: 0,
        });
      }
    }
    // Both money-safety alarms, both envs, must target the PAGE topic.
    const alarms = Object.values(
      template.findResources("AWS::CloudWatch::Alarm"),
    ) as any[];
    let paged = 0;
    for (const alarm of alarms) {
      const name: string = alarm.Properties.AlarmName;
      if (/-gate-error$|-gate-hold_release_failed$/.test(name)) {
        const gateEnv = name.startsWith("isol8-dev-") ? "dev" : "prod";
        expect(JSON.stringify(alarm.Properties.AlarmActions)).toContain(
          `lightbulb-${gateEnv}-alerts-page`,
        );
        paged++;
      }
    }
    expect(paged).toBe(4);
  });

  test("this stack mints NO SNS topic (references the env topics by ARN)", () => {
    template.resourceCountIs("AWS::SNS::Topic", 0);
  });
});
