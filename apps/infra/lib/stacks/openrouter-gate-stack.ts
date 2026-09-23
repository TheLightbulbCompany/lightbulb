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

export interface OpenRouterGateStackProps extends cdk.StackProps {
  environment: string;
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
        MODEL_POLICY_JSON: env === "dev"
          ? (process.env.OPENROUTER_MODEL_POLICY_JSON_DEV ?? "{}")
          : (process.env.OPENROUTER_MODEL_POLICY_JSON_PROD ?? "{}"),
        OPENROUTER_MANAGED_ENABLED: env === "dev"
          ? (process.env.OPENROUTER_MANAGED_ENABLED_DEV || "false")
          : (process.env.OPENROUTER_MANAGED_ENABLED_PROD || "false"),
        // The hard cap for zero-price requests (no byte-for-token bound applies to them).
        MAX_BODY_BYTES: "1048576",
        MAX_OUTPUT_TOKENS: "4096",
        RESERVE_ESTIMATE_MICROCENTS: "300000",
        RESERVE_TTL_SECONDS: "1200",
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
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "..", "lambda", "openrouter-settlement"), {
        exclude: ["test_*.py"],
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
        FAST_GATE_TABLE: `${tablePrefix(env)}metronome-fast-gate`,
        RESERVE_TTL_SECONDS: "1200",
        METRONOME_API_KEY_SECRET_ID: `lightbulb/${env}/metronome_api_key`,
        OPENROUTER_MANAGEMENT_KEY_SECRET_ID: `lightbulb/${env}/openrouter/management`,
        OPENROUTER_WORKSPACE_ID: env === "dev"
          ? (process.env.OPENROUTER_WORKSPACE_ID_DEV ?? "")
          : (process.env.OPENROUTER_WORKSPACE_ID_PROD ?? ""),
        OPENROUTER_MARKUP,
        OPENROUTER_MONEY_CONTRACT_VERIFIED: "false",
        OPENROUTER_RATE_CARD_PROOF_ID: "",
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
    settlementRole.addToPolicy(new iam.PolicyStatement({
      actions: ["secretsmanager:GetSecretValue"],
      resources: [
        `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/openrouter/owners/*`,
        `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/openrouter/management-*`,
        `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/metronome_api_key-*`,
      ],
    }));
    settlementRole.addToPolicy(new iam.PolicyStatement({
      actions: ["kms:Decrypt", "kms:GenerateDataKey"],
      resources: ["*"],
      conditions: { StringEquals: { "kms:ViaService": `dynamodb.${this.region}.amazonaws.com` } },
    }));
    const dlq = new sqs.Queue(this, "SettlementDlq", {
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });
    // Settles free/capped rows and retires keys. Metered rows stay pending: settle_one
    // refuses them until OPENROUTER_MONEY_CONTRACT_VERIFIED + a rate-card proof land.
    const schedule = new events.Rule(this, "SettlementSchedule", {
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
    });
    schedule.addTarget(new targets.LambdaFunction(settlement, { deadLetterQueue: dlq, retryAttempts: 2 }));
    // Stays off: the handler refuses an audit without the money-contract proof.
    const audit = new events.Rule(this, "DailyActivityAudit", {
      schedule: events.Schedule.cron({ minute: "15", hour: "1" }),
      enabled: false,
    });
    audit.addTarget(new targets.LambdaFunction(settlement, {
      event: events.RuleTargetInput.fromObject({ audit_previous_day: true }),
      deadLetterQueue: dlq,
      retryAttempts: 2,
    }));
  }
}
