import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Match, Template } from "aws-cdk-lib/assertions";
import { ContainerStack } from "../lib/stacks/container-stack";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { HOSTED_ZONE_CONTEXT } from "./helpers";

/**
 * What is left of this stack after the Hermes-on-AgentCore migration: the
 * platform ECS cluster, the Cloud Map namespace Paperclip registers into, the
 * per-owner Bedrock base policy + permissions boundary (which double as every
 * workspace's AgentCore Runtime execution role), the per-owner state bucket
 * every Hermes profile lives in, and the CI image-builder role.
 *
 * The per-user OpenClaw task definition and its log group are gone, and so are
 * the EFS filesystem, the EFS security group, the two per-user ECS roles, the
 * per-user Cloud Map service and the transitional export pins that held their
 * exports open for one deploy.
 */
function buildStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const supportStack = new cdk.Stack(app, `Support-${environment}`, { env });
  const vpc = new ec2.Vpc(supportStack, "Vpc");
  const containerStack = new ContainerStack(app, `Container-${environment}`, {
    env,
    environment,
    vpc,
  });
  return Template.fromStack(containerStack);
}

function managedPolicy(template: Template, name: string): any {
  const policies = template.findResources("AWS::IAM::ManagedPolicy");
  const policy = Object.values(policies).find(
    (p: any) => p.Properties.ManagedPolicyName === name,
  );
  expect(policy, `missing managed policy ${name}`).toBeDefined();
  return policy;
}

