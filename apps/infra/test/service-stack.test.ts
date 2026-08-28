import { legacyTablePrefix, tablePrefix } from "../lib/table-names";
import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as kms from "aws-cdk-lib/aws-kms";
import {
  BROWSER_EGRESS_PROXY_PORT,
  ContainerStack,
} from "../lib/stacks/container-stack";
import { ROOT_DOMAIN } from "../lib/domains";
import { DatabaseStack } from "../lib/stacks/database-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { ServiceStack } from "../lib/stacks/service-stack";

/**
 * Builds a real ServiceStack wired to real upstream stacks (Network, Database,
 * Container). Mirrors the wiring in isol8-stage.ts (and marketplace-resources.test.ts)
 * so the synthesized template matches what gets deployed.
 */
function buildServiceStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };

  // KMS key lives in a separate support stack (mirrors AuthStack).
  const supportStack = new cdk.Stack(app, `Support-${environment}`, { env });
  const kmsKey = new kms.Key(supportStack, "KmsKey");

  const network = new NetworkStack(app, `Network-${environment}`, {
    env,
    environment,
  });

  const database = new DatabaseStack(app, `Database-${environment}`, {
    env,
    environment,
    kmsKey,
    vpc: network.vpc,
  });

  const container = new ContainerStack(app, `Container-${environment}`, {
    env,
    environment,
    vpc: network.vpc,
    kmsKeyArn: kmsKey.keyArn,
    gogKeyringPasswordSecretName: `lightbulb/${environment}/gog-keyring-password`,
    cloudflareWarpEnrollmentSecretName: `lightbulb/${environment}/cloudflare-warp-enrollment`,
  });

  const service = new ServiceStack(app, `Service-${environment}`, {
    env,
    environment,
    vpc: network.vpc,
    targetGroup: network.targetGroup,
    albSecurityGroup: network.albSecurityGroup,
    secretNames: {
      clerkIssuer: `isol8/${environment}/clerk_issuer`,
      clerkSecretKey: `isol8/${environment}/clerk_secret_key`,
      clerkWebhookSecret: `isol8/${environment}/clerk_webhook_secret`,
      stripeSecretKey: `isol8/${environment}/stripe_secret_key`,
      stripeWebhookSecret: `isol8/${environment}/stripe_webhook_secret`,
      encryptionKey: `isol8/${environment}/encryption_key`,
      posthogProjectApiKey: `isol8/${environment}/posthog_project_api_key`,
      paperclipServiceTokenKey: `isol8/${environment}/paperclip_service_token_key`,
      paperclipEventsWebhookSecret: `isol8/${environment}/paperclip_events_webhook_secret`,
      gogKeyringPassword: `lightbulb/${environment}/gog-keyring-password`,
      metronomeApiKey: `isol8/${environment}/metronome_api_key`,
      metronomeWebhookSecret: `isol8/${environment}/metronome_webhook_secret`,
      wsInternalSecret: `isol8/${environment}/ws_internal_secret`,
      sesIngestSecret: `isol8/${environment}/ses_ingest_secret`,
    },
    kmsKeyArn: kmsKey.keyArn,
    container: {
      cluster: container.cluster,
      cloudMapNamespace: container.cloudMapNamespace,
      cloudMapService: container.cloudMapService,
      efsFileSystem: container.efsFileSystem,
      efsSecurityGroup: container.efsSecurityGroup,
      containerSecurityGroup: container.containerSecurityGroup,
      taskExecutionRole: container.taskExecutionRole,
      taskRole: container.taskRole,
      openclawTaskDef: container.openclawTaskDef,
      ownerBedrockBasePolicy: container.ownerBedrockBasePolicy,
      ownerBedrockBoundary: container.ownerBedrockBoundary,
    },
    managementApiUrl: "https://example.execute-api.us-east-1.amazonaws.com",
    webSocketUrl: "wss://example.execute-api.us-east-1.amazonaws.com",
    connectionsTableName: `${tablePrefix(environment)}ws-connections`,
    wsApiId: "test-ws-api-id",
    wsStage: "prod",
    agentMailConfigurationSet: "isol8-test-agent-mail",
  });

  return Template.fromStack(service);
}

// Hoist a single ServiceStack synthesis and reuse across tests.
const SERVICE_TEMPLATE_DEV = buildServiceStack("dev");

