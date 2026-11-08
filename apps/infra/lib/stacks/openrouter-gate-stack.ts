import * as path from "path";
import * as fs from "fs";
import { execSync } from "child_process";
import * as cdk from "aws-cdk-lib";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import { logGroupPath, tablePrefix } from "../table-names";

// The one managed-credit markup (repo-root openrouter-pricing.json); the Metronome rate is
// set from the same file by apps/backend/scripts/setup_metronome_openrouter_pricing.py.
const OPENROUTER_MARKUP: string = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "..", "..", "..", "openrouter-pricing.json"), "utf8"),
).markup;
if (!(Number(OPENROUTER_MARKUP) >= 1)) throw new Error(`openrouter-pricing.json markup must be a number >= 1, got ${OPENROUTER_MARKUP}`);

export interface OpenRouterGateStackProps extends cdk.StackProps {
  environment: string;
  /** The stage CMK that wraps credential-envelopes data keys (owner OpenRouter keys). */
  kmsKeyArn: string;
}

/** Managed OpenRouter inference gate + settlement worker, one per env. */
export class OpenRouterGateStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: OpenRouterGateStackProps) {
    super(scope, id, props);
    const env = props.environment;
    const source = path.join(__dirname, "..", "..", "lambda", "openrouter-gate");
    const code = lambda.Code.fromAsset(source, {
      bundling: {
        image: lambda.Runtime.NODEJS_24_X.bundlingImage,
        command: ["bash", "-c", "cp *.mjs package.json package-lock.json /asset-output/ && cd /asset-output && npm ci --omit=dev --no-audit --no-fund"],
        local: {
          tryBundle(outputDir: string): boolean {
            try {
              for (const file of fs.readdirSync(source)) {
                if (file.endsWith(".mjs") || file === "package.json" || file === "package-lock.json") {
                  fs.copyFileSync(path.join(source, file), path.join(outputDir, file));
                }
              }
              execSync("npm ci --omit=dev --no-audit --no-fund", { cwd: outputDir, stdio: "ignore" });
              return true;
            } catch {
              return false;
            }
          },
        },
      },
    });
    const role = new iam.Role(this, "GateRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole")],
    });
    const fn = new lambda.Function(this, "GateFn", {
      functionName: `lightbulb-${env}-openrouter-gate`,
      role,
      runtime: lambda.Runtime.NODEJS_24_X,
      architecture: lambda.Architecture.ARM_64,
      handler: "handler.handler",
      code,
      memorySize: 1024,
      timeout: cdk.Duration.seconds(900),
      reservedConcurrentExecutions: 25,
      environment: {
        GATE_ENV: env,
        TOKENS_TABLE: `${tablePrefix(env)}bedrock-gate-tokens`,
        BILLING_TABLE: `${tablePrefix(env)}billing-accounts`,
        FAST_GATE_TABLE: `${tablePrefix(env)}metronome-fast-gate`,
        OPENROUTER_KEYS_TABLE: `${tablePrefix(env)}openrouter-keys`,
        REQUESTS_TABLE: `${tablePrefix(env)}openrouter-requests`,
        CREDENTIAL_ENVELOPES_TABLE: `${tablePrefix(env)}credential-envelopes`,
        MODEL_POLICY_JSON: env === "dev"
          ? (process.env.OPENROUTER_MODEL_POLICY_JSON_DEV ?? "{}")
          : (process.env.OPENROUTER_MODEL_POLICY_JSON_PROD ?? "{}"),
        // The hard cap for zero-price requests (no byte-for-token bound applies to them).
        // 5 MiB: a turn that carries a photo (an inline image part is base64) is >1 MiB, and the
        // function URL takes up to 6 MB. 413 here read as "The AI model is having problems".
        MAX_BODY_BYTES: "5242880",
        // 4096 cut agents off mid-file: a new team's first "build our site" turn ended in
        // Hermes's "reply was cut off" error (dev walkthrough 2026-10-14). At the policy's
        // completion ceiling 32k tokens cost < $0.01, and the paid reservation stays under
        // metronome_gate.RESERVE_ESTIMATE_MICROCENTS (55,865 vs 300,000 microcents).
        MAX_OUTPUT_TOKENS: "32768",
        OPENROUTER_MARKUP,
      },
      logGroup: new logs.LogGroup(this, "GateLogs", {
        logGroupName: logGroupPath(env, "openrouter-gate"),
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    const tableArn = (name: string) => `arn:aws:dynamodb:${this.region}:${this.account}:table/${tablePrefix(env)}${name}`;
    role.addToPolicy(new iam.PolicyStatement({
      actions: ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:TransactWriteItems"],
      resources: [
        tableArn("bedrock-gate-tokens"), tableArn("billing-accounts"),
        tableArn("metronome-fast-gate"), tableArn("openrouter-keys"),
        tableArn("openrouter-requests"),
      ],
    }));
    // Owner keys are credential-envelopes rows: read one row, decrypt only an openrouter/* data key.
    // The gate is a public Function URL, so nothing wider (no other provider, no writes, no scans).
    const openEnvelopes = () => [
      new iam.PolicyStatement({ actions: ["dynamodb:GetItem"], resources: [tableArn("credential-envelopes")] }),
      new iam.PolicyStatement({
        actions: ["kms:Decrypt"],
        resources: [props.kmsKeyArn],
        conditions: { StringLike: { "kms:EncryptionContext:provider": "openrouter/*" } },
      }),
    ];
    openEnvelopes().forEach(statement => role.addToPolicy(statement));
    role.addToPolicy(new iam.PolicyStatement({
      actions: ["secretsmanager:GetSecretValue"],
      resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/openrouter/owners/*`],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      actions: ["kms:Decrypt", "kms:GenerateDataKey"],
      resources: ["*"],
      conditions: { StringEquals: { "kms:ViaService": `dynamodb.${this.region}.amazonaws.com` } },
    }));
    const url = fn.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.NONE, invokeMode: lambda.InvokeMode.RESPONSE_STREAM });
    new ssm.StringParameter(this, "GateUrl", {
      parameterName: `/lightbulb/${env}/openrouter-gate-url`,
      stringValue: url.url,
    });

    const settlementSrc = path.join(__dirname, "..", "..", "lambda", "openrouter-settlement");
    const wheelFlags = "--only-binary=:all: --platform manylinux2014_aarch64 --implementation cp --python-version 3.12";
    const settlementRole = new iam.Role(this, "SettlementRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole")],
    });
    const settlement = new lambda.Function(this, "SettlementFn", {
      functionName: `lightbulb-${env}-openrouter-settlement`,
      role: settlementRole,
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "index.handler",
      // cryptography opens owner-key envelopes (AES-256-GCM); arm64 cp312 wheels from any host.
      code: lambda.Code.fromAsset(settlementSrc, {
        exclude: ["test_*.py"],
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          command: ["bash", "-c", `pip install -r requirements.txt -t /asset-output ${wheelFlags} && cp index.py /asset-output`],
          local: {
            tryBundle(outputDir: string): boolean {
              try {
                execSync(`pip3 install -r ${settlementSrc}/requirements.txt -t ${outputDir} -q ${wheelFlags}`, { stdio: "inherit" });
                fs.copyFileSync(path.join(settlementSrc, "index.py"), path.join(outputDir, "index.py"));
                return true;
              } catch {
                return false;
              }
            },
          },
        },
      }),
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      // The 5-minute schedule is the retry. Lambda's async retries would only re-run a tick
      // that raised on purpose (a row it may not settle yet) two more times.
      retryAttempts: 0,
      environment: {
        GATE_ENV: env,
        REQUESTS_TABLE: `${tablePrefix(env)}openrouter-requests`,
        OPENROUTER_KEYS_TABLE: `${tablePrefix(env)}openrouter-keys`,
        CREDENTIAL_ENVELOPES_TABLE: `${tablePrefix(env)}credential-envelopes`,
        FAST_GATE_TABLE: `${tablePrefix(env)}metronome-fast-gate`,
        // Settled OpenRouter cost is added here: the backend's own usage counters price only Bedrock.
        USAGE_COUNTERS_TABLE: `${tablePrefix(env)}usage-counters`,
        RESERVE_TTL_SECONDS: "1200",
        METRONOME_API_KEY_SECRET_ID: `lightbulb/${env}/metronome_api_key`,
        OPENROUTER_MANAGEMENT_KEY_SECRET_ID: `lightbulb/${env}/openrouter/management`,
        OPENROUTER_WORKSPACE_ID: env === "dev"
          ? (process.env.OPENROUTER_WORKSPACE_ID_DEV ?? "")
          : (process.env.OPENROUTER_WORKSPACE_ID_PROD ?? ""),
        OPENROUTER_MARKUP,
      },
      logGroup: new logs.LogGroup(this, "SettlementLogs", {
        logGroupName: logGroupPath(env, "openrouter-settlement"),
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    settlementRole.addToPolicy(new iam.PolicyStatement({
      actions: ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:TransactWriteItems"],
      resources: [tableArn("openrouter-requests"), `${tableArn("openrouter-requests")}/index/state-created-index`, tableArn("openrouter-keys"), tableArn("metronome-fast-gate")],
    }));
    openEnvelopes().forEach(statement => settlementRole.addToPolicy(statement));
    // Settlement only ADDs real OpenRouter cost to the usage counters (inside its transaction).
    settlementRole.addToPolicy(new iam.PolicyStatement({
      actions: ["dynamodb:UpdateItem"],
      resources: [tableArn("usage-counters")],
    }));
    settlementRole.addToPolicy(new iam.PolicyStatement({
      actions: ["secretsmanager:GetSecretValue"],
      resources: [
        `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/openrouter/owners/*`,
        `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/openrouter/management-*`,
        `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/metronome_api_key-*`,
      ],
    }));
    // Secrets Manager too: metronome_api_key is on a CMK, and paid rows can't ingest without it
    // (free/capped rows never read it, which is why this went unseen).
    settlementRole.addToPolicy(new iam.PolicyStatement({
      actions: ["kms:Decrypt", "kms:GenerateDataKey"],
      resources: ["*"],
      conditions: { StringEquals: { "kms:ViaService": [
        `secretsmanager.${this.region}.amazonaws.com`,
        `dynamodb.${this.region}.amazonaws.com`,
      ] } },
    }));
    const dlq = new sqs.Queue(this, "SettlementDlq", {
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });
    // Settles free/capped/metered rows and retires keys.
    const schedule = new events.Rule(this, "SettlementSchedule", {
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
    });
    schedule.addTarget(new targets.LambdaFunction(settlement, { deadLetterQueue: dlq, retryAttempts: 2 }));
    // Read-only reconciliation of OpenRouter's per-key activity against settled rows; a gap
    // fails the run (openrouter-settlement-errors alarm).
    const audit = new events.Rule(this, "DailyActivityAudit", {
      schedule: events.Schedule.cron({ minute: "15", hour: "1" }),
    });
    audit.addTarget(new targets.LambdaFunction(settlement, {
      event: events.RuleTargetInput.fromObject({ audit_previous_day: true }),
      deadLetterQueue: dlq,
      retryAttempts: 2,
    }));
  }
}