describe("ContainerStack — dev", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  const memoryNamed = (suffix: string) =>
    (Object.values(template.findResources("AWS::BedrockAgentCore::Memory")) as any[]).find((m) =>
      m.Properties.Name.endsWith(suffix),
    );

  test("workspace memory declares the six indexed keys", () => {
    const memory = memoryNamed("_workspace_memory");
    expect(memory.Properties.IndexedKeys).toEqual([
      { Key: "kind", Type: "STRING" },
      { Key: "entity", Type: "STRING" },
      { Key: "attribute", Type: "STRING" },
      { Key: "status", Type: "STRING" },
      { Key: "validFrom", Type: "NUMBER" },
      { Key: "validTo", Type: "NUMBER" },
    ]);
    const custom = memory.Properties.MemoryStrategies.find((s: any) => s.CustomMemoryStrategy)
      .CustomMemoryStrategy;
    expect(custom.Name).toBe("workspace_claims");
    expect(custom.Namespaces).toEqual(["/owners/{actorId}/"]);
    const schema = Object.fromEntries(
      custom.MemoryRecordSchema.MetadataSchema.map((m: any) => [m.Key, m]),
    );
    expect(Object.keys(schema).sort()).toEqual(
      ["attribute", "entity", "kind", "status", "validFrom", "validTo"].sort(),
    );
    const llm = (k: string) => schema[k].ExtractionConfig.LlmExtractionConfig;
    expect(llm("kind").Validation.StringValidation.AllowedValues).toEqual([
      "claim", "observation", "rule", "mention", "anchor",
    ]);
    expect(llm("status").Validation.StringValidation.AllowedValues).toEqual(["current", "contested"]);
    expect(llm("status").LlmExtractionInstruction).toBe("LATEST_VALUE");
    for (const k of ["status", "validFrom", "validTo"]) {
      expect(llm(k).Definition).toMatch(/record text/i);
    }
    // Every record carries a validFrom, so an as-of filter (validFrom <= as_of) keeps undated claims.
    expect(llm("validFrom").Definition).toMatch(/when no date is stated, the date of the conversation it came from/);
    // Haiku guessed the year (2024 for claims said in 2026, prod 30 Oct 2026): the year comes from the
    // messages, and the epoch is counted from a stated anchor, not recalled.
    expect(llm("validFrom").Definition).toMatch(/never a year you assume/);
    expect(llm("validFrom").Definition).toContain("2026-01-01 is 1767225600");
    expect(llm("validFrom").Definition.length).toBeLessThanOrEqual(1000); // AgentCore's limit
    // Both overrides carry our prompt files after AWS's defaults.
    const sem = custom.Configuration.SemanticOverride;
    expect(sem.Extraction.AppendToPrompt).toContain("<entity> <attribute>: <value>");
    expect(sem.Extraction.AppendToPrompt).toMatch(/never from your own sense of the current year/);
    expect(sem.Consolidation.AppendToPrompt).toContain("Contested:");
    expect(sem.Extraction.ModelId).toMatch(/haiku/);
  });

  test("memory execution role is trusted by agentcore and can invoke the extract model", () => {
    const memory = memoryNamed("_workspace_memory");
    const roleId = memory.Properties.MemoryExecutionRoleArn["Fn::GetAtt"][0];
    const roles = template.findResources("AWS::IAM::Role");
    const role = roles[roleId] as any;
    const trust = role.Properties.AssumeRolePolicyDocument.Statement[0];
    expect(trust.Principal.Service).toBe("bedrock-agentcore.amazonaws.com");
    expect(JSON.stringify(trust.Condition)).toContain("memory/*");
    // The memory waits for the role's policy, not only the role (UpdateMemory checks model access).
    const policies = template.findResources("AWS::IAM::Policy");
    const policyId = Object.keys(policies).find((id) =>
      ((policies[id] as any).Properties.Roles ?? []).some((r: any) => r.Ref === roleId),
    );
    expect(policyId).toBeDefined();
    expect(memory.DependsOn).toContain(policyId);
    template.hasResourceProperties("AWS::IAM::Policy", {
      Roles: [{ Ref: roleId }],
      PolicyDocument: {
        Statement: [
          Match.objectLike({
            Action: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            Resource: Match.arrayWith([
              Match.stringLikeRegexp("inference-profile/us\\.anthropic\\.claude-haiku"),
              "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
            ]),
          }),
        ],
      },
    });
  });

  test("episodic strategy lives alone in the sessions memory, with per-user namespaces", () => {
    const memory = memoryNamed("_sessions_memory");
    expect(memory.Properties.Name).toBe("lightbulb_dev_sessions_memory");
    expect(memory.Properties.MemoryStrategies).toHaveLength(1);
    const workspace = memoryNamed("_workspace_memory");
    expect(workspace.Properties.MemoryStrategies.some((s: any) => s.EpisodicMemoryStrategy)).toBe(false);
    const episodic = memory.Properties.MemoryStrategies.find((s: any) => s.EpisodicMemoryStrategy)
      .EpisodicMemoryStrategy;
    expect(episodic.Name).toBe("session_episodes");
    expect(episodic.Namespaces).toEqual(["/users/{actorId}/episodes/"]);
    expect(episodic.ReflectionConfiguration.Namespaces).toEqual(["/users/{actorId}/"]);
  });

  test("sessions prefixes expire after 30 and 365 days", () => {
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "lightbulb-dev-openclaw-replicas",
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({ Prefix: "sessions/free/", ExpirationInDays: 30, Status: "Enabled" }),
          Match.objectLike({ Prefix: "sessions/workspace/", ExpirationInDays: 365, Status: "Enabled" }),
        ]),
      },
    });
  });

  test("replicas bucket expires task overlays after 14 days by tag", () => {
    // Super-agent per-task VMs tag every object they write under <owner>/hermes-tasks/ with
    // lb-kind=task-overlay. Tag-scoped, no prefix: inert wherever nothing carries the tag (prod).
    for (const environment of ["dev", "prod"] as const) {
      const rules = Object.values(buildStack(environment).findResources("AWS::S3::Bucket"))
        .find((b: any) => b.Properties.BucketName === `lightbulb-${environment}-openclaw-replicas`)!
        .Properties.LifecycleConfiguration.Rules;
      const overlay = rules.find((r: any) => r.Id === "TaskOverlays");
      expect(overlay).toEqual({
        Id: "TaskOverlays",
        ExpirationInDays: 14,
        Status: "Enabled",
        TagFilters: [{ Key: "lb-kind", Value: "task-overlay" }],
      });
      expect(rules.filter((r: any) => r.Prefix).map((r: any) => [r.Id, r.Prefix, r.ExpirationInDays])).toEqual([
        ["SessionsFree", "sessions/free/", 30],
        ["SessionsWorkspace", "sessions/workspace/", 365],
      ]);
    }
  });

  test("owns the platform ECS cluster the backend runs on", () => {
    template.hasResourceProperties("AWS::ECS::Cluster", {
      ClusterName: "lightbulb-dev-container",
    });
  });

  test("defines no task definition — nothing per-user runs on ECS any more", () => {
    template.resourceCountIs("AWS::ECS::TaskDefinition", 0);
  });

  test("keeps the Cloud Map namespace PaperclipStack registers into", () => {
    // paperclip-stack derives PAPERCLIP_INTERNAL_URL from this namespace name
    // and registers `paperclip.<namespace>` through cloudMapOptions. Deleting
    // it takes the Teams Inbox down; only the per-user SERVICE goes.
    template.hasResourceProperties("AWS::ServiceDiscovery::PrivateDnsNamespace", {
      Name: "lightbulb-dev.local",
    });
  });

  test("keeps the security group every AgentCore runtime ENI carries", () => {
    // Named for the retired per-user ECS task, but LIVE: service-stack threads it
    // to the backend as ECS_SECURITY_GROUP_ID, and hermes_provisioning puts it in
    // CreateAgentRuntime's networkConfiguration. It is NOT part of the follow-up
    // PR's delete set, and its export comes from that live reference, not a pin.
    template.hasResourceProperties("AWS::EC2::SecurityGroup", {
      GroupDescription: Match.stringLikeRegexp("Fargate container security group"),
    });
  });

  test("declares none of the retired per-user resources", () => {
    // The EFS filesystem, its security group, the two per-user ECS roles and the
    // per-user Cloud Map service went with the runtime. Two security groups are
    // left (ContainerSecurityGroup, asserted above, and the workspace browser's) and
    // one Cloud Map resource (the namespace Paperclip registers into).
    template.resourceCountIs("AWS::EFS::FileSystem", 0);
    template.resourceCountIs("AWS::EC2::SecurityGroup", 2);
    template.resourceCountIs("AWS::ServiceDiscovery::Service", 0);
    const roleNames = Object.values(template.findResources("AWS::IAM::Role")).map(
      (r: any) => r.Properties?.RoleName,
    );
    expect(roleNames).not.toContain("lightbulb-dev-openclaw-task");
    expect(roleNames).not.toContain("lightbulb-dev-openclaw-task-execution");
  });

  test("keeps the ECS task-state-change trail for the platform cluster", () => {
    // Scoped to the CLUSTER, not to the retired per-user services: the backend
    // service and the Paperclip service still run there,
    // and ECS drops stoppedReason after ~1h. This log group is the only durable
    // record of why one of them stopped.
    template.hasResourceProperties("AWS::Events::Rule", {
      Name: "lightbulb-dev-ecs-task-state-change",
    });
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: "/lightbulb/dev/ecs-task-events",
      RetentionInDays: 90,
    });
  });

  test("does NOT create the ECR repos (referenced by name, account-scoped)", () => {
    // The image repos are managed as existing AWS state outside any stack, to
    // avoid the "RETAIN orphan blocks recreate" trap.
    template.resourceCountIs("AWS::ECR::Repository", 0);
  });

  test("creates the image-builder OIDC role for CI image pushes", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "lightbulb-openclaw-image-builder",
      AssumeRolePolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "sts:AssumeRoleWithWebIdentity",
            Condition: Match.objectLike({
              StringLike: Match.absent(),
              StringEquals: Match.objectLike({
                "token.actions.githubusercontent.com:sub":
                  "repo:TheLightbulbCompany@253404738/lightbulb@1182791203:ref:refs/heads/main",
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                // The OpenClaw image and its workflow are deleted; a workflow
                // that can assume this role but has no ECR grant would fail on
                // push instead of at the door.
                "token.actions.githubusercontent.com:workflow": [
                  "build-paperclip-image",
                  "build-hermes-image",
                ],
              }),
            }),
          }),
        ]),
      }),
    });
  });

  test("builder role has ECR auth + push permissions", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "ecr:GetAuthorizationToken",
            Effect: "Allow",
            Resource: "*",
          }),
        ]),
      }),
    });
  });

  test("creates the per-owner state bucket every Hermes profile lives in", () => {
    // Named for litestream, which is a scar: `<owner>/hermes/<agent>/...` is
    // what it holds now. Renaming it strands every profile.
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "lightbulb-dev-openclaw-replicas",
    });
  });

  test("creates the OwnerBedrockBasePolicy managed policy", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "lightbulb-dev-owner-bedrock-base",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({ Sid: "HermesSecretsRead" }),
        ]),
      }),
    });
  });

  test("creates the OwnerBedrockBoundary permissions-boundary policy", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "lightbulb-dev-owner-bedrock-boundary",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:InvokeModel"]),
          }),
        ]),
      }),
    });
  });

  test("the execution role can pull the Hermes bridge image, and only that image", () => {
    // The per-owner role IS the AgentCore runtime's execution role, so it pulls
    // the image itself. The retired openclaw-gateway-arm64 grant is gone from
    // both the base policy and the boundary.
    for (const [name, sid] of [
      ["lightbulb-dev-owner-bedrock-base", "AgentCoreEcrPull"],
      ["lightbulb-dev-owner-bedrock-boundary", "BoundaryHermesEcrPull"],
    ]) {
      const statements = managedPolicy(template, name).Properties.PolicyDocument
        .Statement as any[];
      const pull = statements.find((s) => s.Sid === sid);
      expect(pull, `missing ${sid} on ${name}`).toBeDefined();
      const resources = JSON.stringify(pull.Resource);
      expect(resources).toContain("repository/lightbulb/hermes-agent-arm64");
      expect(resources).not.toContain("openclaw-gateway-arm64");
    }
  });

  test("neither the base policy nor the boundary grants EFS any more", () => {
    for (const name of [
      "lightbulb-dev-owner-bedrock-base",
      "lightbulb-dev-owner-bedrock-boundary",
    ]) {
      const statements = managedPolicy(template, name).Properties.PolicyDocument
        .Statement as any[];
      expect(JSON.stringify(statements)).not.toContain("elasticfilesystem:");
    }
  });

  test("base grants no logs or ssm; the boundary caps logs at AgentCore runtime groups", () => {
    const base = JSON.stringify(
      managedPolicy(template, "lightbulb-dev-owner-bedrock-base").Properties.PolicyDocument,
    );
    expect(base).not.toContain("logs:");
    expect(base).not.toContain("ssmmessages:");
    const boundary = managedPolicy(template, "lightbulb-dev-owner-bedrock-boundary").Properties
      .PolicyDocument.Statement as any[];
    expect(JSON.stringify(boundary)).not.toContain("ssmmessages:");
    const logs = boundary.find((s) => s.Sid === "BoundaryLogs");
    expect(JSON.stringify(logs.Resource)).toContain(
      "log-group:/aws/bedrock-agentcore/runtimes/*",
    );
    expect(logs.Resource).not.toBe("*");
  });

  test("owner base policy and boundary both allow publishing to the ws namespace", () => {
    for (const [name, sid] of [
      ["lightbulb-dev-owner-bedrock-base", "AppSyncWsPublish"],
      ["lightbulb-dev-owner-bedrock-boundary", "BoundaryAppSyncWsPublish"],
    ]) {
      const statements = managedPolicy(template, name).Properties.PolicyDocument
        .Statement as any[];
      const statement = statements.find((s) => s.Sid === sid);
      expect(statement.Action).toBe("appsync:EventPublish");
      expect(JSON.stringify(statement.Resource)).toContain(
        "apis/*/channelNamespace/ws",
      );
    }
  });

  test("boundary invoke statement carries all 9 FM ARNs + the AIP wildcard", () => {
    // Guards against FM_IDS/FAN_OUT_REGIONS drift between this stack and the
    // Python mint (core/billing/bedrock_aip.py). A region/model added on one
    // side but not the other would silently make the boundary too narrow
    // (AccessDenied on invoke) or too wide. 3 regions x 3 models = 9 ARNs.
    const FAN_OUT_REGIONS = ["us-east-1", "us-east-2", "us-west-2"];
    const FM_IDS = [
      "anthropic.claude-opus-4-6-v1",
      "anthropic.claude-sonnet-4-6",
      "anthropic.claude-haiku-4-5-20251001-v1:0",
    ];
    const expectedFmArns = FAN_OUT_REGIONS.flatMap((region) =>
      FM_IDS.map((model) => `arn:aws:bedrock:${region}::foundation-model/${model}`),
    );
    expect(expectedFmArns).toHaveLength(9);

    const boundary = managedPolicy(template, "lightbulb-dev-owner-bedrock-boundary");
    const invokeStatement = (
      boundary.Properties.PolicyDocument.Statement as any[]
    ).find((s) => JSON.stringify(s.Action).includes("bedrock:InvokeModel"));
    expect(invokeStatement).toBeDefined();

    const resources: string[] = invokeStatement.Resource;
    for (const arn of expectedFmArns) {
      expect(resources).toContain(arn);
    }
    expect(
      resources.some((r) => r.includes("application-inference-profile/*")),
    ).toBe(true);
  });

  test("every AgentCore* Sid in the base policy has a boundary twin with the same actions", () => {
    // #2688 added AgentCoreEcrPull/EcrToken/WorkloadToken to the base policy only, so the
    // boundary silently made them no-ops (effective perms = base ∩ boundary). This guards the
    // invariant that broke. AgentCoreEcrPull's twin is BoundaryHermesEcrPull: the boundary
    // names the one image a runtime pulls, so it is matched by ACTION, not by Sid suffix.
    const base = managedPolicy(template, "lightbulb-dev-owner-bedrock-base");
    const boundary = managedPolicy(template, "lightbulb-dev-owner-bedrock-boundary");
    const boundaryStatements = boundary.Properties.PolicyDocument.Statement as any[];
    const agentCoreBaseStatements = (
      base.Properties.PolicyDocument.Statement as any[]
    ).filter((s) => typeof s.Sid === "string" && s.Sid.startsWith("AgentCore"));
    expect(agentCoreBaseStatements.length).toBeGreaterThan(0);

    for (const baseStatement of agentCoreBaseStatements) {
      const twin = boundaryStatements.find(
        (s) =>
          JSON.stringify(s.Action) === JSON.stringify(baseStatement.Action) &&
          typeof s.Sid === "string" &&
          s.Sid.startsWith("Boundary"),
      );
      expect(twin, `no boundary twin for ${baseStatement.Sid}`).toBeDefined();
    }
  });

  test("boundary denies assume-into-our-account unconditionally and grants no sts:AssumeRole", () => {
    // The per-owner role has no AWS-rail job: the backend vends connector
    // credentials itself. The Deny must be unconditional (no founder carve-out)
    // and the broad AssumeRole ceiling must be gone.
    const stmts = managedPolicy(template, "lightbulb-dev-owner-bedrock-boundary")
      .Properties.PolicyDocument.Statement as any[];
    const deny = stmts.find((s) => s.Effect === "Deny");
    expect(deny).toBeDefined();
    expect(JSON.stringify(deny.Action)).toContain("sts:AssumeRole");
    expect(deny.Condition).toBeUndefined();

    const allowAssume = stmts.find(
      (s) => s.Effect === "Allow" && JSON.stringify(s.Action).includes("sts:AssumeRole"),
    );
    expect(allowAssume).toBeUndefined();
  });

});

