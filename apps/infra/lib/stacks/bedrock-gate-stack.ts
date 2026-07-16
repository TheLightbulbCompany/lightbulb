import * as path from "path";
import { execSync } from "child_process";
import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as sns from "aws-cdk-lib/aws-sns";
import { Construct } from "constructs";

// =============================================================================
// BedrockGateStack — account-scoped synchronous Bedrock metering gateway. ONE
// codebase -> TWO function instances (dev + prod), mirroring BedrockMeterStack.
// Each is a streaming Node Lambda fronting bedrock-runtime: bearer auth ->
// pre-flight budget reservation -> maxTokens ceiling clamp -> SigV4 re-sign to
// the owner's OWN application inference profile -> verbatim eventstream
// passthrough -> synchronous debit deduped on Bedrock's requestId.
// =============================================================================
// Instantiated ONCE (prod stage only), exactly like BedrockMeterStack: this is
// account-scoped plumbing (both env instances live in one stack; the dev/prod
// split is pure env-var configuration), so a per-env duplicate would collide on
// the fixed function/table names. No VPC — a Lambda Function URL cannot live in
// a VPC, so the endpoint is public and token-gated (bearer auth is in-app; the
// AWS-provided scheme for fronting Bedrock, strictly narrower than the STS creds
// containers hold today). Reserved concurrency bounds abuse cost.
// =============================================================================

export interface BedrockGateStackProps extends cdk.StackProps {
  /** Stage env name ("prod") — tagging only; both gate instances are fixed. */
  envName: string;
}

// The 3 foundation-model ids x 3 fan-out regions = the 9 FM ARNs the gateway
// role may reach — but ONLY via an in-account application inference profile
// (the ArnLike condition below). Kept in lockstep with container-stack.ts's
// per-owner boundary (FAN_OUT_REGIONS / FM_IDS); a raw-FM invoke that bypassed
// the AIP would escape per-owner metering attribution.
const FAN_OUT_REGIONS = ["us-east-1", "us-east-2", "us-west-2"];
const FM_IDS = [
  "anthropic.claude-opus-4-6-v1",
  "anthropic.claude-sonnet-4-6",
  "anthropic.claude-haiku-4-5-20251001-v1:0",
];

export class BedrockGateStack extends cdk.Stack {
  public readonly gateFnDev: lambda.Function;
  public readonly gateFnProd: lambda.Function;