describe("ServiceStack — clean handoff / rolling deploy", () => {
  const template = SERVICE_TEMPLATE_DEV;

  test("does not wire the retired platform-ops owner or token into backend tasks", () => {
    const taskDefinitions = template.findResources("AWS::ECS::TaskDefinition");
    const rendered = JSON.stringify(taskDefinitions);
    expect(rendered).not.toContain("PLATFORM_OPS_OWNER_ID");
    expect(rendered).not.toContain("PLATFORM_OPS_TOKEN_KEY");
    expect(rendered).not.toContain("platform_ops_token_key");
  });

  test("retains AWS reads required by the human operator console", () => {
    const policies = {
      ...template.findResources("AWS::IAM::Policy"),
      ...template.findResources("AWS::IAM::ManagedPolicy"),
    };
    const rendered = JSON.stringify(policies);
    expect(rendered).toContain("ecs:DescribeServices");
    expect(rendered).toContain("ecs:ListServices");
    expect(rendered).toContain("cloudformation:DescribeStacks");
    expect(rendered).toContain("cloudformation:ListStacks");
    expect(rendered).toContain("cloudwatch:DescribeAlarms");
  });

  test("backend task role can read CloudWatch metric data for the admin growth read", () => {
    const policies = {
      ...template.findResources("AWS::IAM::Policy"),
      ...template.findResources("AWS::IAM::ManagedPolicy"),
    };
    const rendered = JSON.stringify(policies);
    expect(rendered).toContain("cloudwatch:GetMetricData");
  });

  test("backend service uses rolling deploy that starts new before stopping old", () => {
    // At desiredCount=1, MinimumHealthyPercent=100 forces ECS to keep the old
    // task healthy until the new task is healthy (MaximumPercent=200 gives it
    // the headroom to run both at once), so a deploy is a clean handoff with no
    // outage window. Circuit breaker stays on so a bad rollout auto-rolls back.
    template.hasResourceProperties("AWS::ECS::Service", {
      DeploymentConfiguration: {
        MinimumHealthyPercent: 100,
        MaximumPercent: 200,
        DeploymentCircuitBreaker: { Enable: true, Rollback: true },
      },
    });
  });

  test("backend container has a bounded stopTimeout (SIGTERM->SIGKILL window)", () => {
    // 30s gives the app its 20s graceful-shutdown budget plus uvicorn's 25s
    // window before ECS hard-kills. Must be >= the deregistration delay.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({ Name: "backend", StopTimeout: 30 }),
      ]),
    });
  });

  test("backend task definition carries STRIPE_TEAM_BASE_PRICE_ID", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "STRIPE_TEAM_BASE_PRICE_ID" }),
          ]),
        }),
      ]),
    });
  });

  test("backend task env carries an EMPTY launch drop date — the feature is inert until an owner sets it", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "LAUNCH_DROP_AT", Value: "" }),
            Match.objectLike({ Name: "LAUNCH_COMP_SLOTS", Value: "220" }),
          ]),
        }),
      ]),
    });
  });

  test("backend role's wildcard table statement admits the launch tables without a new grant", () => {
    // allTableArns(env) → table/*-{env}-* — any table under the prefix is covered.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["dynamodb:PutItem", "dynamodb:Query"]),
            Resource: Match.arrayWith([Match.stringLikeRegexp("table/\\*-dev-\\*$")]),
          }),
        ]),
      }),
    });
  });

  test("backend role can pass per-owner bedrock roles to ECS", () => {
    // The owner-bedrock prefix ARN is built from ${this.account}; in this
    // concrete-account synth CDK renders it as a literal string (it becomes an
    // Fn::Join only when the account is an unresolved token). The PassRole
    // statement is also PassedToService-conditioned to ecs-tasks.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "iam:PassRole",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                "iam:PassedToService": "ecs-tasks.amazonaws.com",
              }),
            }),
            Resource: Match.arrayWith([
              "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-*",
            ]),
          }),
        ]),
      }),
    });
  });

  // The per-owner role-management + Bedrock-control-plane grants live in a
  // dedicated ManagedPolicy (isol8-dev-backend-owner-bedrock-manage), NOT the
  // inline default policy, so the inline policy stays under CloudFormation's
  // 10 KB limit and does not spill other statements into an overflow policy.
  test("backend role can create boundary-bounded per-owner roles", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "iam:CreateRole",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                "iam:PermissionsBoundary": Match.anyValue(),
              }),
            }),
          }),
        ]),
      }),
    });
  });

  test("backend role can mint Bedrock application inference profiles, scoped by the ManagedBy request tag", () => {
    // CreateInferenceProfile is gated on aws:RequestTag/ManagedBy so the
    // backend can only mint profiles it marks as its own (the AIP ARN cannot be
    // pre-scoped at synth time). Without the condition this would be an
    // unconditioned Resource:"*" create grant — see I-1.
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:CreateInferenceProfile"]),
            Resource: "*",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                // Both values during the rename migration: the legacy tag on the 39
                // existing profiles MUST keep matching, or the backend
                // AccessDenies on every one of them.
                "aws:RequestTag/ManagedBy": ["lightbulb-backend"],
              }),
            }),
          }),
        ]),
      }),
    });
  });

  test("backend role can act on its own Bedrock profiles, scoped by the ManagedBy resource tag", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:DeleteInferenceProfile"]),
            Resource: "*",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                // Both values during the rename migration: the legacy tag on the 39
                // existing profiles MUST keep matching, or the backend
                // AccessDenies on every one of them.
                "aws:ResourceTag/ManagedBy": ["lightbulb-backend"],
              }),
            }),
          }),
        ]),
      }),
    });
  });

  // Account-wipe tool (Task 5): the backend needs to delete the per-owner
  // Bedrock role once its policies are detached. Scoped to the same
  // owner-bedrock-* role-name pattern as the rest of OwnerBedrockRoleManage —
  // never a bare "*", and never reaching the shared base managed policy.
  test("backend role can delete per-owner bedrock roles, scoped to the owner-bedrock-* role pattern", () => {
    // OwnerBedrockRoleManage has a single-element `resources` array, so CDK
    // synthesizes it as a bare Resource string (not a list) — unlike the
    // multi-resource IamPassRole statement asserted with arrayWith() above.
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-backend-owner-bedrock-manage",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "OwnerBedrockRoleManage",
            Action: Match.arrayWith(["iam:DeleteRole"]),
            Resource: "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-*",
          }),
        ]),
      }),
    });
  });

  // Account-wipe (Task 7 fix): the in-backend teardown deletes the owner's
  // Paperclip Better Auth "user" row via the RDS Data API, which needs three
  // grants the task role otherwise lacks. These live in the inline default
  // policy (AWS::IAM::Policy) via addToPolicy — unlike the bedrock-role-delete
  // grant, which lives in the dedicated ManagedPolicy. Assertions are
  // non-vacuous: each asserts the action IS present AND the resource is the
  // expected scoped ARN (not a bare "*"), except ListSecrets which IS "*".
  test("backend role can run the Paperclip Aurora-user delete (rds-data:ExecuteStatement scoped to the paperclip cluster)", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AccountWipePaperclipAuroraExec",
            Action: "rds-data:ExecuteStatement",
            Resource:
              "arn:aws:rds:us-east-1:877352799272:cluster:isol8-dev-paperclip-db",
          }),
        ]),
      }),
    });
  });

  test("backend role can read the dash-named paperclip-db-credentials secret (GetSecretValue scoped, not the slash-named wildcard)", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AccountWipePaperclipDbSecretRead",
            Action: "secretsmanager:GetSecretValue",
            Resource:
              "arn:aws:secretsmanager:us-east-1:877352799272:secret:isol8-dev-paperclip-db-credentials-*",
          }),
        ]),
      }),
    });
  });

  test("backend role can list secrets (metadata-only, resource '*' — the paginated ARN discovery)", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AccountWipeListSecrets",
            Action: "secretsmanager:ListSecrets",
            Resource: "*",
          }),
        ]),
      }),
    });
  });

  test("backend STS lives in a dedicated ManagedPolicy: AssumeRole scoped to the connector role, GetCallerIdentity unscoped", () => {
    // A ManagedPolicy (not DefaultPolicy) so the near-10 KB inline document's
    // packing stays stable; the AssumeRole resource pin is the security fence
    // (an unscoped * let a compromised backend assume any trusted role).
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "StsAccess",
            Action: "sts:GetCallerIdentity",
            Resource: "*",
          }),
          Match.objectLike({
            Sid: "StsAssumeConnectorRole",
            Action: "sts:AssumeRole",
            // Both names: the role is in the OWNER'S account, so dropping
            // the legacy ARN AccessDenies anyone who has not relaunched.
            Resource: [
              "arn:aws:iam::*:role/lightbulb-connector",
              "arn:aws:iam::*:role/isol8-connector",
            ],
          }),
        ]),
      }),
    });
  });

  test("backend role can read the bedrock-gate URL SSM parameter (single-parameter GetParameter, not a wildcard)", () => {
    // The gate stack publishes each env's Function URL to
    // /lightbulb/{env}/bedrock-gate-url; the backend discovers it at runtime and
    // the parameter doubles as the break-glass lever ("direct" = bypass gate).
    //
    // Searched across BOTH inline and managed policies: the task role's
    // DefaultPolicy sits near CloudFormation's 10 KB limit, so CDK packs
    // grants into overflow ManagedPolicies and the exact placement shifts as
    // grants change (this statement lives in an overflow policy today). What
    // this test pins is the single-parameter resource scope, wherever the
    // statement lands — asserting the packing location made the test red the
    // first time CDK repacked, which no CI job caught.
    const found = ["AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"].some((type) =>
      Object.values(SERVICE_TEMPLATE_DEV.findResources(type)).some((r) =>
        ((r.Properties?.PolicyDocument?.Statement ?? []) as Array<Record<string, unknown>>).some(
          (s) =>
            s.Sid === "BedrockGateUrlParamRead" &&
            s.Action === "ssm:GetParameter" &&
            s.Resource ===
              "arn:aws:ssm:us-east-1:877352799272:parameter/lightbulb/dev/bedrock-gate-url",
        ),
      ),
    );
    expect(found).toBe(true);
  });
});