describe("ContainerStack — prod", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("prod");
  });

  test("does NOT create the ECR repos (referenced by name from dev)", () => {
    template.resourceCountIs("AWS::ECR::Repository", 0);
  });

  test("defines no task definition", () => {
    template.resourceCountIs("AWS::ECS::TaskDefinition", 0);
  });

  test("declares none of the retired per-user resources", () => {
    // Same invariant as the dev case, asserted separately because prod is where
    // the RemovalPolicy asymmetry lived: ENV_CONFIG gives prod RETAIN, and the
    // filesystem used to inherit it.
    //
    // ⚠️ REMOVING A RESOURCE DOES NOT OVERRIDE THE DEPLOYED POLICY. CloudFormation
    // reads DeletionPolicy from the DEPLOYED template when a resource is absent
    // from the new one, so the flip to Delete had to ship while the filesystem
    // was still declared. It did: #2784 (d780e55a6) set the construct to DESTROY,
    // and the deployed prod template read Delete/Delete on 2026-09-19 before this
    // PR's dispatch. Precondition for the prod deploy of THIS PR: `cdk diff`
    // renders fs-07640461cc0f09b14 as `destroy`, never `orphan`. See Rollout.
    template.resourceCountIs("AWS::EFS::FileSystem", 0);
    template.resourceCountIs("AWS::EC2::SecurityGroup", 2); // ContainerSecurityGroup + the workspace browser's
    template.resourceCountIs("AWS::ServiceDiscovery::Service", 0);
    const roleNames = Object.values(template.findResources("AWS::IAM::Role")).map(
      (r: any) => r.Properties?.RoleName,
    );
    expect(roleNames).not.toContain("lightbulb-prod-openclaw-task");
    expect(roleNames).not.toContain("lightbulb-prod-openclaw-task-execution");
  });

  test("state bucket is RETAINed in prod (never auto-deleted)", () => {
    // The other half of the asymmetry: ENV_CONFIG's RETAIN still governs the
    // replicas bucket and MUST keep doing so — every Hermes profile lives in
    // it. A fix for the filesystem's DeletionPolicy that goes through
    // ENV_CONFIG would take this with it.
    template.hasResource("AWS::S3::Bucket", {
      Properties: Match.objectLike({ BucketName: "lightbulb-prod-openclaw-replicas" }),
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
    });
  });

  test("does NOT create the image-builder role (single account-wide role)", () => {
    const roles = template.findResources("AWS::IAM::Role");
    const builderRoles = Object.values(roles).filter(
      (r: unknown) =>
        (r as { Properties?: { RoleName?: string } }).Properties?.RoleName ===
        "lightbulb-openclaw-image-builder",
    );
    expect(builderRoles).toHaveLength(0);
  });
});