  constructor(scope: Construct, id: string, props: BedrockGateStackProps) {
    super(scope, id, props);

    // ONE code asset shared by both instances. The asset dir gitignores
    // node_modules, so `fromAsset` alone ships no deps — bundle @smithy/@aws-sdk
    // at synth time. `local.tryBundle` runs a host-side `npm ci` first (fast, no
    // Docker); CI without a local toolchain falls back to the Node 20 bundling
    // image. Both use the committed package-lock.json for a reproducible tree.
    const gateSrcDir = path.join(
      __dirname,
      "..",
      "..",
      "lambda",
      "bedrock-gate",
    );
    const gateCode = lambda.Code.fromAsset(gateSrcDir, {
      bundling: {
        image: lambda.Runtime.NODEJS_20_X.bundlingImage,
        command: [
          "bash",
          "-c",
          [
            "mkdir -p /asset-output",
            "cp package.json package-lock.json /asset-output/",
            "cp *.mjs /asset-output/",
            "cd /asset-output",
            "npm ci --omit=dev --no-audit --no-fund",
          ].join(" && "),
        ],
        local: {
          tryBundle(outputDir: string): boolean {
            try {
              for (const f of fs.readdirSync(gateSrcDir)) {
                if (
                  f.endsWith(".mjs") ||
                  f === "package.json" ||
                  f === "package-lock.json"
                ) {
                  fs.copyFileSync(
                    path.join(gateSrcDir, f),
                    path.join(outputDir, f),
                  );
                }
              }
              execSync("npm ci --omit=dev --no-audit --no-fund", {
                cwd: outputDir,
                stdio: "inherit",
              });
              return true;
            } catch {
              return false;
            }
          },
        },
      },
    });

    const fns: Record<string, lambda.Function> = {};

    for (const gateEnv of ["dev", "prod"] as const) {
      // Prod keeps the unsuffixed construct ids (the account-scoped-stack
      // convention shared with BedrockMeterStack); dev gets "Dev"-suffixed ids.
      const idSuffix = gateEnv === "prod" ? "" : "Dev";
      // CfnOutput logical ids are explicitly Dev/Prod per the plan.
      const outSuffix = gateEnv === "prod" ? "Prod" : "Dev";

      // Gate-private state: the bearer-token table. Holds only SHA-256(token) +
      // owner_id + a denied flag + created_at — never the plaintext token — so
      // AWS-owned encryption is sufficient (no cross-stage CMK to reference).
      // RETAIN + PITR: a lost token row silently breaks an owner's inference.
      const tokensTable = new dynamodb.Table(this, `GateTokensTable${idSuffix}`, {
        tableName: `isol8-${gateEnv}-bedrock-gate-tokens`,
        partitionKey: {
          name: "token_hash",
          type: dynamodb.AttributeType.STRING,
        },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
        pointInTimeRecovery: true,
        // On rotation the superseded token row is grace-expired (a short ttl)
        // rather than deleted, so the still-running task keeps serving through a
        // reprovision roll; DynamoDB reaps it after. Current tokens carry no ttl.
        timeToLiveAttribute: "ttl",
      });

      // The function name is stable and env-specific: it also becomes the
      // Lambda session-name segment of the gateway's assumed-role ARN in the
      // Bedrock invocation log, which the meter's env-split subscription filter
      // keys off (`?"isol8-{env}-bedrock-gate"`). No CREATE-collision risk: this
      // is a greenfield stack and the name never changes.
      const fn = new lambda.Function(this, `GateFn${idSuffix}`, {
        functionName: `isol8-${gateEnv}-bedrock-gate`,
        runtime: lambda.Runtime.NODEJS_20_X,
        architecture: lambda.Architecture.ARM_64,
        handler: "handler.handler",
        code: gateCode,
        memorySize: 1024,
        timeout: cdk.Duration.seconds(900),
        // Bounds blast radius: a runaway cannot exhaust account concurrency,
        // and the throttle alarm below fires if it tries.
        reservedConcurrentExecutions: 100,
        environment: {
          // The env dimension on the gate's EMF metrics (namespace
          // Isol8/BedrockGate) and the env segment the alarms key off.
          GATE_ENV: gateEnv,
          // Bearer-token table (this stack) — SHA-256(token) -> owner_id/denied.
          TOKENS_TABLE: tokensTable.tableName,
          // Plan + trial daily-spend/reserved counters (billing-accounts) and
          // the paid credit balance projection (metronome-fast-gate).
          BILLING_TABLE: `isol8-${gateEnv}-billing-accounts`,
          FAST_GATE_TABLE: `isol8-${gateEnv}-metronome-fast-gate`,
          // Shared per-requestId dedup with the meter (source attr distinguishes
          // gateway vs meter claims) — counts each invocation exactly once.
          INVOCATION_DEDUP_TABLE: `isol8-${gateEnv}-bedrock-invocation-dedup`,
          // The gateway enforces the TRUE $3.00 cap: the reservation hold bounds
          // overshoot, so it does NOT use the async $2.60 enforce-early margin.
          // NOTE: trial_cap.py (and the meter's index.py) stay 2_600_000 until
          // Phase 5 (Task 10) — do NOT move only one of the lockstep copies.
          CAP_MICROCENTS: "3000000",
          // Realistic p95 per-turn hold (~$0.30), NOT the 128K worst case:
          // worst-case reserves would serialize a trial owner's own subagents.
          RESERVE_ESTIMATE_MICROCENTS: "300000",
          // Dedup claim TTL: 15 days, longer than any DLQ redrive window.
          DEDUP_TTL_SECONDS: "1296000",
          // AWS_REGION is auto-set by the Lambda runtime (reserved key).
        },
        logRetention: logs.RetentionDays.ONE_MONTH,
      });
      fns[gateEnv] = fn;

      // Streaming Function URL. RESPONSE_STREAM so the eventstream body pipes to
      // the container as Bedrock emits it; auth NONE because bearer auth is
      // enforced in-app (the token table lookup), not by IAM.
      const fnUrl = fn.addFunctionUrl({
        authType: lambda.FunctionUrlAuthType.NONE,
        invokeMode: lambda.InvokeMode.RESPONSE_STREAM,
      });
      new cdk.CfnOutput(this, `GateUrl${outSuffix}`, {
        value: fnUrl.url,
        description: `bedrock-gate Function URL (${gateEnv})`,
      });

      // --- Gateway IAM: invoke the owner's OWN AIP, nothing raw ----------------
      const foundationModelArns = FAN_OUT_REGIONS.flatMap((region) =>
        FM_IDS.map((model) => `arn:aws:bedrock:${region}::foundation-model/${model}`),
      );
      // Every in-account application inference profile — the gateway fronts EVERY
      // owner, so it cannot enumerate specific AIP ARNs at deploy time; per-owner
      // isolation is enforced in-app (assertOwnAip) against the token's owner_id.
      const aipWildcard = `arn:aws:bedrock:*:${this.account}:application-inference-profile/*`;

      // The AIP RESOURCE itself is allowed UNCONDITIONALLY: AWS does not populate
      // bedrock:InferenceProfileArn when authorizing the profile resource, so
      // conditioning this statement denies the invoke outright (verified in
      // container-stack.ts / bedrock_aip.py).
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "GatewayInvokeProfile",
          effect: iam.Effect.ALLOW,
          actions: [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
          ],
          resources: [aipWildcard],
        }),
      );
      // The raw foundation-model ARNs are reachable ONLY THROUGH an in-account
      // AIP — same conditioned shape as the per-owner boundary in
      // container-stack.ts:285-338 / bedrock_aip.build_owner_bedrock_policy, so
      // the gateway can never bypass a profile and escape metering attribution.
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "GatewayInvokeFmViaProfile",
          effect: iam.Effect.ALLOW,
          actions: [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
          ],
          resources: foundationModelArns,
          conditions: {
            ArnLike: {
              "bedrock:InferenceProfileArn": aipWildcard,
            },
          },
        }),
      );
      // Control-plane trait lookup (read-only, fails soft).
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "GatewayGetInferenceProfile",
          effect: iam.Effect.ALLOW,
          actions: ["bedrock:GetInferenceProfile"],
          resources: [aipWildcard],
        }),
      );
      // Never let the gateway mint a wider profile to escape its scope.
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "GatewayDenyCreateProfile",
          effect: iam.Effect.DENY,
          actions: ["bedrock:CreateInferenceProfile"],
          resources: ["*"],
        }),
      );

      // --- DynamoDB: the four gate tables (by ARN convention, same as meter) ---
      const ddbArn = (name: string) =>
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${name}`;
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "GateTablesReadWrite",
          effect: iam.Effect.ALLOW,
          // trialSettle's atomic claim+debit+release uses TransactWriteItems,
          // but DynamoDB transactions carry NO distinct IAM action — they are
          // authorized by the UNDERLYING item permissions (PutItem for the
          // conditional dedup claim, UpdateItem for the billing write), both of
          // which are granted here. (AWS: "Permissions for Put, Update, Delete
          // ... are governed by the permissions used for the underlying
          // PutItem, UpdateItem, DeleteItem ... operations." — transaction-apis-iam.)
          actions: [
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
          ],
          resources: [
            tokensTable.tableArn,
            ddbArn(`isol8-${gateEnv}-billing-accounts`),
            ddbArn(`isol8-${gateEnv}-metronome-fast-gate`),
            ddbArn(`isol8-${gateEnv}-bedrock-invocation-dedup`),
          ],
        }),
      );

      // billing-accounts AND metronome-fast-gate are CMK-encrypted (per-env
      // auth-stack key). DynamoDB mediates kms:Decrypt to READ and
      // kms:GenerateDataKey to WRITE — a Decrypt-only role GetItems happily and
      // fails EVERY UpdateItem (the exact silent-in-prod trap the meter stack
      // documents). Confined by kms:ViaService to DynamoDB in this region, so
      // the effective reach is just those tables' CMKs. The dev CMK ARN is
      // unknowable from this prod-stage stack (cross-stage), hence resource-*.
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "KmsDecryptViaDynamoDb",
          effect: iam.Effect.ALLOW,
          actions: ["kms:Decrypt", "kms:GenerateDataKey"],
          resources: ["*"],
          conditions: {
            StringEquals: {
              "kms:ViaService": [`dynamodb.${this.region}.amazonaws.com`],
            },
          },
        }),
      );

      // --- Alarms (per instance) ----------------------------------------------
      // Errors/Throttles warn; the two money-safety EMF metrics page. Same
      // per-env topic lookup mechanism as the meter (ARN reference, no export
      // coupling, mints no topic).
      const alarmTopic = sns.Topic.fromTopicArn(
        this,
        `GateAlerts${idSuffix}`,
        `arn:aws:sns:${this.region}:${this.account}:isol8-${gateEnv}-alerts-warn`,
      );
      const alarmAction = new cloudwatchActions.SnsAction(alarmTopic);

      const pageTopic = sns.Topic.fromTopicArn(
        this,
        `GatePage${idSuffix}`,
        `arn:aws:sns:${this.region}:${this.account}:isol8-${gateEnv}-alerts-page`,
      );
      const pageAction = new cloudwatchActions.SnsAction(pageTopic);

      // A gate Lambda erroring means cut-over owners get Bedrock 5xx — no
      // corruption, but chat breaks. The Errors metric is the only signal for a
      // whole-invocation failure (cold-start import, timeout, uncaught throw).
      const errorsAlarm = new cloudwatch.Alarm(this, `GateFnErrorsAlarm${idSuffix}`, {
        alarmName: `isol8-${gateEnv}-bedrock-gate-fn-errors`,
        alarmDescription:
          "Bedrock gate Lambda invocation errored - cut-over owners are seeing Bedrock 5xx (cold-start import error, timeout, or uncaught throw). Chat breaks for gated owners; investigate or break-glass to direct-invoke.",
        metric: fn.metricErrors({
          period: cdk.Duration.minutes(5),
          statistic: "Sum",
        }),
        threshold: 0,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      errorsAlarm.addAlarmAction(alarmAction);

      // Reserved concurrency (100) is a hard ceiling; sustained throttles mean
      // legitimate traffic is being turned away as Bedrock 429.
      const throttlesAlarm = new cloudwatch.Alarm(this, `GateFnThrottlesAlarm${idSuffix}`, {
        alarmName: `isol8-${gateEnv}-bedrock-gate-fn-throttles`,
        alarmDescription:
          "Bedrock gate Lambda is throttling - reserved concurrency (100) is exhausted and legitimate inference is being turned away. Investigate a runaway or raise the reservation.",
        metric: fn.metricThrottles({
          period: cdk.Duration.minutes(5),
          statistic: "Sum",
        }),
        threshold: 0,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      throttlesAlarm.addAlarmAction(alarmAction);

      // The two EMF metrics that mean the accounting invariant broke:
      //  - gate.error: the catch-all fired (a request threw past the handler);
      //  - gate.hold_release_failed: a reservation hold could not be released,
      //    so it sits until TTL — repeated failures silently shrink an owner's
      //    admittable budget. Both burn correctness/money, so both PAGE.
      // EMF auto-publishes these into namespace Isol8/BedrockGate with a single
      // `env` dimension (metrics.mjs), so the alarm keys on env alone.
      const pagingMetrics: Array<[string, string]> = [
        ["gate.error", "GateErrorAlarm"],
        ["gate.hold_release_failed", "GateHoldReleaseFailedAlarm"],
      ];
      for (const [metricName, alarmId] of pagingMetrics) {
        const alarm = new cloudwatch.Alarm(this, `${alarmId}${idSuffix}`, {
          alarmName: `isol8-${gateEnv}-${metricName.replace(/\./g, "-")}`,
          alarmDescription: `Bedrock gate (${gateEnv}) emitted ${metricName} - the synchronous debit/hold accounting invariant broke. Investigate immediately (money/correctness).`,
          metric: new cloudwatch.Metric({
            namespace: "Isol8/BedrockGate",
            metricName,
            dimensionsMap: {
              env: gateEnv,
            },
            period: cdk.Duration.minutes(5),
            statistic: "Sum",
          }),
          threshold: 0,
          comparisonOperator:
            cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
          evaluationPeriods: 1,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        });
        alarm.addAlarmAction(pageAction);
      }
    }

    this.gateFnDev = fns["dev"];
    this.gateFnProd = fns["prod"];

    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", props.envName);
  }
}