describe("ServiceStack — provisioning executor (state machine, live)", () => {
  const template = SERVICE_TEMPLATE_DEV;

  test("provision state machine and provisioner task-def exist", () => {
    template.resourceCountIs("AWS::StepFunctions::StateMachine", 1);
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: "isol8-dev-provisioner",
    });
  });

  test("backend task definition carries PROVISION_STATE_MACHINE_ARN and no PROVISION_EXECUTOR flag", () => {
    // No flag, no dual-run window (spec §7 / PR-10) — the executor is active
    // on merge. Regression guard: a re-introduced flag would silently
    // reopen the inline/SFN split.
    const backendTaskDefs = Object.values(
      template.findResources("AWS::ECS::TaskDefinition", {
        Properties: { Family: "isol8-dev-backend" },
      }),
    );
    expect(backendTaskDefs).toHaveLength(1);
    const containerDefs = (backendTaskDefs[0] as any).Properties
      ?.ContainerDefinitions as any[];
    const backendContainer = containerDefs.find((c) => c.Name === "backend");
    const envNames = (backendContainer.Environment ?? []).map(
      (e: any) => e.Name,
    );
    expect(envNames).toContain("PROVISION_STATE_MACHINE_ARN");
    expect(envNames).not.toContain("PROVISION_EXECUTOR");
  });

  test("state machine definition pins the load-bearing ASL (claim condition, missing-exit-code guard, exit-code routing, per-attempt fencing token)", () => {
    const stateMachines = Object.values(
      template.findResources("AWS::StepFunctions::StateMachine"),
    );
    expect(stateMachines).toHaveLength(1);
    const def = (stateMachines[0] as any).Properties?.DefinitionString;
    // DefinitionString synthesizes as an Fn::Join of literal strings and Refs;
    // flatten the literal parts to assert on the ASL content.
    const flat = (def?.["Fn::Join"]?.[1] ?? [def])
      .map((part: any) => (typeof part === "string" ? part : ""))
      .join("");
    // (a) Claim-conditioned DDB writes — the superseded/SIGKILL backstop rule.
    expect(flat).toContain(
      "provision_claim_id = :cid AND (#s = :prov OR #s = :err)",
    );
    // (b) A task that never started (EFS mount / image-pull failure) stops
    // with NO exit code in the Cause. A Choice rule referencing the missing
    // path throws States.Runtime, Choice states cannot Catch, and the
    // execution dies with the row stuck at "provisioning" — so ClassifyFailure
    // MUST guard on IsPresent and route the no-exit-code case to the retry
    // loop before any numeric comparison. ORDER is the load-bearing property
    // (Choice rules evaluate in order), so assert ordinally: the IsPresent
    // guard exists, precedes the numeric rules, and its Next targets the
    // retry gate (AttemptsRemain) within the same Choices region.
    expect(flat).toContain(
      '"Variable":"$.cause.Containers[0].ExitCode","IsPresent":true',
    );
    const isPresentIdx = flat.indexOf('"IsPresent":true');
    const exit20Idx = flat.indexOf('"NumericEquals":20');
    expect(isPresentIdx).toBeGreaterThanOrEqual(0);
    expect(exit20Idx).toBeGreaterThanOrEqual(0);
    expect(isPresentIdx).toBeLessThan(exit20Idx);
    const guardNextIdx = flat.indexOf('"Next":"AttemptsRemain"', isPresentIdx);
    expect(guardNextIdx).toBeGreaterThan(isPresentIdx);
    expect(guardNextIdx).toBeLessThan(exit20Idx);
    // (c) Exit-code ABI routing: 20 superseded, 10 deterministic.
    expect(flat).toContain(
      '"Variable":"$.cause.Containers[0].ExitCode","NumericEquals":20',
    );
    expect(flat).toContain(
      '"Variable":"$.cause.Containers[0].ExitCode","NumericEquals":10',
    );
    // (d) Terminal-write backstop resilience: the DDB writes retry transient
    // failures, with the fencing signal (ConditionalCheckFailed) excluded
    // FIRST so a taken-over row falls to the Superseded catch, never a retry.
    expect(flat).toContain(
      '"ErrorEquals":["DynamoDB.ConditionalCheckFailedException"],"MaxAttempts":0',
    );
    // (e) Per-retry attempt-token fencing (spec §7 hazard 1): BumpAttempt
    // mints a FRESH token via the States.UUID() intrinsic on every retry,
    // while claim_id (and so the execution name) stays untouched.
    expect(flat).toContain('"attempt_token.$":"States.UUID()"');
    expect(flat).toContain('"claim_id.$":"$.claim_id"');
  });

  test("machine watches the row from claim time before running the provisioner", () => {
    // Custody invariant: the backend starts this execution AT CLAIM TIME and
    // runs attempt 0 as its own child process, so the machine's job is to
    // OBSERVE the row and take the ECS leg over on its own clock. Before
    // this, the machine started at RunProvisioner and the backend had to
    // hand the claim over per exit path — the leg it never had (nonzero
    // exit) orphaned claims onto a frontend poll.
    const stateMachines = Object.values(
      template.findResources("AWS::StepFunctions::StateMachine"),
    );
    const def = (stateMachines[0] as any).Properties?.DefinitionString;
    const flat = (def?.["Fn::Join"]?.[1] ?? [def])
      .map((part: any) => (typeof part === "string" ? part : ""))
      .join("");
    // (a) The machine starts by watching, not by launching an ECS task.
    expect(flat).toContain('"StartAt":"InitWatch"');
    // (b) watch_ticks is seeded INSIDE the machine. It must never appear in
    // the backend's StartExecution input, which has to stay byte-identical
    // across Branch-A re-issues of the same claim.
    expect(flat).toContain('"watch_ticks":0');
    expect(flat).toContain('"watch_ticks.$":"States.MathAdd($.watch_ticks, 1)"');
    // (c) Consistent read: the runner may have written the terminal row
    // milliseconds ago, and an eventually-consistent read would spend a whole
    // tick (or mis-route) on stale data.
    expect(flat).toContain('"ConsistentRead":true');
    // (d) Terminal-success rule must precede the claim-identity rule: the
    // running transition REMOVEs the lease fields, so a claim check first
    // would read a successful provision as "superseded".
    const runningIdx = flat.indexOf('"Variable":"$.row.Item.status.S","StringEquals":"running"');
    const claimIdx = flat.indexOf('"$.row.Item.provision_claim_id.S"');
    expect(runningIdx).toBeGreaterThanOrEqual(0);
    expect(claimIdx).toBeGreaterThan(runningIdx);
    // (d2) No rule may compare a path that could be absent — a Choice on a
    // missing path throws States.Runtime, which a Choice cannot Catch, and
    // the execution would die holding the lease (the very orphan this loop
    // prevents). Guaranteed here by rule ORDER: each presence check is its
    // own earlier rule, so the comparison after it cannot be reached with the
    // attribute missing.
    const statusPresentIdx = flat.indexOf(
      '"Variable":"$.row.Item.status.S","IsPresent":false,"Next":"Superseded"',
    );
    expect(statusPresentIdx).toBeGreaterThanOrEqual(0);
    expect(statusPresentIdx).toBeLessThan(runningIdx);
    const claimPresentIdx = flat.indexOf(
      '"Variable":"$.row.Item.provision_claim_id.S","IsPresent":false,"Next":"Superseded"',
    );
    const claimComparisonIdx = flat.indexOf('"StringEqualsPath":"$.claim_id"');
    expect(claimPresentIdx).toBeGreaterThanOrEqual(0);
    expect(claimComparisonIdx).toBeGreaterThan(claimPresentIdx);
    // (d3) The watch loop must not read `retryable` at all: it is absent on
    // rows predating the field, and the deterministic case already
    // terminates via the runner's exit-10 -> ClassifyFailure route.
    const watchRowRegion = flat.slice(
      flat.indexOf('"WatchRow"'),
      flat.indexOf('"Superseded":{"Type":"Succeed"}'),
    );
    expect(watchRowRegion).not.toContain("retryable");
    // (e) The dead-runner backstop: a leased row with no progress for 40
    // ticks gets the ECS leg. 40 x 60s = 2400s MUST exceed the backend's
    // inline queue bound + attempt timeout (300 + 1800 = 2100s,
    // provision_dispatch.py) so a takeover never overlaps a live inline
    // child.
    expect(flat).toContain('"Variable":"$.watch_ticks","NumericGreaterThanEquals":40');
    // (f) The watch loop cycles: WaitWatchTick -> TickWatch -> GetContainerRow.
    expect(flat).toContain('"WaitWatchTick"');
    expect(flat).toContain('"Next":"GetContainerRow"');
    // (f2) The row read must never be able to KILL the watchdog. Its retries
    // cover only ~14s; a longer DynamoDB/KMS impairment would exhaust them
    // and, with no Catch, fail the execution while the row is still leased —
    // the orphaned claim this loop exists to prevent, caused by the loop
    // itself. It must fall back into the wait/tick cycle and keep trying.
    expect(flat).toContain(
      '"Catch":[{"ErrorEquals":["States.ALL"],"ResultPath":null,"Next":"WaitWatchTick"}]',
    );
    // (g) A stopped row stands down and must be decided BEFORE the tick
    // backstop, or the watchdog would resurrect a deliberately stopped
    // container. update_status("stopped") is not claim-conditioned, so this
    // state really can appear under our own claim.
    const stoppedIdx = flat.indexOf(
      '"Variable":"$.row.Item.status.S","StringEquals":"stopped","Next":"Superseded"',
    );
    const backstopIdx = flat.indexOf('"NumericGreaterThanEquals":40');
    expect(stoppedIdx).toBeGreaterThanOrEqual(0);
    expect(backstopIdx).toBeGreaterThan(stoppedIdx);
  });

  test("machine timeout covers the watch loop plus every ECS attempt", () => {
    // 40 ticks (~40 min) + up to 3 x 30-min attempts + retry waits ~= 132
    // min. The old 2h ceiling would time the machine out mid-final-attempt.
    // The timeout lives INSIDE the ASL (TimeoutSeconds), not as a CFN prop.
    const stateMachines = Object.values(
      template.findResources("AWS::StepFunctions::StateMachine"),
    );
    const def = (stateMachines[0] as any).Properties?.DefinitionString;
    const flat = (def?.["Fn::Join"]?.[1] ?? [def])
      .map((part: any) => (typeof part === "string" ? part : ""))
      .join("");
    expect(flat).toContain('"TimeoutSeconds":10800');
  });

  test("state machine may read the containers row it watches", () => {
    // The watch loop's GetItem needs dynamodb:GetItem AND the CMK grant on
    // the SAME role: the containers table is CUSTOMER_MANAGED-encrypted and
    // the key crosses stacks as a bare ARN, so nothing auto-grants it.
    // Without Decrypt every tick fails and the watchdog is dead on arrival —
    // the 2026-08-10 KMS class of bug, one state over. The KMS Sid uniquely
    // identifies the state machine's role policy (the backend task role also
    // holds dynamodb:GetItem, so finding by action would match the wrong
    // policy).
    const policies = Object.values(
      template.findResources("AWS::IAM::Policy"),
    ) as any[];
    const machinePolicy = policies.find((p) =>
      (p.Properties?.PolicyDocument?.Statement ?? []).some(
        (s: any) => s.Sid === "ContainersTableKmsAccess",
      ),
    );
    expect(machinePolicy).toBeDefined();
    const statements = machinePolicy.Properties.PolicyDocument
      .Statement as any[];
    const kmsStatement = statements.find(
      (s) => s.Sid === "ContainersTableKmsAccess",
    );
    expect(kmsStatement.Action).toEqual(
      expect.arrayContaining(["kms:Decrypt", "kms:GenerateDataKey"]),
    );
    const canRead = statements.some((s) =>
      Array.isArray(s.Action)
        ? s.Action.includes("dynamodb:GetItem")
        : s.Action === "dynamodb:GetItem",
    );
    expect(canRead).toBe(true);
  });

  test("RunProvisioner overrides PROVISION_ATTEMPT_TOKEN from $.attempt_token, not baked into the static command", () => {
    const stateMachines = Object.values(
      template.findResources("AWS::StepFunctions::StateMachine"),
    );
    const def = (stateMachines[0] as any).Properties?.DefinitionString;
    const flat = (def?.["Fn::Join"]?.[1] ?? [def])
      .map((part: any) => (typeof part === "string" ? part : ""))
      .join("");
    expect(flat).toContain("PROVISION_ATTEMPT_TOKEN");
    expect(flat).toContain("$.attempt_token");
  });

  test("backend task role may start provision executions", () => {
    // The task role's inline default policy is at CloudFormation's ~10 KB
    // ceiling, so statements added late in the constructor spill into a
    // TaskRoleOverflowPolicy ManagedPolicy attached to the same role (see the
    // overflow-packing comment at the companySlugsTable grant in
    // service-stack.ts). Functionally identical either way, so this assertion
    // accepts the statement in EITHER the inline AWS::IAM::Policy or an
    // AWS::IAM::ManagedPolicy — but if it's in a ManagedPolicy, that policy
    // must be attached to the task role.
    const wantedActions = [
      "states:StartExecution",
      "states:DescribeExecution",
      "states:StopExecution",
    ];
    const hasDispatchStatement = (doc: any): boolean =>
      (doc?.Statement ?? []).some(
        (s: any) =>
          s.Sid === "ProvisionStateMachineDispatch" &&
          JSON.stringify(s.Action) === JSON.stringify(wantedActions) &&
          // Pin the Resource scope: exactly the state machine ARN (a Ref to
          // the SM resource) plus the execution-ARN pattern — a future
          // widening to "*" must fail this test.
          Array.isArray(s.Resource) &&
          s.Resource.length === 2 &&
          typeof s.Resource[0]?.Ref === "string" &&
          s.Resource[0].Ref.startsWith("ProvisionStateMachine") &&
          s.Resource[1] ===
            "arn:aws:states:us-east-1:877352799272:execution:lightbulb-dev-provision:*",
      );

    const inline = Object.values(
      template.findResources("AWS::IAM::Policy"),
    ).some((r: any) => hasDispatchStatement(r.Properties?.PolicyDocument));
    const managed = Object.values(
      template.findResources("AWS::IAM::ManagedPolicy"),
    ).some(
      (r: any) =>
        hasDispatchStatement(r.Properties?.PolicyDocument) &&
        (r.Properties?.Roles ?? []).some(
          (role: any) => typeof role?.Ref === "string" && role.Ref.startsWith("TaskRole"),
        ),
    );
    expect(inline || managed).toBe(true);
  });

  // These two alarms shipped with NO AlarmActions at all — they watched the
  // signup path and notified nobody. An alarm without an action is decoration
  // and it fails silently, so assert the wiring, not the alarm's existence.
  test("provisioning failure alarms page the alerts topic", () => {
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    const provisioning = Object.values(alarms).filter((a: any) =>
      String(a.Properties?.AlarmName ?? "").includes("provision-executions"),
    );

    // failed + timed-out + slow (the 2026-08-20 founder outage was an
    // execution that SUCCEEDED after 35 min, which the first two cannot see).
    expect(provisioning).toHaveLength(3);
    for (const alarm of provisioning as any[]) {
      expect(alarm.Properties.AlarmActions).toEqual([
        expect.stringContaining("lightbulb-dev-alerts-page"),
      ]);
    }
  });
});