/**
 * Export names have to be asserted against a FULL stage synth, not an isolated
 * ContainerStack: every one of these is generated by a cross-stack reference
 * from -service or -observability, so none of them exists when this stack is
 * synthesized alone.
 */
describe("ContainerStack — CFN exports", () => {
  let template: Template;
  beforeAll(() => {
    const app = new cdk.App({ context: HOSTED_ZONE_CONTEXT });
    const stage = new LightbulbStage(app, "dev", {
      environment: "dev",
      env: { account: "877352799272", region: "us-east-1" },
    });
    const containerStack = stage.node.children.find(
      (c): c is cdk.Stack =>
        c instanceof cdk.Stack && c.stackName === "lightbulb-dev-container",
    );
    expect(containerStack).toBeDefined();
    template = Template.fromStack(containerStack!);
  });

  test("publishes exactly the eight exports its live consumers import", () => {
    // The eight transitional `exportValue` pins are gone, together with the EFS
    // filesystem, the EFS security group, the two per-user ECS roles and the
    // per-user Cloud Map service they named. What is left is exactly what
    // -service and -observability still import for real (the Cloud Map namespace
    // id went with PaperclipStack); every one of these is
    // generated by a live cross-stack reference, so a name here that no longer
    // appears means a consumer was dropped, and a name added means a new one.
    //
    // The exact names, not a count or a fragment match: an export name is a hash
    // of the construct path, so one that drifts is a deploy that fails on the
    // real stack and on nothing else.
    const exports = Object.values((template.toJSON() as any).Outputs ?? {})
      .flatMap((o: any) => (o.Export ? [o.Export.Name as string] : []))
      .sort();
    expect(exports).toEqual([
      "lightbulb-dev-container:ExportsOutputFnGetAttClusterEB0386A7Arn2F2E3C3F",
      "lightbulb-dev-container:ExportsOutputFnGetAttContainerSecurityGroup38DC7A88GroupId12565082",
      // The sessions memory (the evaluator's): -service names it in the backend env and grant.
      "lightbulb-dev-container:ExportsOutputFnGetAttSessionsMemoryMemoryIdDC40DD83",
      // The workspace browser (VPC mode): -service names it in the backend env and the grants.
      "lightbulb-dev-container:ExportsOutputFnGetAttWorkspaceBrowserBrowserId09016DCA",
      // Workspace memory (Track G): -service stamps the id into the backend env and derives the
      // wipe grant's ARN from it -- one export, not two.
      "lightbulb-dev-container:ExportsOutputFnGetAttWorkspaceMemoryMemoryIdFA6E3207",
      "lightbulb-dev-container:ExportsOutputRefClusterEB0386A796A0E3FE",
      "lightbulb-dev-container:ExportsOutputRefOwnerBedrockBasePolicy975142B0B0F2F85F",
      "lightbulb-dev-container:ExportsOutputRefOwnerBedrockBoundary85EE08FFB4B914DE",
    ]);
  });
});