describe("ServiceStack — Clerk azp (SEC-8) per-env wiring", () => {
  // Clerk session tokens carry no `aud`; the token-origin fence is the azp
  // allowlist. Pin the per-env origins and the audit rollout default — a
  // dropped var here silently disables the check (backend treats empty as
  // off), and a premature "enforce" would reject before audit validated the
  // list in the wild.
  test.each([
    [
      "dev",
      `https://dev.${ROOT_DOMAIN},https://admin.dev.${ROOT_DOMAIN},https://dev.company.${ROOT_DOMAIN},http://localhost:3000`,
    ],
    [
      "prod",
      `https://${ROOT_DOMAIN},https://www.${ROOT_DOMAIN},https://admin.${ROOT_DOMAIN},https://company.${ROOT_DOMAIN}`,
    ],
  ] as const)("%s backend gets azp allowlist in audit mode", (envName, parties) => {
    const template = envName === "dev" ? SERVICE_TEMPLATE_DEV : buildServiceStack("prod");
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "CLERK_AUTHORIZED_PARTIES", Value: parties }),
            Match.objectLike({ Name: "CLERK_AZP_MODE", Value: "audit" }),
          ]),
        }),
      ]),
    });
  });
});


describe("ServiceStack — production HTTP CORS origins", () => {
  test("prod backend accepts apex and www frontend origins", () => {
    const template = buildServiceStack("prod");
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "CORS_ORIGINS",
              Value: `https://${ROOT_DOMAIN},https://www.${ROOT_DOMAIN}`,
            }),
          ]),
        }),
      ]),
    });
  });
});

describe("ServiceStack — Metronome per-env wiring", () => {
  // METRONOME_LIFECYCLE_ENABLED is GONE (it defaulted false and gated only the
  // ledger-CREATING paths, never the ones that depend on a ledger, so any
  // fail-closed credit check became unrecoverable wherever it was off). What
  // still needs pinning is the per-env ids: they are console-created values
  // with no schema to catch a mix-up, and swapping dev/prod would bill against
  // the wrong rate card.
  test("dev references the sandbox rate card", () => {
    SERVICE_TEMPLATE_DEV.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "METRONOME_RATE_CARD_ID",
              Value: "99576f5a-37d2-4983-ba64-fea9bad96e08",
            }),
          ]),
        }),
      ]),
    });
  });

  test("prod references the prod rate card + credits product", () => {
    // Prod's runbook completed 2026-06-12: prod token in Secrets Manager,
    // rate card + credits product created via API (rates verified 12/12
    // against sandbox), Stripe live product wired, webhook secret set.
    const prodTemplate = buildServiceStack("prod");
    prodTemplate.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "METRONOME_RATE_CARD_ID",
              Value: "fdac2edd-f207-4f11-b65b-01fad6fbbdbc",
            }),
            Match.objectLike({
              Name: "METRONOME_CREDITS_PRODUCT_ID",
              Value: "876e1694-9d76-4c37-9814-c957dbd9370f",
            }),
          ]),
        }),
      ]),
    });
  });
});

describe("ServiceStack — browser egress proxy knob", () => {
  // The backend decides whether Chrome gets a PAC; the container decides what
  // port WARP listens on. They are set in two different stacks, so the only
  // thing stopping a drift is that both read one exported constant. Flipping
  // this knob on by typing a port number instead points Chrome at a port
  // nothing listens on — and that fails SILENTLY, because the PAC's
  // "; DIRECT" tail quietly falls back to the NAT IP. Hence the assert.
  test.each(["dev", "prod"] as const)(
    "%s sets BROWSER_EGRESS_PROXY_PORT to off or to the container's actual port",
    (envName) => {
      const template =
        envName === "dev" ? SERVICE_TEMPLATE_DEV : buildServiceStack(envName);
      const taskDefs = Object.values(
        template.findResources("AWS::ECS::TaskDefinition"),
      ) as Array<{
        Properties?: {
          ContainerDefinitions?: Array<{
            Environment?: Array<{ Name?: string; Value?: string }>;
          }>;
        };
      }>;
      const values = taskDefs
        .flatMap((t) => t.Properties?.ContainerDefinitions ?? [])
        .flatMap((c) => c.Environment ?? [])
        .filter((e) => e.Name === "BROWSER_EGRESS_PROXY_PORT")
        .map((e) => e.Value);
      expect(values.length).toBeGreaterThan(0);
      for (const value of values) {
        expect(["0", String(BROWSER_EGRESS_PROXY_PORT)]).toContain(value);
      }
    },
  );
});

describe("ServiceStack — agent-mail outbound wiring", () => {
  const template = SERVICE_TEMPLATE_DEV;

  test("backend task definition carries AGENT_MAIL_CONFIGURATION_SET", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "AGENT_MAIL_CONFIGURATION_SET",
              Value: "isol8-test-agent-mail",
            }),
          ]),
        }),
      ]),
    });
  });

  test("the backend-ses-send managed policy grants per-owner SES tenant creation", () => {
    // Per-owner SES tenants are created lazily by the backend at first send
    // (core/services/agent_mail_tenant.py) — this is the IAM side of that.
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-backend-ses-send",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AgentMailTenants",
            Action: Match.arrayWith(["ses:CreateTenant"]),
            Resource: Match.arrayWith([
              "arn:aws:ses:us-east-1:877352799272:tenant/owner-*",
              "arn:aws:ses:us-east-1:aws:reputation-policy/standard",
            ]),
          }),
        ]),
      }),
    });
  });

  test("the AgentMailTenants tenant resource is tightened to owner-* (not a bare wildcard)", () => {
    // Tenant names are deterministic `owner-{owner_id}` — a bare `tenant/*`
    // would also authorize actions against tenants outside this fleet's
    // naming convention.
    const policies = template.findResources("AWS::IAM::ManagedPolicy");
    const sesSend = Object.values(policies).find(
      (p: any) => p.Properties?.ManagedPolicyName === "isol8-dev-backend-ses-send",
    ) as any;
    expect(sesSend).toBeDefined();
    const statement = sesSend.Properties.PolicyDocument.Statement.find(
      (s: any) => s.Sid === "AgentMailTenants",
    );
    expect(statement.Resource).not.toContain(
      "arn:aws:ses:us-east-1:877352799272:tenant/*",
    );
  });
});
