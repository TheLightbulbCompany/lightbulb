import * as path from "path";
import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatch_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as sns from "aws-cdk-lib/aws-sns";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import {
  allTableArns,
  bothTableArns,
  legacyTablePrefix,
  alertTopicArn,
  resourcePrefix,
  tableName,
  tablePrefix,
} from "../table-names";
import { Construct } from "constructs";
import { LEGACY_MAIL_DOMAIN, PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN } from "../domains";
import { BROWSER_EGRESS_PROXY_PORT } from "./container-stack";

// openclaw-version.json is the single source of truth for the pinned OpenClaw
// version + per-env extended-image tags. The backend image's Docker build
// context is apps/backend/, so the file (at the repo root) can't be COPY-d into
// the image; instead we read it here at synth time and inject the values as
// container env vars, which core/openclaw_version.py reads at runtime. Mirrors
// the read in container-stack.ts — bump openclaw-version.json only.
interface OpenClawVersionConfig {
  tag: string;
  dev: { tag: string };
  prod: { tag: string };
  [key: string]: unknown;
}

const OPENCLAW_VERSION: OpenClawVersionConfig = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "..", "..", "..", "..", "openclaw-version.json"),
    "utf8",
  ),
);

/**
 * Secret name strings (NOT ISecret objects) to avoid cross-stack dependency
 * cycles. ServiceStack imports them locally via fromSecretNameV2, which
 * prevents CDK from auto-granting KMS access on AuthStack's key.
 */
export interface SecretNames {
  clerkIssuer: string;
  clerkSecretKey: string;
  /**
   * Svix signing secret for the Clerk webhook endpoint. This IS the auth on
   * /webhooks/clerk — the backend rejects deliveries it cannot verify.
   */
  clerkWebhookSecret: string;
  stripeSecretKey: string;
  stripeWebhookSecret: string;
  encryptionKey: string;
  /** PostHog personal API key for admin Activity tab. Empty string stubs the endpoint gracefully. */
  posthogProjectApiKey: string;
  /**
   * Symmetric secret used by the FastAPI Paperclip proxy router to sign
   * service-token JWTs forwarded to the Paperclip server (Better Auth
   * verifies them). Must match the secret name AuthStack creates in the
   * same env. Encrypted under the same CMK as the other auth secrets, so
   * the existing `KmsDecryptForSecrets` policy already covers it.
   */
  paperclipServiceTokenKey: string;
  /**
   * Bearer secret the isol8.notifications Paperclip plugin attaches to
   * POST /webhooks/paperclip-events. Same CMK, so KmsDecryptForSecrets
   * covers it.
   */
  paperclipEventsWebhookSecret: string;
  /**
   * Shared password for the gog CLI file-keyring backend. The backend
   * materializes the owner's Google OAuth keyring on EFS by running gog with
   * this password; the same secret is injected into every per-user OpenClaw
   * container. Must match the secret name AuthStack creates in the same env
   * and is encrypted under the same CMK, so `KmsDecryptForSecrets` covers it.
   */
  gogKeyringPassword: string;
  /** Metronome API bearer token (sandbox/prod by token). Read by the lifecycle/gating modules. */
  metronomeApiKey: string;
  /** HMAC key verifying inbound Metronome webhook signatures. */
  metronomeWebhookSecret: string;
  /**
   * Shared secret the WebSocket Lambdas attach to authenticate themselves to
   * the backend /api/v1/ws/* endpoints. Same secret AuthStack creates and the
   * WS Lambdas read; same CMK as the other auth secrets, so `KmsDecryptForSecrets`
   * already covers it. Enforcement is gated by WS_ENFORCE_INTERNAL_SECRET (a
   * plain env, default "false") so the secret can be wired to both sides before
   * the backend starts rejecting callers that lack the header.
   */
  wsInternalSecret: string;
  /**
   * Shared secret the SES-ingest Lambda (ses-inbound-stack.ts) attaches to
   * its POST /webhooks/ses-inbound calls. Created unconditionally in every
   * env (same shape as wsInternalSecret), but only meaningful once the
   * prod-stage SesInboundStack actually exists (prod-only). Same CMK as the
   * other auth secrets, so KmsDecryptForSecrets already covers decrypt.
   */
  sesIngestSecret: string;
}

export interface ServiceStackProps extends cdk.StackProps {
  environment: string;
  /** Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN. */
  rootDomain?: string;
  /**
   * Domain the platform sender + agent mail live on (see lib/domains.ts).
   * Defaults to PLATFORM_MAIL_DOMAIN. This is the SES domain identity the
   * backend send policy is scoped to — not the product domain.
   */
  platformMailDomain?: string;
  vpc: ec2.IVpc;
  targetGroup: elbv2.IApplicationTargetGroup;
  albSecurityGroup: ec2.ISecurityGroup;
    // NOTE: this stack deliberately takes NO database table constructs.
    // Every table is reached by NAME (see the DynamoDbOwnedTables statement and
    // the tableName() helper). Taking a construct would create a cross-stack
    // Export, and CloudFormation cannot change an exported value while anything
    // imports it — the deadlock that blocked the table rename.
  /** Pass secret names (strings) to avoid cross-stack KMS auto-grant cycles. */
  secretNames: SecretNames;
  /** Pass as string ARN to avoid cross-stack dependency cycle. */
  kmsKeyArn: string;
  container: {
    cluster: ecs.ICluster;
    cloudMapNamespace: servicediscovery.IPrivateDnsNamespace;
    cloudMapService: servicediscovery.IService;
    efsFileSystem: efs.IFileSystem;
    efsSecurityGroup: ec2.ISecurityGroup;
    containerSecurityGroup: ec2.ISecurityGroup;
    taskExecutionRole: iam.IRole;
    taskRole: iam.IRole;
    openclawTaskDef: ecs.ITaskDefinition;
    // Per-owner Bedrock enforcement (Plan 2): the backend reads these ARNs to
    // condition iam:CreateRole on the boundary and to attach the base policy to
    // every minted per-owner role. Live construct refs (same CDK stage), NOT
    // CfnOutput/ImportValue — matching how taskRole is already passed through.
    ownerBedrockBasePolicy: iam.IManagedPolicy;
    ownerBedrockBoundary: iam.IManagedPolicy;
  };
  managementApiUrl: string;
  /** Public wss:// endpoint returned by /api/v1/desktop/pair so desktop clients
   * know where to connect. Sourced from ApiStack.webSocketUrl. */
  webSocketUrl: string;
  connectionsTableName: string;
  wsApiId: string;
  wsStage: string;
  /**
   * Agent-email base domain, "isol8.co" in prod (set in lockstep with the
   * prod-only SesInboundStack — see lightbulb-stage.ts). Empty/undefined in every
   * other env; the backend's AGENT_EMAIL_DOMAIN guards treat that as "agent
   * email off", keeping dev/local/CI inert.
   */
  agentEmailDomain?: string;
  /**
   * SES configuration set every agent-mail send is stamped with; empty ⇒
   * unstamped. Sourced from the AgentMailOutboundStack instantiated in
   * lockstep with agentEmailDomain (see lightbulb-stage.ts) — undefined in any
   * env/deploy where that stack isn't instantiated.
   */
  agentMailConfigurationSet?: string;
  /**
   * Connect platform client id (ca_…) for the Standard-OAuth "connect an
   * existing Stripe account" flow. Public, not a secret — see
   * STRIPE_CONNECT_CLIENT_ID_BY_ENV in isol8-stage.ts. Undefined/empty until
   * the owner fills it in from Stripe → Connect settings; the backend setting
   * defaults to "" so an unconfigured env just can't start that flow yet.
   */
  stripeConnectClientId?: string;
  /**
   * Provider catalog partition for the Stripe Projects provisioning rail:
   * `prod | dev | testing`. Dev reads/writes Stripe's `testing` partition,
   * prod the real one. NOT a payment-mode switch — Stripe's own caveat:
   * "Selecting a DEV or TESTING catalog doesn't mean 'no real charges.'"
   * Defaults to `prod`, the safe value for an env that forgot to set it:
   * reading one partition while writing another provisions into a catalog
   * the owner never saw.
   */
  stripeProjectsCatalog?: string;
  /**
   * Stripe's recommended Provisioning onboarding path: request the Accounts v2
   * `developer` configuration with the `projects` capability on the company
   * account. DEFAULT FALSE — `configuration.developer` is an "Unknown field"
   * for our platform today (probed 2026-08-29); sending it breaks every
   * company-account create. Flip once Stripe enables the field.
   */
  stripeProjectsDeveloperConfig?: boolean;
  /**
   * The Accounts v2 API version pinned on the company account/account-link
   * calls (`Stripe-Version` header). Empty ⇒ the SDK's own default, i.e.
   * today's behaviour. Set once Stripe names the version during preview
   * onboarding for stripeProjectsDeveloperConfig.
   */
  stripeAccountsV2Version?: string;
}

/**
 * Tag value accepted by the owner-AIP IAM fence.
 *
 * The `ManagedBy` tag gates every mutate action on an owner's Bedrock
 * application inference profile, so flipping the writer and this condition
 * TOGETHER would AccessDeny the backend from every EXISTING profile -- Bedrock
 * inference dies fleet-wide, free tier included, while newly minted profiles
 * keep working and mask it.
 *
 * That is why the rename ran as four separately reversible steps: widen this to
 * accept both values -> re-tag the existing profiles -> flip the writer ->
 * narrow back to the new value alone. All four are now DONE; this is the
 * narrowed end state, verified against the live account first (37/37 owner
 * profiles on `lightbulb-backend`, 0 on the old value; the 2 untagged ones are
 * the idempotency probes, which this condition does not gate).
 *
 * If you ever need to move it again, widen FIRST. Never flip writer+condition
 * in one deploy.
 */
const MANAGED_BY_ACCEPTED = ["lightbulb-backend"];

export class ServiceStack extends cdk.Stack {
  public readonly service: ecs.FargateService;
  public readonly taskRole: iam.Role;

  constructor(scope: Construct, id: string, props: ServiceStackProps) {
    super(scope, id, props);

    const env = props.environment;
    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    const platformMailDomain = props.platformMailDomain ?? PLATFORM_MAIL_DOMAIN;
    // Every SES identity the backend may send under. This was two while the
    // mail-domain move was in flight, because `resolve_from_address`
    // (core/services/agent_email.py) returns whatever the DynamoDB address book
    // holds and pre-move addresses were still on the legacy domain. The re-key
    // has run in both envs and no address remains there, so this is now one.
    //
    // The filter is load-bearing, not defensive: LEGACY_MAIL_DOMAIN is now ""
    // and an unguarded spread would emit `identity/` -- a malformed ARN in the
    // task role's ses:SendRawEmail resource list.
    const sendIdentityArns = [
      platformMailDomain,
      ...(LEGACY_MAIL_DOMAIN && LEGACY_MAIL_DOMAIN !== platformMailDomain
        ? [LEGACY_MAIL_DOMAIN]
        : []),
    ].map((d) => `arn:aws:ses:${this.region}:${this.account}:identity/${d}`);

    // -------------------------------------------------------------------------
    // Docker Image Asset (built & pushed by CDK Pipeline)
    // -------------------------------------------------------------------------
    const backendImage = new DockerImageAsset(this, "BackendImage", {
      directory: path.join(__dirname, "..", "..", "..", "backend"),
      platform: Platform.LINUX_AMD64,
    });

    // -------------------------------------------------------------------------
    // Service Security Group
    // -------------------------------------------------------------------------
    const serviceSg = new ec2.SecurityGroup(this, "ServiceSecurityGroup", {
      vpc: props.vpc,
      description: `Isol8 ${env} Fargate service security group`,
      allowAllOutbound: true,
    });

    // Allow traffic from ALB on port 8000
    serviceSg.addIngressRule(
      props.albSecurityGroup,
      ec2.Port.tcp(8000),
      "HTTP from ALB",
    );

    // Cross-stack security group ingress rules.
    // We use CfnSecurityGroupIngress to avoid circular dependencies between stacks.

    // Allow service to mount EFS (port 2049)
    new ec2.CfnSecurityGroupIngress(this, "EfsFromServiceIngress", {
      groupId: props.container.efsSecurityGroup.securityGroupId,
      ipProtocol: "tcp",
      fromPort: 2049,
      toPort: 2049,
      sourceSecurityGroupId: serviceSg.securityGroupId,
      description: "Allow NFS from Fargate service",
    });

    // Allow service to manage Fargate containers (all TCP)
    new ec2.CfnSecurityGroupIngress(this, "ContainerFromServiceIngress", {
      groupId: props.container.containerSecurityGroup.securityGroupId,
      ipProtocol: "tcp",
      fromPort: 0,
      toPort: 65535,
      sourceSecurityGroupId: serviceSg.securityGroupId,
      description: "Allow all TCP from Fargate service for container management",
    });

    // -------------------------------------------------------------------------
    // IAM Task Role
    // -------------------------------------------------------------------------
    this.taskRole = new iam.Role(this, "TaskRole", {
      roleName: `lightbulb-${env}-backend-task`,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Lightbulb ${env} Fargate task role`,
    });

    // ECR pull + DescribeImages (admin dropdown of available openclaw-extended tags)
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EcrAccess",
        actions: [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:DescribeImages",
        ],
        resources: ["*"],
      }),
    );

    // Replica lifecycle: the backend owns the S3 replica prefix's lifecycle —
    // wipe/reprovision and agent-delete flows must prefix-delete
    // s3://isol8-{env}-openclaw-replicas/{owner}/..., or the next boot's
    // restore resurrects pre-wipe state (sessions, device trust, auth rows
    // with live OAuth tokens). List+Delete only across the bucket — the
    // backend's sole READ is the statement below.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "LitestreamReplicaLifecycle",
        actions: ["s3:ListBucket"],
        resources: [`arn:aws:s3:::lightbulb-${env}-openclaw-replicas`],
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "LitestreamReplicaDelete",
        actions: ["s3:DeleteObject"],
        resources: [`arn:aws:s3:::lightbulb-${env}-openclaw-replicas/*`],
      }),
    );
    // The one read path: the OAuth seed harvest litestream-restores the
    // DEFAULT agent's auth-store DB right before the prefix delete, to copy
    // the rotated ChatGPT refresh token back to DynamoDB
    // (oauth_service.harvest_rotated_oauth_seed) — without it, every
    // wipe-and-reprovision re-stages a spent seed. Scoped to that single
    // database's key shape (<owner>/agents/main/agent/openclaw-agent.sqlite/
    // <level>/<txid>.ltx), so the backend cannot read session/state DBs or
    // any non-default agent's replica. It still spans owners: the shared
    // backend role serves every tenant, the same floor as the delete grant
    // and the DynamoDB grants — per-tenant narrowing would need per-tenant
    // roles, which this service does not have.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "LitestreamReplicaAuthDbRead",
        actions: ["s3:GetObject"],
        resources: [`arn:aws:s3:::lightbulb-${env}-openclaw-replicas/*/agents/main/agent/openclaw-agent.sqlite/*`],
      }),
    );

    // ECS management (per-user Fargate tasks)
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EcsManagement",
        actions: [
          "ecs:CreateService",
          "ecs:UpdateService",
          "ecs:DeleteService",
          "ecs:DescribeServices",
          "ecs:DescribeTasks",
          "ecs:ListServices",
          "ecs:ListTasks",
          "ecs:ExecuteCommand",
        ],
        resources: ["*"],
        conditions: {
          ArnEquals: {
            "ecs:cluster": props.container.cluster.clusterArn,
          },
        },
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EcsTaskDefinition",
        actions: [
          "ecs:RegisterTaskDefinition",
          "ecs:DeregisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
        ],
        resources: ["*"],
      }),
    );

    // IAM PassRole for ECS task roles (shared execution + task roles AND the
    // per-owner Bedrock roles the backend mints at provision time). Without
    // the owner-bedrock-* prefix, RegisterTaskDefinition/CreateService with a
    // per-owner taskRoleArn fails AccessDenied on PassRole.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IamPassRole",
        actions: ["iam:PassRole"],
        resources: [
          props.container.taskExecutionRole.roleArn,
          props.container.taskRole.roleArn,
          `arn:aws:iam::${this.account}:role/lightbulb-${env}-owner-bedrock-*`,
        ],
        conditions: {
          StringEquals: {
            "iam:PassedToService": "ecs-tasks.amazonaws.com",
          },
        },
      }),
    );

    // Self-account trust-pin gate (aws-sts scope tiers, Task 5): the backend
    // reads the trust policy of a pasted our-account connector role before
    // trusting it. Scoped to the single fixed role name CFN mints for this
    // path — not a prefix wildcard like the owner-bedrock-* roles above.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ReadConnectorRoleForTrustCheck",
        actions: ["iam:GetRole"],
        // Canonical connector role name = bedrock_aip.CONNECTOR_ROLE_NAME. Must
        // match the CFN template RoleName and the backend constant; renaming
        // any one silently breaks the self-account trust-pin check (GetRole
        // scoped to the old name → AccessDenied → fail-closed 400).
        resources: [`arn:aws:iam::${this.account}:role/lightbulb-connector`],
      }),
    );

    // Backend mints + scopes + kills per-owner Bedrock roles (Plan 2). These
    // three statements live in a dedicated ManagedPolicy attached to the task
    // role, NOT in the inline default policy: the backend role's inline policy
    // is already near CloudFormation's 10 KB inline-policy limit, and adding
    // these inline would tip CDK into spilling statements (e.g. the by-name
    // DynamoDbIntegrations grant) into an overflow ManagedPolicy — silently
    // relocating them out of the AWS::IAM::Policy resource other features rely
    // on. A standalone, explicitly named ManagedPolicy keeps the inline policy
    // stable and is more auditable. CreateRole is boundary-conditioned so a
    // backend that can CreateRole+PutRolePolicy cannot escalate beyond the
    // OwnerBedrockBoundary (Bedrock+EFS+logs+ssm).
    new iam.ManagedPolicy(this, "BackendOwnerBedrockManage", {
      managedPolicyName: `lightbulb-${env}-backend-owner-bedrock-manage`,
      roles: [this.taskRole],
      statements: [
        new iam.PolicyStatement({
          sid: "OwnerBedrockRoleManage",
          actions: [
            "iam:PutRolePolicy",
            "iam:DeleteRolePolicy",
            "iam:GetRolePolicy",
            "iam:AttachRolePolicy",
            "iam:DetachRolePolicy",
            // delete_owner_bedrock_role now detaches by ENUMERATION rather than
            // by naming an ARN from settings, so teardown can see an attachment
            // made under a previous policy ARN. Without this the enumeration
            // AccessDenies and the role can never be deleted.
            "iam:ListAttachedRolePolicies",
            "iam:TagRole",
            "iam:GetRole",
            // Lets ensure_owner_bedrock_role converge a previously-minted role's
            // trust policy onto the SourceAccount/SourceArn-conditioned form.
            "iam:UpdateAssumeRolePolicy",
            // Account-wipe tool (Task 5): lets bedrock_aip.delete_owner_bedrock_role
            // remove the per-owner role once its inline policy is deleted and the
            // base managed policy is detached (IAM refuses DeleteRole otherwise).
            // Same owner-bedrock-* resource scope as the rest of this statement —
            // never touches the shared base managed policy itself.
            "iam:DeleteRole",
          ],
          resources: [
            `arn:aws:iam::${this.account}:role/lightbulb-${env}-owner-bedrock-*`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "OwnerBedrockRoleCreate",
          actions: ["iam:CreateRole"],
          resources: [
            `arn:aws:iam::${this.account}:role/lightbulb-${env}-owner-bedrock-*`,
          ],
          conditions: {
            StringEquals: {
              "iam:PermissionsBoundary":
                props.container.ownerBedrockBoundary.managedPolicyArn,
            },
          },
        }),
        // Bedrock control plane. AIP ARNs are created at runtime so cannot be
        // pre-scoped by ARN; we constrain by tag instead. The mint stamps every
        // AIP with a ManagedBy tag (see bedrock_aip.ensure_owner_aips), so:
        //   - create + tag-on-create are gated on aws:RequestTag/ManagedBy, so
        //     the backend can only mint profiles it marks as its own;
        //   - delete/get/untag/list-tags are gated on aws:ResourceTag/ManagedBy,
        //     so the backend can only act on profiles it minted;
        //   - ListInferenceProfiles is a collection-level read with no resource
        //     to tag, so it stays unconditioned (read-only; leaks no access).
        // The actions are split across three statements because not every
        // action supports the same condition key (RequestTag applies only to
        // create/tag; ResourceTag applies only to actions that name a resource;
        // List supports neither).
        new iam.PolicyStatement({
          sid: "OwnerBedrockProfileCreate",
          actions: [
            "bedrock:CreateInferenceProfile",
            "bedrock:TagResource",
          ],
          resources: ["*"],
          conditions: {
            StringEquals: {
              "aws:RequestTag/ManagedBy": MANAGED_BY_ACCEPTED,
            },
          },
        }),
        new iam.PolicyStatement({
          sid: "OwnerBedrockProfileMutate",
          actions: [
            "bedrock:DeleteInferenceProfile",
            "bedrock:GetInferenceProfile",
            "bedrock:UntagResource",
            "bedrock:ListTagsForResource",
          ],
          resources: ["*"],
          conditions: {
            StringEquals: {
              "aws:ResourceTag/ManagedBy": MANAGED_BY_ACCEPTED,
            },
          },
        }),
        new iam.PolicyStatement({
          sid: "OwnerBedrockProfileList",
          actions: ["bedrock:ListInferenceProfiles"],
          resources: ["*"],
        }),
      ],
    });

    // Agent-email outbound send (SES). Standalone ManagedPolicy for the SAME
    // 10 KB inline-policy-ceiling reason as BackendOwnerBedrockManage above.
    // Scoped to the platform mail domain identity (plus the legacy one, see
    // sendIdentityArns above) — SES authorizes sending from ANY address under
    // a verified domain identity, including every company's
    // `{agent}@{slug}.{mailDomain}` subdomain address, so one identity ARN per
    // domain covers the whole fleet. Safe to grant unconditionally in
    // every env (dev included): the identity itself only exists once the
    // prod-stage SesInboundStack is deployed (prod-only), and sends are
    // separately gated by AGENT_EMAIL_DOMAIN being set (core/services/ses_email.py).
    new iam.ManagedPolicy(this, "BackendSesSend", {
      managedPolicyName: `lightbulb-${env}-backend-ses-send`,
      roles: [this.taskRole],
      statements: [
        new iam.PolicyStatement({
          sid: "AgentMailSesSend",
          actions: ["ses:SendEmail", "ses:SendRawEmail"],
          resources: sendIdentityArns,
        }),
        // Per-owner SES tenants are created lazily by the backend at first
        // send (core/services/agent_mail_tenant.py) — dynamic per owner, so
        // not declared as CDK resources. Scoped to this env's configuration
        // set + the shared domain identity, same posture as the statement
        // above. Tenant names are deterministic `owner-{owner_id}` (see
        // agent_mail_tenant.py), so `tenant/owner-*` is tight instead of a
        // bare wildcard; the real ARN shape is `tenant/{name}/{tn-id}`, which
        // `owner-*` still matches. ses:UpdateReputationEntityPolicy
        // authorizes against BOTH the tenant resource AND a
        // `reputation-policy` resource in the AWS-owned `aws` account (SES's
        // managed-policy ARN convention — not a typo).
        new iam.PolicyStatement({
          sid: "AgentMailTenants",
          actions: [
            "ses:CreateTenant",
            "ses:GetTenant",
            "ses:CreateTenantResourceAssociation",
            "ses:UpdateReputationEntityPolicy",
            "ses:GetReputationEntity",
            "ses:SendEmail",
            "ses:SendRawEmail",
          ],
          resources: [
            `arn:aws:ses:${this.region}:${this.account}:tenant/owner-*`,
            `arn:aws:ses:${this.region}:aws:reputation-policy/standard`,
            `arn:aws:ses:${this.region}:${this.account}:configuration-set/lightbulb-${env}-agent-mail`,
            ...sendIdentityArns,
          ],
        }),
      ],
    });

    // Secrets Manager
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SecretsAccess",
        actions: [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/*`,
        ],
      }),
    );

    // Bootstrap admin credentials write (Paperclip service account):
    // apps/backend/scripts/bootstrap_paperclip_admin.py is invoked via
    // `aws ecs execute-command` against the backend task. It signs up
    // the bootstrap admin email (see ADMIN_EMAIL constant in the
    // script) in Paperclip, then calls PutSecretValue on
    // isol8/{env}/paperclip_admin_credentials. Without this scoped
    // grant the put fails with AccessDeniedException after the
    // Paperclip user is already created — leaves an inconsistent
    // bootstrap state (user exists but credentials not persisted, so
    // re-running fails with "user already exists").
    // Read of this secret is already covered by the wildcard
    // SecretsAccess + KmsDecryptForSecrets policies.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "PaperclipAdminCredentialsWrite",
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:PutSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/paperclip_admin_credentials-*`,
        ],
      }),
    );

    // Account-wipe (Task 7 fix): the in-backend teardown deletes the owner's
    // Paperclip Better Auth "user" row directly via the RDS Data API
    // (core/services/paperclip_user_delete.py) because Paperclip exposes no
    // user-delete API. Three grants the task role otherwise lacks:
    //   - rds-data:ExecuteStatement on the paperclip Aurora cluster;
    //   - secretsmanager:GetSecretValue on the DASH-named DB-credentials secret
    //     (the wildcard SecretsAccess above only covers SLASH-named isol8/{env}/*
    //     secrets, NOT isol8-{env}-paperclip-db-credentials);
    //   - secretsmanager:ListSecrets (metadata only; the helper paginates to find
    //     that secret's full ARN — ListSecrets has no resource-level scoping, so
    //     it must be "*").
    // Without these the Aurora-user step 403s on every real account and orphans
    // the Better Auth identity (Teams 401 on re-signup with the same email).
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AccountWipePaperclipAuroraExec",
        effect: iam.Effect.ALLOW,
        actions: ["rds-data:ExecuteStatement"],
        resources: [
          `arn:aws:rds:${this.region}:${this.account}:cluster:lightbulb-${env}-paperclip-db`,
        ],
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AccountWipePaperclipDbSecretRead",
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb-${env}-paperclip-db-credentials-*`,
        ],
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AccountWipeListSecrets",
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:ListSecrets"],
        resources: ["*"],
      }),
    );

    // Per-user LLM-key secrets (Plan 2 Task 10): backend creates/updates/reads
    // a Secrets Manager secret per user under isol8/{env}/user-keys/*. Scoped
    // tightly to that prefix so the broad SecretsAccess policy above is not
    // implicitly widened to write access on all isol8/{env}/* secrets.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "UserKeysSecretsRW",
        effect: iam.Effect.ALLOW,
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/user-keys/*`,
        ],
      }),
    );

    // Per-owner integration credential secrets (credential-layer redesign):
    // paste-key tokens and backend-custodian OAuth credentials live under
    // isol8/{env}/integrations/*. The secret-free integrations DDB index points
    // here. Same tight prefix scoping as the user-keys statement above.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IntegrationSecretsRW",
        effect: iam.Effect.ALLOW,
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/integrations/*`,
        ],
      }),
    );

    // Per-agent Paperclip API keys (credential-layer redesign): minted by the
    // backend and stored under isol8/{env}/paperclip-agent-keys/* so Friday's
    // openclaw.json MCP block can authenticate to Paperclip. Replaces the old
    // encrypted-in-DynamoDB api-keys storage. Same tight prefix scoping.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "PaperclipAgentKeysRW",
        effect: iam.Effect.ALLOW,
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/paperclip-agent-keys/*`,
        ],
      }),
    );

    // DynamoDB tables
    // Local handle for the Step Functions DynamoDB tasks below. fromTableName
    // resolves a NAME into an ITable without touching the database stack's
    // construct, so it creates no cross-stack export — passing
    // props.database.containersTable directly would reintroduce exactly the
    // coupling this decouple removes.
    const containersTableRef = dynamodb.Table.fromTableName(
      this,
      "ContainersTableByName",
      tableName(env, "containers"),
    );

    // DynamoDB tables — granted by NAME across the env-prefixed namespace,
    // NOT via grantReadWriteData. Same decouple the credential tables already
    // had (credential-export-decouple.test.ts, "the 2026-05-31 incident"),
    // applied to the rest: grant*() on another stack's table creates a
    // cross-stack Export, and CloudFormation cannot change an exported value
    // while anything imports it —
    //
    //   Cannot update export isol8-dev-database:ExportsOutputRefUsageCounters…
    //   as it is in use by isol8-dev-observability
    //
    // which is precisely how the first table-rename attempt failed. No export,
    // no coupling, and the tables become renameable.
    //
    // One wildcard pair rather than 22 enumerated tables because the policy is
    // size-bound: ~8.9KB of IAM's 10,240-byte inline limit, and enumerating
    // would overrun it. See allTableArns.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "DynamoDbOwnedTables",
        actions: [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem",
          "dynamodb:ConditionCheckItem",
          "dynamodb:DescribeTable",
        ],
        resources: allTableArns(this.region, this.account, env),
      }),
    );
    // The credential-layer integration tables are granted by NAME, not via
    // grantReadWriteData. grantReadWriteData would export each table's ARN from
    // the database stack and import it here, creating a cross-stack export. An
    // export that another stack imports cannot be deleted, which permanently
    // blocks renaming or replacing the table. Granting by the table name avoids
    // the export entirely. The action set matches what integrations_repo and
    // oauth_state_repo actually call.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "DynamoDbIntegrations",
        actions: [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ],
        resources: [
          ...bothTableArns(this.region, this.account, env, "integrations"),
          ...bothTableArns(this.region, this.account, env, "oauth-state"),
          // Per-owner AIP reverse index (Plan 2). Folded into this by-name DDB
          // statement (identical action set + condition-less shape) rather than
          // added as a separate fourth statement: the backend role's inline
          // policy is near CloudFormation's 10 KB limit, so a separate statement
          // risks tipping CDK into an overflow ManagedPolicy that would relocate
          // this very statement out of the AWS::IAM::Policy resource other
          // features assert on. Same cross-stack-export-avoidance posture; the
          // grant (GetItem/PutItem/UpdateItem/DeleteItem/Query) is identical.
          ...bothTableArns(this.region, this.account, env, "bedrock-aip-owners"),
          // Metronome local fast-gate flag (Phase 2). Folded into this by-name
          // statement for the SAME reason as bedrock-aip-owners above: a separate
          // grantReadWriteData would both bloat the near-10 KB inline policy
          // (tipping CDK into an overflow ManagedPolicy that relocates this very
          // statement) AND export the table ARN cross-stack. The action set
          // (GetItem/PutItem/UpdateItem/Query) matches MetronomeFastGate +
          // metronome_provisioning + metronome_funding.
          ...bothTableArns(this.region, this.account, env, "metronome-fast-gate"),
          // bedrock-gate bearer-token table. mint_gate_token / deny_gate_token
          // (core/billing/bedrock_gate_tokens.py) PutItem/UpdateItem/DeleteItem
          // here for EVERY Bedrock owner at provision time, so without this grant
          // Bedrock provisioning AccessDenies fleet-wide. Folded into this
          // by-name statement for the SAME 10 KB-inline-policy reason as the two
          // tables above (a separate grantReadWriteData would tip CDK into an
          // overflow ManagedPolicy). The table itself is defined in the
          // bedrock-gate stack (gate-private), so only the ARN is referenced.
          ...bothTableArns(this.region, this.account, env, "bedrock-gate-tokens"),
        ],
      }),
    );
    // Webhook dedup helper does conditional PutItem with attribute_not_exists;
    // never reads. Write-only grant is sufficient.
    // Paperclip integration: the proxy router (paperclip_proxy.py:388,
    // routers/webhooks.py:146) and the daily purge worker
    // (services/update_service.py:198) all read/write this table to look up
    // each user's Paperclip company row and decrypt the per-user Better Auth
    // password. Without this grant the proxy 500s on every Teams click with
    // an AccessDeniedException, even when Clerk auth succeeds.

    // Marketplace tables — backend needs read/write on listings, purchases,
    // payout-accounts, takedowns. Browse + search are served by the in-process
    // search service (60s-TTL DDB scan), so the same listings grant covers it.

    // Bedrock
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BedrockInvoke",
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
          "arn:aws:bedrock:*:*:inference-profile/*",
        ],
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BedrockList",
        actions: [
          "bedrock:ListFoundationModels",
          "bedrock:ListInferenceProfiles",
        ],
        resources: ["*"],
      }),
    );

    // bedrock-gate URL discovery: the gate stack publishes each env's Function
    // URL to /lightbulb/{env}/bedrock-gate-url and the backend reads it at runtime
    // (no cross-stack prop threading). The parameter doubles as the fleet
    // break-glass lever — value "direct" routes new provisions straight to
    // bedrock-runtime. Single-parameter read-only grant; kept as its own tiny
    // statement (NOT folded into DynamoDbIntegrations).
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BedrockGateUrlParamRead",
        actions: ["ssm:GetParameter"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter/lightbulb/${env}/bedrock-gate-url`,
        ],
      }),
    );

    // Platform-unique company slugs (company-identity Task 1). Deliberately
    // granted here — AFTER the bedrock-gate-url statement above, not beside
    // the other table grants earlier in this method — because CDK's inline-
    // policy overflow packs statements into TaskRoleDefaultPolicy in
    // add-order and spills whatever doesn't fit into a TaskRoleOverflowPolicy
    // once the ~10 KB ceiling is hit (confirmed via synth: this stack is
    // already at that ceiling). Adding this grant any earlier pushed
    // BedrockGateUrlParamRead itself into the overflow policy, which broke
    // test/service-stack.test.ts's assertion that it lives in the inline
    // AWS::IAM::Policy. Placing new grants after that statement means any
    // overflow lands on the new grant instead, leaving already-tested
    // statements' packing undisturbed. Functionally identical either way —
    // an overflow ManagedPolicy still attaches to the same role.

    // Provisioned agent email addresses (agent-email-foundation Task 3).
    // Placed immediately after companySlugsTable per the overflow-packing
    // rationale in the comment above — any new IAM-size overflow should land
    // on this grant, not push an earlier, already-tested statement into the
    // TaskRoleOverflowPolicy.

    // Stored inbound agent mail — read by the inbox route, written by ses_inbound.

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "Marketplace",
        actions: [
          "aws-marketplace:ViewSubscriptions",
          "aws-marketplace:Subscribe",
          "aws-marketplace:Unsubscribe",
        ],
        resources: ["*"],
      }),
    );

    // CloudWatch Logs
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchLogs",
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "cloudwatch:PutMetricData",
        ],
        resources: ["*"],
      }),
    );

    // CloudWatch Logs read — for the admin dashboard inline log viewer
    // (apps/backend/core/services/cloudwatch_logs.py). Scoped to the
    // backend's own log group ARN; never "*" — least privilege.
    //
    // Log group name matches service-stack.ts + lightbulb-stage.ts —
    // `/ecs/lightbulb-${env}` (no /aws prefix, no
    // -backend suffix). The cloudwatch_logs service must use this same
    // name when constructing FilterLogEvents calls.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchLogsReadForAdmin",
        actions: [
          "logs:FilterLogEvents",
          "logs:StartQuery",
          "logs:StopQuery",
          "logs:GetQueryResults",
          "logs:GetLogEvents",
          "logs:DescribeLogStreams",
        ],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/ecs/lightbulb-${env}:*`,
        ],
      }),
    );

    // CloudWatch alarms read for the admin operator surface. DescribeAlarms
    // is not resource-scoped, so it requires "*".
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchAlarmsReadForAdmin",
        actions: ["cloudwatch:DescribeAlarms"],
        resources: ["*"],
      }),
    );

    // Admin growth read (routers/ops.py GET /growth): provision.gate.blocked
    // counts by code come from CloudWatch — the gate persists nothing else.
    // GetMetricData has no resource-level scoping, so "*" is the only shape.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AdminGrowthCloudWatchRead",
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:GetMetricData"],
        resources: ["*"],
      }),
    );

    // Cloud Map (service discovery) — constrained to namespace + service
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudMapAccess",
        actions: [
          "servicediscovery:RegisterInstance",
          "servicediscovery:DeregisterInstance",
          "servicediscovery:GetNamespace",
          "servicediscovery:GetService",
          "servicediscovery:ListInstances",
        ],
        resources: [
          props.container.cloudMapNamespace.namespaceArn,
          props.container.cloudMapService.serviceArn,
        ],
      }),
    );
    // DiscoverInstances is not resource-scoped — it requires "*"
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudMapDiscover",
        actions: ["servicediscovery:DiscoverInstances"],
        resources: ["*"],
      }),
    );

    // KMS (use string ARN to avoid cross-stack dependency)
    //
    // kms:Encrypt is required so the provision flow can seal per-container
    // secrets (operator device Ed25519 seeds, gateway tokens) before writing
    // them to the containers DynamoDB table. kms:Decrypt is the pre-existing
    // permission for reading them back at handshake time.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "KmsAccess",
        actions: ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
        resources: [props.kmsKeyArn],
      }),
    );

    // EC2 self-discovery + CloudFormation read
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "Ec2SelfDiscovery",
        actions: [
          "ec2:DescribeInstances",
          "ec2:DescribeTags",
          "cloudformation:DescribeStacks",
          "cloudformation:DescribeStackEvents",
          // Required to call DescribeStacks WITHOUT a StackName (enumerate all
          // stacks): CloudFormation authorizes that form against ListStacks,
          // not DescribeStacks. The deploy-status view lists every stack, so
          // dropping this returns AccessDenied on the enumerate call.
          "cloudformation:ListStacks",
        ],
        resources: ["*"],
      }),
    );

    // STS. GetCallerIdentity is resource-less; AssumeRole is scoped to the
    // customer connector role only (aws-connector.yaml pins
    // RoleName=lightbulb-connector = bedrock_aip.CONNECTOR_ROLE_NAME, and
    // routers/connections.py is the sole assume_role caller). Unscoped
    // AssumeRole would let a compromised backend assume ANY role in ANY
    // account that trusts this principal.
    //
    // A dedicated ManagedPolicy, NOT addToPolicy: the role's DefaultPolicy
    // inline document sits near CloudFormation's 10 KB limit, and one more
    // inline statement tips CDK into an overflow ManagedPolicy that
    // relocates unrelated statements (the packing the stack tests pin).
    new iam.ManagedPolicy(this, "TaskRoleSts", {
      managedPolicyName: `lightbulb-${env}-backend-sts`,
      statements: [
        new iam.PolicyStatement({
          sid: "StsAccess",
          actions: ["sts:GetCallerIdentity"],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "StsAssumeConnectorRole",
          actions: ["sts:AssumeRole"],
          // The single canonical name. The role lives in the OWNER'S account
          // and the quick-create template pins RoleName, so an account that
          // still holds a pre-rename role must relaunch the stack before it
          // can vend -- routers/connections.py rejects any other name at
          // connect time with a 400 rather than an AccessDenied here.
          resources: ["arn:aws:iam::*:role/lightbulb-connector"],
        }),
      ],
      roles: [this.taskRole],
    });

    // EFS access points management
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EfsAccessPoints",
        actions: [
          "elasticfilesystem:ClientMount",
          "elasticfilesystem:ClientWrite",
          "elasticfilesystem:CreateAccessPoint",
          "elasticfilesystem:DescribeAccessPoints",
          "elasticfilesystem:TagResource",
        ],
        resources: [props.container.efsFileSystem.fileSystemArn],
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "EfsDeleteAccessPoints",
        actions: [
          "elasticfilesystem:DeleteAccessPoint",
          "elasticfilesystem:TagResource",
        ],
        resources: [
          `arn:aws:elasticfilesystem:*:${this.account}:access-point/*`,
        ],
      }),
    );

    // S3 (OpenClaw config bucket)
    // Per-user path scoping via IAM policy variables (${aws:userid}) is not
    // feasible: aws:userid resolves to the IAM role unique ID, not the app-
    // level Clerk user ID that keys the S3 paths. The bucket is already
    // constrained to isol8-${env}-openclaw-configs so the blast radius is
    // limited to this bucket. Application-level authorization enforces per-
    // user access.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "S3Access",
        actions: [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ],
        resources: [
          `arn:aws:s3:::lightbulb-${env}-openclaw-configs`,
          `arn:aws:s3:::lightbulb-${env}-openclaw-configs/*`,
        ],
      }),
    );

    // WebSocket push (execute-api:ManageConnections)
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "WebSocketManageConnections",
        actions: ["execute-api:ManageConnections"],
        resources: [
          `arn:aws:execute-api:${this.region}:${this.account}:${props.wsApiId}/${props.wsStage}/*`,
        ],
      }),
    );

    // DynamoDB CRUD on connections table
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "DynamoDbConnections",
        actions: [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ],
        resources: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${props.connectionsTableName}`,
          // GSI ARN — required to Query the by-user-id index added in
          // api-stack.ts. Without this, broker fanout queries 400 with
          // AccessDeniedException at runtime.
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${props.connectionsTableName}/index/*`,
        ],
      }),
    );

    // -------------------------------------------------------------------------
    // Marketplace Artifacts S3 Bucket
    //
    // Stores artifacts uploaded for marketplace listings (agent bundles,
    // screenshots, manifests, signed receipts). Versioned so a bad publish
    // can be rolled back without losing buyer history. Blocked from public
    // access; bytes are served via signed URLs from the marketplace API.
    // -------------------------------------------------------------------------
    const marketplaceArtifactsBucket = new s3.Bucket(this, "MarketplaceArtifactsBucket", {
      bucketName: `lightbulb-${env}-marketplace-artifacts`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: env !== "prod",
      // Backup-import uploads go browser → S3 directly via a presigned PUT
      // (API Gateway caps bodies at 10 MB; real OpenClaw backups are 10×–100×
      // that, dominated by a Chrome profile the importer discards anyway).
      cors: [
        {
          allowedMethods: [s3.HttpMethods.PUT],
          allowedOrigins:
            env === "prod"
              ? [`https://${rootDomain}`]
              : [
                  `https://dev.${rootDomain}`,
                  "http://localhost:3000",
                  "https://*.vercel.app",
                ],
          allowedHeaders: ["content-type"],
          maxAge: 3600,
        },
      ],
      lifecycleRules: [
        {
          // Raw uploads are consumed within minutes; nothing under imports/
          // is ever read twice. Versioned bucket → expire noncurrent too.
          prefix: "imports/",
          expiration: cdk.Duration.days(1),
          noncurrentVersionExpiration: cdk.Duration.days(1),
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(1),
        },
      ],
    });

    marketplaceArtifactsBucket.grantReadWrite(this.taskRole);

    // -------------------------------------------------------------------------
    // CFN Templates S3 Bucket (public-read of the connector template only)
    //
    // Hosts the AWS connector CloudFormation template so the
    // "Create role in AWS" quick-create button works: CloudFormation requires
    // the TemplateURL to be an S3 URL it can fetch ANONYMOUSLY (not a
    // CloudFront/Vercel URL). The template (apps/infra/aws/aws-connector.yaml)
    // is generic + fully parameterized — no secrets — so public READ of that
    // single object is safe.
    //
    // Public-access posture: block public ACLs (we never use ACLs) but ALLOW a
    // public bucket POLICY, and the policy below grants ONLY s3:GetObject on the
    // one template object to everyone. No public write, no listing, no other
    // objects (the deployment prunes the bucket to exactly this template). SSE-S3
    // (not KMS) so anonymous CloudFormation GETs aren't blocked on decrypt.
    // -------------------------------------------------------------------------
    const CFN_TEMPLATE_KEY = "aws-connector.yaml";
    const cfnTemplatesBucket = new s3.Bucket(this, "CfnTemplatesBucket", {
      // The one bucket name a CUSTOMER sees: it is the TemplateURL in the
      // CloudFormation quick-create link on the AWS-connect flow.
      //
      // Renaming replaces the bucket, which is safe here in a way it is not for
      // the other buckets: prod is RETAIN, so the old bucket is orphaned rather
      // than deleted and keeps serving any quick-create link a customer already
      // holds, and BucketDeployment repopulates the new one with the same single
      // object. Delete the orphan only once you accept that held links break.
      bucketName: `${resourcePrefix(env)}cfn-templates`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: new s3.BlockPublicAccess({
        blockPublicAcls: true,
        ignorePublicAcls: true,
        blockPublicPolicy: false,
        restrictPublicBuckets: false,
      }),
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: env !== "prod",
    });

    // Public READ of the single connector template object — nothing else.
    cfnTemplatesBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: "PublicReadConnectorTemplate",
        effect: iam.Effect.ALLOW,
        principals: [new iam.AnyPrincipal()],
        actions: ["s3:GetObject"],
        resources: [cfnTemplatesBucket.arnForObjects(CFN_TEMPLATE_KEY)],
      }),
    );

    // Upload the connector template. Exclude the dir's README so the bucket
    // holds ONLY the template; `prune` (default true) keeps it to that object.
    new s3deploy.BucketDeployment(this, "CfnTemplateDeployment", {
      // Any explicit memoryLimit changes the BucketDeployment singleton's
      // uuid -> new logical id -> the helper Lambda is recreated under the
      // lightbulb stack name instead of the retired isol8 auto-name.
      memoryLimit: 128,
      destinationBucket: cfnTemplatesBucket,
      sources: [
        s3deploy.Source.asset(path.join(__dirname, "..", "..", "aws"), {
          exclude: ["*.md"],
        }),
      ],
    });

    // -------------------------------------------------------------------------
    // Marketplace browse / search is served by the backend's in-process
    // search service (apps/backend/core/services/marketplace_search.py) —
    // a 60s-TTL DDB scan over published listings. At v0 scale the listing
    // count is small enough that we don't need an external SaaS index;
    // there is no separate sync Lambda or Stream subscriber.
    // -------------------------------------------------------------------------

    // -------------------------------------------------------------------------
    // Task Execution Role
    // -------------------------------------------------------------------------
    const taskExecutionRole = new iam.Role(this, "TaskExecutionRole", {
      roleName: `lightbulb-${env}-backend-task-execution`,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Lightbulb ${env} Fargate task execution role`,
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonECSTaskExecutionRolePolicy",
        ),
      ],
    });

    // Allow execution role to pull secrets for container injection
    taskExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SecretsManagerForEcs",
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/*`,
        ],
      }),
    );

    // Allow execution role to decrypt secrets encrypted with KMS
    // (manual grant instead of CDK auto-grant to avoid cross-stack cycle)
    taskExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "KmsDecryptForSecrets",
        actions: ["kms:Decrypt"],
        resources: [props.kmsKeyArn],
      }),
    );

    // -------------------------------------------------------------------------
    // Log Group
    // -------------------------------------------------------------------------
    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/lightbulb-${env}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    // -------------------------------------------------------------------------
    // Fargate Task Definition
    // -------------------------------------------------------------------------
    const privateSubnetIds = props.vpc
      .selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS })
      .subnetIds.join(",");

    // Backend container environment + secrets, declared as consts (rather
    // than inline literals) so the provisioning-executor task definition can
    // attach the exact same objects. core.config.Settings reads env vars only,
    // so identical env/secrets => identical runtime config for both processes,
    // and the two container definitions can never drift.
    const backendContainerEnvironment: Record<string, string> = {
      ENVIRONMENT: env,
      AWS_REGION: "us-east-1",
      // OpenClaw image versions — injected from openclaw-version.json (the
      // single source of truth) so the backend never hand-mirrors them.
      // Read at runtime by core/openclaw_version.py.
      OPENCLAW_UPSTREAM_VERSION: OPENCLAW_VERSION.tag,
      OPENCLAW_DEV_TAG: OPENCLAW_VERSION.dev.tag,
      OPENCLAW_PROD_TAG: OPENCLAW_VERSION.prod.tag,
      // The Litestream replica bucket for this env. Every backend code path
      // that touches the local-sqlite layout (sentinel writes, replica
      // lifecycle deletes) keys off this being set. Name-based (never a
      // bucket ref) per the KMS-cycle posture.
      LITESTREAM_BUCKET: `lightbulb-${env}-openclaw-replicas`,
      WS_MANAGEMENT_API_URL: props.managementApiUrl,
      WS_GATEWAY_URL: props.webSocketUrl,
      WS_CONNECTIONS_TABLE: props.connectionsTableName,
      // WS caller-attestation gate, enforced in every env. The
      // x-lightbulb-ws-internal header is verified flowing from the WS Lambdas on
      // both dev and prod (zero secret-fetch failures across real connects),
      // so the backend rejects /ws/* callers that lack it — closing the
      // unauthenticated WebSocket-impersonation hole on the public HTTP
      // API Gateway $default path.
      WS_ENFORCE_INTERNAL_SECRET: "true",
      WEBHOOK_DEDUP_TABLE: tableName(env, "webhook-event-dedup"),
      CORS_ORIGINS:
        env === "prod"
          ? `https://${rootDomain},https://www.${rootDomain}`
          : `https://dev.${rootDomain}`,
      // SEC-8 token-origin fence: Clerk session tokens carry no `aud`, so the
      // backend validates the `azp` (authorized party) claim against this
      // allowlist instead. Broader than CORS_ORIGINS because azp reflects the
      // MINT origin, not the calling origin: admin-host sessions call the API
      // via server actions (no CORS), the paperclip-proxy bootstrap page
      // handshakes Clerk sessions on the company.isol8.co hosts, and
      // localhost frontends point at the dev API during development. Native
      // (mobile) tokens carry no azp and always pass.
      // Rollout: ships in audit mode (log + metric, never
      // reject); flip CLERK_AZP_MODE to "enforce" per env once the
      // auth.azp.mismatch metric shows zero legitimate mismatches.
      CLERK_AUTHORIZED_PARTIES:
        env === "prod"
          ? `https://${rootDomain},https://www.${rootDomain},https://admin.${rootDomain},https://company.${rootDomain}`
          : `https://dev.${rootDomain},https://admin.dev.${rootDomain},https://dev.company.${rootDomain},http://localhost:3000`,
      CLERK_AZP_MODE: "audit",
      // Per-container secrets CMK — used by core/crypto/kms_secrets.py to
      // encrypt operator device seeds + gateway tokens at rest. Reuses the
      // same CMK as the BYOK Fernet layer since the blast radius is the same
      // (any principal with kms:Decrypt on this key can read any of them).
      CONTAINER_SECRETS_KMS_KEY_ID: props.kmsKeyArn,
      // Stripe flat-fee Price ID is environment-specific: prod is created in
      // Stripe live mode, dev in test mode, and they MUST NOT cross-pollinate
      // (a test-mode Price would silently create test subscriptions in prod).
      // Read per-env from the runner's secrets at synth time.
      STRIPE_FLAT_PRICE_ID:
        env === "prod"
          ? (process.env.STRIPE_FLAT_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_FLAT_PRICE_ID_DEV ?? ""),
      // Stripe per-seat Teams Price ID — same env-specific live/test split
      // as STRIPE_FLAT_PRICE_ID above (a test-mode Price would silently
      // create test subscriptions in prod). Read per-env from the runner's
      // secrets at synth time. Consumed by billing_service.create_team_seat_checkout.
      STRIPE_TEAM_SEAT_PRICE_ID:
        env === "prod"
          ? (process.env.STRIPE_TEAM_SEAT_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_TEAM_SEAT_PRICE_ID_DEV ?? ""),
      // Stripe Teams BASE Price ID — the $50/mo flat component charged
      // alongside the per-seat item. Same env-specific live/test split as
      // STRIPE_FLAT_PRICE_ID above. Read per-env from the runner's secrets at
      // synth time.
      STRIPE_TEAM_BASE_PRICE_ID:
        env === "prod"
          ? (process.env.STRIPE_TEAM_BASE_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_TEAM_BASE_PRICE_ID_DEV ?? ""),
      // Stripe Ultra Pro Price ID — same env-specific live/test split as
      // STRIPE_FLAT_PRICE_ID above (a test-mode Price would silently create
      // test subscriptions in prod). Read per-env from the runner's secrets
      // at synth time. Consumed by the Ultra-tier checkout call site.
      STRIPE_ULTRA_PRICE_ID:
        env === "prod"
          ? (process.env.STRIPE_ULTRA_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_ULTRA_PRICE_ID_DEV ?? ""),
      FRONTEND_URL:
        env === "prod" ? `https://${rootDomain}` : `https://dev.${rootDomain}`,
      // Public-facing base URL of THIS API. Used by /hooks/credentials
      // (and any future endpoint that surfaces an absolute URL back to
      // the browser) to build the webhook URL the Settings panel
      // displays to the user. Per-env API hostname, matches the DNS
      // records provisioned by dns-stack.ts.
      PUBLIC_API_BASE:
        env === "prod" ? `https://api.${rootDomain}` : `https://api-${env}.${rootDomain}`,
      // Paperclip wiring. Without these the backend's HostDispatcherMiddleware
      // (apps/backend/main.py) builds an empty dispatch host set, so requests
      // to `company-{env}.isol8.co` fall through to the default Isol8 API
      // instead of being rewritten to the paperclip_proxy router. Mirrors
      // the env-derivation in paperclip-stack.ts so the two stay in lockstep
      // (keeps ServiceStack independent of PaperclipStack synth order).
      // The `Origin` header the Teams BFF sends on Paperclip /api/auth/* +
      // board-mutation calls. MUST match PaperclipStack's PAPERCLIP_PUBLIC_URL
      // (Better Auth's trustedOrigins / origin guard), so keep the two in lockstep
      // at the same https origin. NOT a live link and NOT the agent's API URL —
      // see the PAPERCLIP_PUBLIC_URL comment in paperclip-stack.ts. Switching this
      // to the http in-VPC host (PR #1073) broke Better Auth sign-in (403).
      PAPERCLIP_PUBLIC_URL:
        env === "prod"
          ? `https://company.${rootDomain}`
          : `https://${env}.company.${rootDomain}`,
      // Cloud Map A record published by PaperclipStack at:
      //   `paperclip.<cloudMapNamespace.namespaceName>:3100`
      // Namespace name is `lightbulb-${env}.local` (ContainerStack). Reading
      // it off the namespace handle keeps this string in lockstep with
      // ContainerStack rather than hard-coding the env suffix here.
      PAPERCLIP_INTERNAL_URL: `http://paperclip.${props.container.cloudMapNamespace.namespaceName}:3100`,
      // Clerk publishable key for the paperclip-proxy bootstrap HTML.
      // The bootstrap loads the Clerk SDK in the browser, fetches a JWT,
      // and POSTs it to /__handshake__ to get a host-scoped session
      // cookie on company.isol8.co. Same value the frontend uses as
      // NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY — publishable by design (Clerk
      // serves it in HTML), so plain env var is appropriate (not a
      // Secrets Manager secret). Same per-env runner-secret pattern as
      // STRIPE_FLAT_PRICE_ID. Dev fallback hardcoded so local synths
      // and developer cdk-synth-from-laptop work out of the box.
      CLERK_PUBLISHABLE_KEY:
        env === "prod"
          ? (process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_PROD ?? "")
          : (process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_DEV ??
              "pk_test_dXAtbW90aC01NS5jbGVyay5hY2NvdW50cy5kZXYk"),
      // Apple IAP. Every one of these is REQUIRED for core/services/apple_iap
      // to build a verifier at all — with any of them missing the link and
      // notification endpoints answer 503, so a completed StoreKit purchase
      // never grants a plan and renewals are never recorded. They are plain env
      // vars, not secrets: the bundle/product/app ids are public (they ship in
      // the binary and the App Store listing), and the actual trust anchor is
      // the certificate directory below.
      APPLE_IAP_BUNDLE_ID: "company.thelightbulb.mobile",
      // Must match the product created in App Store Connect EXACTLY, and the
      // SKU the app requests (EXPO_PUBLIC_APPLE_IAP_PRODUCT_ID in eas.json). A
      // transaction naming anything else is refused rather than granted.
      APPLE_IAP_PRODUCT_ID: "company.thelightbulb.mobile.plan.monthly",
      // Numeric App Store app id — the same ascAppId eas.json submits against.
      // Apple compares it numerically; the backend converts and rejects a
      // non-numeric value as misconfiguration.
      APPLE_IAP_APP_APPLE_ID: "6788944100",
      // The PRIMARY environment only. Both are accepted at verification time
      // (see apple_iap._verify_signed) because App Review and TestFlight send
      // SANDBOX transactions to whichever backend the build points at —
      // including production.
      APPLE_IAP_ENVIRONMENT: env === "prod" ? "production" : "sandbox",
      // Apple's root CAs, baked into the backend image. The chain is PINNED to
      // these, so a missing or wrong directory disables the feature rather than
      // weakening it.
      APPLE_IAP_ROOT_CERTS_DIR: "/app/certs/apple",
      EFS_MOUNT_PATH: "/mnt/efs/users",
      EFS_FILE_SYSTEM_ID: props.container.efsFileSystem.fileSystemId,
      ECS_CLUSTER_ARN: props.container.cluster.clusterArn,
      // ECS_TASK_DEFINITION is intentionally not set. Backend reads the
      // latest CDK base revision live via describe_task_definition(family)
      // — the base family is uncontaminated because per-user clones go to
      // a separate `<base>-user` family.
      ECS_SUBNETS: privateSubnetIds,
      ECS_SECURITY_GROUP_ID:
        props.container.containerSecurityGroup.securityGroupId,
      CLOUD_MAP_SERVICE_ARN: props.container.cloudMapService.serviceArn,
      CLOUD_MAP_NAMESPACE_NAME: props.container.cloudMapNamespace.namespaceName,
      CLOUD_MAP_SERVICE_NAME: props.container.cloudMapService.serviceName,
      DYNAMODB_TABLE_PREFIX: tablePrefix(env),
      OAUTH_TOKENS_TABLE: tableName(env, "oauth-tokens"),
      // Metronome lifecycle + gating settings. Metronome is the ONLY
      // billing ledger: the backend provisions Metronome customers, funds
      // payment-gated top-up commits, handles the low-balance webhook,
      // lazily refreshes the gate, and displays balances — it never
      // ingests usage (the meter Lambda does). Base URL is the same for
      // sandbox + prod (token decides env). Fast-gate table backs the
      // synchronous chat gate's balance projection.
      METRONOME_BASE_URL: "https://api.metronome.com/v1",
      // Literal name (not a cross-stack tableName Ref) — same posture as
      // BEDROCK_AIP_OWNERS_TABLE above: the table is granted BY NAME in the
      // DynamoDbIntegrations statement, so no cross-stack export is created.
      METRONOME_FAST_GATE_TABLE: tableName(env, "metronome-fast-gate"),
      // Per-env rate card the per-customer contract references at
      // provision. Dev = sandbox rate card (2026-06-10); prod =
      // isol8-bedrock-prod (2026-06-12). Both price AWS us. CRIS x 1.03
      // in cents per token (margin decision 2026-06-11).
      METRONOME_RATE_CARD_ID:
        env === "dev"
          ? "99576f5a-37d2-4983-ba64-fea9bad96e08"
          : env === "prod"
            ? "fdac2edd-f207-4f11-b65b-01fad6fbbdbc"
            : "",
      // The FIXED-type product top-up commits land on. Payment-gated
      // invoicing additionally requires a stripe_product_id custom field
      // on it (set: dev -> test-mode product, prod -> live-mode
      // prod_UgkNkatoEaWFnb); without it, top-ups 400 loudly rather
      // than silently no-op.
      METRONOME_CREDITS_PRODUCT_ID:
        env === "dev"
          ? "6dab1634-020d-4288-b5e6-99568a30404c"
          : env === "prod"
            ? "876e1694-9d76-4c37-9814-c957dbd9370f"
            : "",
      // Per-owner Bedrock AIP + scoped-role enforcement (Plan 2). The reverse
      // table is granted BY NAME above, so the env var is the literal name
      // (not a cross-stack tableName ref). The boundary + base-policy ARNs
      // come from the container stack's live members.
      BEDROCK_AIP_OWNERS_TABLE: tableName(env, "bedrock-aip-owners"),
      BEDROCK_OWNER_BOUNDARY_ARN:
        props.container.ownerBedrockBoundary.managedPolicyArn,
      BEDROCK_OWNER_BASE_POLICY_ARN:
        props.container.ownerBedrockBasePolicy.managedPolicyArn,
      // Wake-on-intent gating for parked (stop_reason=trial_idle) trial
      // containers. ON in every environment — the code is fail-open (any
      // uncertainty falls back to wake-on-read, never worse than the old
      // behavior) and heavily reviewed, and the urgent cost driver (the 30m
      // heartbeat) is already fixed fleet-wide. This env var is retained as
      // the KILL SWITCH: set it to "false" + deploy to disable wake gating
      // wholesale without a code revert if prod ever misbehaves. The
      // stop_reason stamping and chat-send server wake are live regardless.
      TRIAL_WAKE_GATE_ENABLED: "true",
      // Browser egress via the container's Cloudflare WARP local proxy.
      // ON in both envs. "0" = off: write_openclaw_config emits no proxy flag
      // and no browser.ssrfPolicy (the two travel together — see
      // _browser_egress_args) and Chrome egresses on the AWS NAT IP. The
      // container half (Dockerfile layer 9c + Step C4b boot script) is inert
      // without the cloudflare-warp-enrollment secret, and with WARP down the
      // PAC falls back to DIRECT, so flip order is harmless. Existing
      // containers pick a change up only on reprovision / Regenerate config —
      // or a PATCH .../config deep-merge.
      BROWSER_EGRESS_PROXY_PORT: String(BROWSER_EGRESS_PROXY_PORT),
      // Marketplace DDB tables + S3 artifacts bucket. Backend reads these
      // env vars via core.config.settings; empty defaults keep the app
      // booting in non-marketplace contexts (unit tests, smoke tests).
      MARKETPLACE_LISTINGS_TABLE:
        tableName(env, "marketplace-listings"),
      MARKETPLACE_PURCHASES_TABLE:
        tableName(env, "marketplace-purchases"),
      MARKETPLACE_PAYOUT_ACCOUNTS_TABLE:
        tableName(env, "marketplace-payout-accounts"),
      MARKETPLACE_TAKEDOWNS_TABLE:
        tableName(env, "marketplace-takedowns"),
      MARKETPLACE_SELLERS_TABLE:
        tableName(env, "marketplace-sellers"),
      MARKETPLACE_ARTIFACTS_BUCKET: marketplaceArtifactsBucket.bucketName,
      // Deterministic public S3 URL of the hosted AWS connector
      // CloudFormation template. The connections aws-setup endpoint feeds this
      // as the quick-create TemplateURL (CloudFormation must fetch it
      // anonymously). Bucket name + key are fixed, so this is a literal string
      // — no cross-stack output ref needed (the bucket lives in this stack).
      CFN_TEMPLATE_URL: `https://${resourcePrefix(env)}cfn-templates.s3.us-east-1.amazonaws.com/${CFN_TEMPLATE_KEY}`,
      // Stripe Connect onboarding redirects — environment-specific so
      // dev/prod hit the correct marketplace host. Stripe-hosted onboarding
      // bounces sellers back here on refresh + completion.
      STRIPE_CONNECT_REFRESH_URL:
        env === "prod"
          ? `https://${rootDomain}/marketplace/payouts/refresh`
          : `https://dev.${rootDomain}/marketplace/payouts/refresh`,
      STRIPE_CONNECT_RETURN_URL:
        env === "prod"
          ? `https://${rootDomain}/marketplace/payouts/return`
          : `https://dev.${rootDomain}/marketplace/payouts/return`,
      // Company Stripe account — owner's own business account on the same
      // Connect platform. Return/refresh land on Settings → Connections; the
      // OAuth redirect is the backend callback on the API host.
      STRIPE_COMPANY_REFRESH_URL:
        env === "prod"
          ? `https://${rootDomain}/settings/connections?stripe=refresh`
          : `https://dev.${rootDomain}/settings/connections?stripe=refresh`,
      STRIPE_COMPANY_RETURN_URL:
        env === "prod"
          ? `https://${rootDomain}/settings/connections?stripe=return`
          : `https://dev.${rootDomain}/settings/connections?stripe=return`,
      STRIPE_COMPANY_OAUTH_REDIRECT_URL:
        env === "prod"
          ? `https://api.${rootDomain}/api/v1/company/stripe/connect/callback`
          : `https://api-dev.${rootDomain}/api/v1/company/stripe/connect/callback`,
      STRIPE_CONNECT_CLIENT_ID: props.stripeConnectClientId ?? "",
      // Provider catalog partition. dev reads/writes Stripe's `testing`
      // partition, prod the real one. NOT a payment-mode switch — Stripe's own
      // caveat: "Selecting a DEV or TESTING catalog doesn't mean 'no real
      // charges.'"
      STRIPE_PROJECTS_CATALOG: props.stripeProjectsCatalog ?? "prod",
      // Stripe's recommended `developer` configuration + `projects`
      // capability on the company account. Default-off flag — see the
      // stripeProjectsDeveloperConfig prop doc for why.
      STRIPE_PROJECTS_DEVELOPER_CONFIG: String(props.stripeProjectsDeveloperConfig ?? false),
      // Accounts v2 API version pin for the company account/account-link
      // calls. Empty ⇒ the SDK's own default.
      STRIPE_ACCOUNTS_V2_VERSION: props.stripeAccountsV2Version ?? "",
      // PostHog server-side reads for the admin Activity tab.
      // us.posthog.com is the API host for US Cloud (our project lives
      // there). Project ID is public — visible in the PostHog dashboard
      // URL for the Default project. Personal API key comes in via the
      // backendContainerSecrets const below.
      POSTHOG_HOST: "https://us.posthog.com",
      POSTHOG_PROJECT_ID: "380894",
      // PostHog project (phc_) INGESTION key for the server-side billing
      // bridge (core/observability/product_analytics.py -> trial_started /
      // trial_converted / subscription_cancelled, keyed on the Clerk user).
      // Write-only / public — the same key the frontend ships as
      // NEXT_PUBLIC_POSTHOG_KEY (PostHog: "Safe to use in public apps"), so
      // it lives in the plain env block, NOT secrets. Distinct from the
      // POSTHOG_PROJECT_API_KEY personal read key in backendContainerSecrets.
      POSTHOG_PROJECT_WRITE_KEY: "phc_nzqnGGPwHfrKycptH3TViSAo3uaxT8CHAXovvHnBGEma",
      // Agent-email base domain — the platform mail domain in prod (where the
      // SES stack and verified identity exist), empty in every other env. The backend's
      // AGENT_EMAIL_DOMAIN guards treat empty as "agent email off", so
      // dev/local/CI stay inert.
      AGENT_EMAIL_DOMAIN: props.agentEmailDomain ?? "",
      // Agent-email inbound attachments bucket, from the prod-stage
      // SesInboundStack when it exists (prod-only — see lightbulb-stage.ts).
      // SES configuration set stamped on every agent-mail send
      // (core/services/ses_email.py), from the AgentMailOutboundStack
      // instantiated in lockstep with AGENT_EMAIL_DOMAIN above. Empty string
      // wherever that stack isn't deployed — sends stay unstamped, a
      // harmless no-op.
      AGENT_MAIL_CONFIGURATION_SET: props.agentMailConfigurationSet ?? "",
      // Platform sender identities, on the apex of the mail domain. Set here
      // rather than left to core/config.py's defaults so the mail domain has a
      // SINGLE source of truth (lib/domains.ts) and a move cannot leave the
      // backend quietly sending as the old domain — which SES would refuse
      // once the legacy identity is retired. All three are apex addresses, so
      // the one apex domain identity authorizes them; `support@` is a Google
      // Workspace mailbox (apex MX), not an SES one.
      EMAIL_FROM_WELCOME: `Lightbulb <hello@${platformMailDomain}>`,
      EMAIL_SUPPORT_ADDRESS: `support@${platformMailDomain}`,
      EMAIL_FROM_TICKETS: `Lightbulb Support <tickets@${platformMailDomain}>`,
    };

    const backendContainerSecrets: Record<string, ecs.Secret> = {
      // Import secrets by name (NOT cross-stack ISecret) to avoid CDK
      // auto-granting KMS decrypt on AuthStack's key, which causes a
      // circular dependency: auth -> service -> container -> auth.
      //
      CLERK_ISSUER: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportClerkIssuer", props.secretNames.clerkIssuer),
      ),
      CLERK_SECRET_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportClerkSecretKey", props.secretNames.clerkSecretKey),
      ),
      CLERK_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportClerkWebhookSecret", props.secretNames.clerkWebhookSecret),
      ),
      STRIPE_SECRET_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportStripeSecretKey", props.secretNames.stripeSecretKey),
      ),
      STRIPE_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportStripeWebhookSecret", props.secretNames.stripeWebhookSecret),
      ),
      // Marketplace webhook signing secrets. TWO Stripe endpoint
      // registrations deliver to /webhooks/stripe-marketplace: a
      // platform-scoped one (checkout/refund events — they originate on
      // the platform balance) whose secret is STRIPE_MARKETPLACE_WEBHOOK_SECRET,
      // and a Connect-scoped one (connected-account account.updated) whose
      // secret is STRIPE_CONNECT_WEBHOOK_SECRET. The route verifies against
      // both. An empty value means that half's events fail signature
      // verification: empty MARKETPLACE ⇒ paid checkouts never produce
      // purchase rows; empty CONNECT ⇒ onboarding-completed payout flushes
      // never fire. Operator provisions both via `aws secretsmanager
      // update-secret` with the secrets returned when the endpoints are
      // created (Stripe API returns each endpoint's secret at creation).
      STRIPE_MARKETPLACE_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(
          this,
          "ImportStripeMarketplaceWebhookSecret",
          `lightbulb/${env}/stripe_marketplace_webhook_secret`,
        ),
      ),
      STRIPE_CONNECT_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(
          this,
          "ImportStripeConnectWebhookSecret",
          `lightbulb/${env}/stripe_connect_webhook_secret`,
        ),
      ),
      ENCRYPTION_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportEncryptionKey", props.secretNames.encryptionKey),
      ),
      // PostHog personal API key for the admin Activity tab. Empty value
      // → posthog_admin.py returns {stubbed: true}; populated value →
      // real Persons API calls. Rotate via `aws secretsmanager update-secret`.
      POSTHOG_PROJECT_API_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(
          this,
          "ImportPosthogProjectApiKey",
          props.secretNames.posthogProjectApiKey,
        ),
      ),
      // Symmetric secret used by the paperclip_proxy router to sign
      // service-token JWTs Paperclip's Better Auth verifies. KMS decrypt
      // for this secret is covered by the existing `KmsDecryptForSecrets`
      // policy below — same CMK as the other auth secrets.
      PAPERCLIP_SERVICE_TOKEN_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(
          this,
          "ImportPaperclipServiceTokenKey",
          props.secretNames.paperclipServiceTokenKey,
        ),
      ),
      // Bearer secret validated by /webhooks/paperclip-events (inbound
      // Paperclip plugin events). Same CMK, covered by KmsDecryptForSecrets.
      PAPERCLIP_EVENTS_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(
          this,
          "ImportPaperclipEventsWebhookSecret",
          props.secretNames.paperclipEventsWebhookSecret,
        ),
      ),
      // Shared password for the gog CLI file keyring. The backend's gog_cli
      // materializer runs gog against the owner's EFS workspace to write the
      // Google OAuth keyring, which the file-keyring backend encrypts with
      // this password — the identical value every per-user OpenClaw container
      // also receives. KMS decrypt is covered by `KmsDecryptForSecrets` below
      // (same CMK as the other auth secrets).
      GOG_KEYRING_PASSWORD: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(
          this,
          "ImportGogKeyringPassword",
          props.secretNames.gogKeyringPassword,
        ),
      ),
      // Read via settings.METRONOME_API_KEY by the Metronome lifecycle
      // modules (customer provisioning, top-up funding, balance display)
      // unconditionally — Metronome shadows even while the DDB ledger
      // still bills, so the key is live in every environment.
      // KMS decrypt covered by the wildcard SecretsAccess + same CMK.
      METRONOME_API_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportMetronomeApiKey", props.secretNames.metronomeApiKey),
      ),
      METRONOME_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(
          this,
          "ImportMetronomeWebhookSecret",
          props.secretNames.metronomeWebhookSecret,
        ),
      ),
      // Shared secret the WS Lambdas attach as x-lightbulb-ws-internal. The
      // backend compares it (require_ws_internal_caller) only when
      // WS_ENFORCE_INTERNAL_SECRET is "true". Same CMK as the other auth
      // secrets, so KmsDecryptForSecrets below already covers decrypt.
      WS_INTERNAL_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(
          this,
          "ImportWsInternalSecret",
          props.secretNames.wsInternalSecret,
        ),
      ),
      // Shared secret the SES-ingest Lambda attaches as X-Lightbulb-SES-Secret.
      // The backend compares it in constant time (routers/ses_inbound.py)
      // only when AGENT_EMAIL_DOMAIN is also set. KMS decrypt covered by
      // the wildcard SecretsAccess + KmsDecryptForSecrets below (same CMK
      // as the other auth secrets).
      SES_INBOUND_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(
          this,
          "ImportSesIngestSecret",
          props.secretNames.sesIngestSecret,
        ),
      ),
    };

    // ---------------------------------------------------------------------
    // Provisioning executor: one-shot provisioner task + state machine.
    // Same image / roles / env / SG as the backend so provision_user_container
    // runs with identical capability, but OUTSIDE the deployable web process.
    // serviceSg is mandatory: the per-user container 18789 ingress rule
    // (ContainerFromServiceIngress above) is keyed to it.
    // ---------------------------------------------------------------------
    const provisionerTaskDef = new ecs.FargateTaskDefinition(this, "ProvisionerTaskDef", {
      family: `lightbulb-${env}-provisioner`,
      cpu: 1024,
      memoryLimitMiB: 2048,
      taskRole: this.taskRole,
      executionRole: taskExecutionRole,
    });
    provisionerTaskDef.addVolume({
      name: "efs-users",
      efsVolumeConfiguration: {
        fileSystemId: props.container.efsFileSystem.fileSystemId,
        transitEncryption: "ENABLED",
        authorizationConfig: { iam: "ENABLED" },
      },
    });
    const provisionerContainer = provisionerTaskDef.addContainer("provisioner", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      // Run as root, exactly like the backend container: provisioning writes
      // EFS user directories and chowns them to UID 1000 (workspace.py), and
      // the image's default USER appuser cannot. Dropping this silently
      // breaks every EFS write the runner performs.
      user: "0:0",
      environment: backendContainerEnvironment,
      secrets: backendContainerSecrets,
      // No healthCheck and no port mappings: a batch job must not be killed
      // for not serving HTTP. Command is overridden per-execution by the
      // state machine ($.command).
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: "provisioner", logGroup }),
    });
    provisionerContainer.addMountPoints({
      containerPath: "/mnt/efs",
      sourceVolume: "efs-users",
      readOnly: false,
    });

    const runProvisioner = new tasks.EcsRunTask(this, "RunProvisioner", {
      integrationPattern: sfn.IntegrationPattern.RUN_JOB,
      cluster: props.container.cluster,
      taskDefinition: provisionerTaskDef,
      launchTarget: new tasks.EcsFargateLaunchTarget({
        platformVersion: ecs.FargatePlatformVersion.VERSION1_4, // EFS requires >= 1.4
      }),
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [serviceSg],
      assignPublicIp: false,
      containerOverrides: [
        {
          containerDefinition: provisionerContainer,
          command: sfn.JsonPath.listAt("$.command"),
          // PROVISION_ATTEMPT_TOKEN is THIS attempt's fencing token — fresh
          // every retry via BumpAttempt's States.UUID() below, while
          // $.command (owner/claim args) stays static across the whole
          // execution. An env override (not a $.command element) because
          // CDK/SFN can splice ONE dynamic field per override cleanly; see
          // scripts/provision_runner.py module docstring for the full
          // hazard this closes and why it is NOT in the StartExecution
          // input (that must stay byte-identical for Branch-A idempotency —
          // core/services/provision_dispatch.py).
          environment: [
            {
              name: "PROVISION_ATTEMPT_TOKEN",
              value: sfn.JsonPath.stringAt("$.attempt_token"),
            },
          ],
        },
      ],
      resultPath: sfn.JsonPath.DISCARD,
      taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(30)),
    });

    const succeedProvisioned = new sfn.Succeed(this, "Provisioned");
    const succeedSuperseded = new sfn.Succeed(this, "Superseded");
    const failProvision = new sfn.Fail(this, "ProvisionFailed", {
      error: "ProvisionFailed",
      cause: "provisioner exhausted retries or failed deterministically",
    });

    // Claim-conditioned row writes — the SIGKILL backstop. On a clean runner
    // exit the runner already wrote these values; these writes are idempotent
    // re-assertions that also cover the case where the runner died mid-write.
    // Condition accepts BOTH "provisioning" (runner died before its own
    // release) AND "error" (runner released cleanly, this re-asserts) — a
    // clean transient failure leaves the row at "error", and conditioning on
    // "provisioning" alone would fail every write after attempt 1 and kill
    // the retry loop. Rows at "running"/"stopped", or under a different
    // claim, are never touched (routes to Superseded via the catch below).
    const ddbWrite = (id: string, updateExpr: string, values: Record<string, tasks.DynamoAttributeValue>) => {
      const write = new tasks.DynamoUpdateItem(this, id, {
        table: containersTableRef,
        key: {
          owner_id: tasks.DynamoAttributeValue.fromString(sfn.JsonPath.stringAt("$.owner_id")),
        },
        updateExpression: updateExpr,
        conditionExpression: "provision_claim_id = :cid AND (#s = :prov OR #s = :err)",
        expressionAttributeNames: { "#s": "status" },
        expressionAttributeValues: {
          ...values,
          ":cid": tasks.DynamoAttributeValue.fromString(sfn.JsonPath.stringAt("$.claim_id")),
          ":prov": tasks.DynamoAttributeValue.fromString("provisioning"),
          ":ts": tasks.DynamoAttributeValue.fromString(sfn.JsonPath.stringAt("$$.State.EnteredTime")),
        },
        resultPath: sfn.JsonPath.DISCARD,
      });
      // These writes are the SIGKILL backstop — without a Retry policy a
      // single DDB throttle fails the whole execution and the row wedges at
      // "provisioning". Retriers evaluate IN ORDER, so the fencing signal
      // (ConditionalCheckFailed = the row was taken over) is excluded FIRST
      // with maxAttempts 0 — it must fall through to the Superseded catch,
      // never be retried; every other error is transient and retried.
      write.addRetry({
        errors: ["DynamoDB.ConditionalCheckFailedException"],
        maxAttempts: 0,
      });
      write.addRetry({
        errors: ["States.ALL"],
        interval: cdk.Duration.seconds(2),
        backoffRate: 2,
        maxAttempts: 3,
      });
      return write;
    };

    const markFailedDeterministic = ddbWrite(
      "MarkFailedDeterministic",
      "SET #s = :err, substatus = :sub, retryable = :r, updated_at = :ts",
      {
        ":err": tasks.DynamoAttributeValue.fromString("error"),
        ":sub": tasks.DynamoAttributeValue.fromString("provision_failed"),
        ":r": tasks.DynamoAttributeValue.fromBoolean(false),
      },
    );
    const markFailedExhausted = ddbWrite(
      "MarkFailedExhausted",
      "SET #s = :err, substatus = :sub, retryable = :r, updated_at = :ts",
      {
        ":err": tasks.DynamoAttributeValue.fromString("error"),
        ":sub": tasks.DynamoAttributeValue.fromString("provision_failed"),
        ":r": tasks.DynamoAttributeValue.fromBoolean(true),
      },
    );
    const markRetrying = ddbWrite(
      "MarkRetrying",
      "SET #s = :err, substatus = :sub, retryable = :r, updated_at = :ts",
      {
        ":err": tasks.DynamoAttributeValue.fromString("error"),
        ":sub": tasks.DynamoAttributeValue.fromString("error_retrying"),
        ":r": tasks.DynamoAttributeValue.fromBoolean(true),
      },
    );
    // ConditionalCheckFailed on any write means a deliberate reprovision took
    // the row over — stop quietly, exactly the runner's superseded rule.
    // (Optimized-integration exception prefix is "DynamoDB", uppercase DB.)
    for (const write of [markFailedDeterministic, markFailedExhausted, markRetrying]) {
      write.addCatch(succeedSuperseded, {
        errors: ["DynamoDB.ConditionalCheckFailedException"],
        resultPath: sfn.JsonPath.DISCARD,
      });
    }
    markFailedDeterministic.next(failProvision);
    markFailedExhausted.next(failProvision);

    const bumpAttempt = new sfn.Pass(this, "BumpAttempt", {
      parameters: {
        "attempt.$": "States.MathAdd($.attempt, 1)",
        // Fresh per-retry fencing token (spec §7 hazard 1): the state
        // machine's task timeout does not guarantee the previous attempt's
        // ECS task is dead, so a straggler must be fenced out by something
        // OTHER than the (stable, reused) claim_id/execution name. The
        // runner re-stamps container_repo's provision_attempt_token to this
        // value as its first action every attempt
        // (container_repo.claim_attempt) — a still-running previous attempt
        // then fails its NEXT checkpoint with a stale token. claim_id and
        // the execution name stay untouched, preserving Branch-A idempotency
        // (core/services/provision_dispatch.py).
        "attempt_token.$": "States.UUID()",
        "claim_id.$": "$.claim_id",
        "command.$": "$.command",
        "owner_id.$": "$.owner_id",
      },
    });
    const wait60 = new sfn.Wait(this, "WaitBeforeRetry", {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(60)),
    });
    markRetrying.next(wait60);
    wait60.next(bumpAttempt);
    bumpAttempt.next(runProvisioner);

    const transientGate = new sfn.Choice(this, "AttemptsRemain")
      .when(sfn.Condition.numberLessThan("$.attempt", 2), markRetrying)
      .otherwise(markFailedExhausted);

    // Non-TaskFailed catches (timeout, launch failure): the Cause is not the
    // ECS task JSON, so skip parsing and treat as transient.
    const shapeForRetry = new sfn.Pass(this, "ShapeForRetry", {
      parameters: {
        "attempt.$": "$.attempt",
        "claim_id.$": "$.claim_id",
        "command.$": "$.command",
        "owner_id.$": "$.owner_id",
      },
    });
    shapeForRetry.next(transientGate);

    // TaskFailed: Cause is the stringified ECS task object — parse it and
    // classify on the container exit code (design doc §11: a non-zero
    // essential-container exit fails the .sync state with States.TaskFailed).
    const parseCause = new sfn.Pass(this, "ParseFailureCause", {
      parameters: {
        "attempt.$": "$.attempt",
        "cause.$": "States.StringToJson($.error.Cause)",
        "claim_id.$": "$.claim_id",
        "command.$": "$.command",
        "owner_id.$": "$.owner_id",
      },
    });
    const classify = new sfn.Choice(this, "ClassifyFailure")
      // MUST be the FIRST rule: a task that never started (EFS mount /
      // image-pull ResourceInitializationError) stops with NO exitCode in the
      // Cause, and a Choice rule referencing a missing path throws
      // States.Runtime — which Choice states cannot Catch, killing the
      // execution with no DDB write (row stuck at "provisioning"). That is
      // exactly the transient case the retry loop exists for.
      .when(
        sfn.Condition.not(sfn.Condition.isPresent("$.cause.Containers[0].ExitCode")),
        transientGate, // task never started (EFS mount / image pull) — no exit code, retry
      )
      .when(sfn.Condition.numberEquals("$.cause.Containers[0].ExitCode", 20), succeedSuperseded)
      .when(sfn.Condition.numberEquals("$.cause.Containers[0].ExitCode", 10), markFailedDeterministic)
      .otherwise(transientGate); // 11, SIGKILL-after-start (137), Spot reclaim
    parseCause.next(classify);

    runProvisioner.addCatch(shapeForRetry, {
      errors: ["States.Timeout"],
      resultPath: "$.error",
    });
    runProvisioner.addCatch(parseCause, {
      errors: ["States.TaskFailed"],
      resultPath: "$.error",
    });
    runProvisioner.addCatch(shapeForRetry, {
      errors: ["States.ALL"], // AmazonECS.Unknown launch failures etc.
      resultPath: "$.error",
    });

    runProvisioner.next(succeedProvisioned);

    // ------------------------------------------------------------------
    // Watch loop — custody from t=0.
    //
    // The backend starts this execution AT CLAIM TIME
    // (provision_dispatch._dispatch_inner Branch B) and then runs attempt 0
    // as its own child process, which is ~30s faster than an ECS cold
    // launch. So this machine's job is to OBSERVE the row: succeed when the
    // runner lands "running", stand down when the claim changes hands, take
    // the ECS leg when the row goes error+retryable, and take it
    // unconditionally after the tick backstop (a runner that died without
    // writing — a SIGKILLed backend task).
    //
    // Before this, the machine STARTED at RunProvisioner and the backend had
    // to hand each claim over per exit path. The common leg — a nonzero
    // runner exit — had no handoff at all: it relied on the frontend's ~3s
    // status poll to re-dispatch, which orphans the claim whenever no tab is
    // open, and a saturated inline queue suppressed even that. Dev broke
    // fleet-wide that way on 2026-08-13 during a Fargate Spot drought.
    //
    // watch_ticks is seeded HERE, not in the StartExecution input: Branch-A
    // idempotency requires that input to stay byte-identical per claim.
    const initWatch = new sfn.Pass(this, "InitWatch", {
      parameters: {
        "attempt.$": "$.attempt",
        "attempt_token.$": "$.attempt_token",
        "claim_id.$": "$.claim_id",
        "command.$": "$.command",
        "owner_id.$": "$.owner_id",
        watch_ticks: 0,
      },
    });
    const getRow = new tasks.DynamoGetItem(this, "GetContainerRow", {
      table: containersTableRef,
      key: {
        owner_id: tasks.DynamoAttributeValue.fromString(sfn.JsonPath.stringAt("$.owner_id")),
      },
      // The runner may have written the terminal row milliseconds ago; an
      // eventually-consistent read would burn a whole tick on stale data (or
      // mis-route on a half-visible write).
      consistentRead: true,
      resultPath: "$.row",
    });
    // Same reasoning as the ddbWrite retriers: a single DDB throttle must not
    // kill the watchdog and orphan the claim it exists to protect.
    getRow.addRetry({
      errors: ["States.ALL"],
      interval: cdk.Duration.seconds(2),
      backoffRate: 2,
      maxAttempts: 3,
    });
    const tickWatch = new sfn.Pass(this, "TickWatch", {
      parameters: {
        "attempt.$": "$.attempt",
        "attempt_token.$": "$.attempt_token",
        "claim_id.$": "$.claim_id",
        "command.$": "$.command",
        "owner_id.$": "$.owner_id",
        "watch_ticks.$": "States.MathAdd($.watch_ticks, 1)",
      },
    });
    const waitWatch = new sfn.Wait(this, "WaitWatchTick", {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(60)),
    });
    // EVERY rule below is ordered so that no comparison can reference a path
    // that might be absent. That is not style: a Choice rule on a missing
    // path throws States.Runtime, Choice states cannot Catch, and the
    // execution would die with the row still leased — reintroducing the exact
    // orphan this loop exists to prevent (the same hazard ClassifyFailure's
    // IsPresent guard documents below).
    //
    // ASL guarantees rules evaluate IN ORDER, and AWS documents that an
    // IsPresent member short-circuits the rest of its And. Either mechanism
    // is sufficient; this uses ordering because a flat list of single-purpose
    // rules reads better than nested compound conditions, and it stays
    // correct without depending on the short-circuit at all.
    const watchChoice = new sfn.Choice(this, "WatchRow")
      // Row deleted mid-provision (account wipe / delete): nothing to drive.
      .when(sfn.Condition.isNotPresent("$.row.Item"), succeedSuperseded)
      // Defensive: a row with no status at all. DynamoDB UpdateItem CREATES
      // the item when absent, so any unconditioned writer that touches an
      // owner with no row can leave one with no status. Stand down rather
      // than let a later rule throw.
      .when(sfn.Condition.isNotPresent("$.row.Item.status.S"), succeedSuperseded)
      // Terminal success. MUST precede the claim rules: the running
      // transition REMOVEs the lease fields
      // (container_repo.update_container_leased), so checking the claim first
      // would read a successful provision as "superseded".
      .when(sfn.Condition.stringEquals("$.row.Item.status.S", "running"), succeedProvisioned)
      // Something deliberately stopped this box (trial park, conversion
      // revert, admin stop). update_status is NOT claim-conditioned, so this
      // CAN land on a row still holding our claim — and without this rule the
      // tick backstop below would resurrect a container someone chose to
      // stop. Never fight that decision from a watchdog: recovery for a
      // wrongly-stopped row already belongs to the status poll's auto-retry
      // and release_stranded_stop_to_error.
      .when(sfn.Condition.stringEquals("$.row.Item.status.S", "stopped"), succeedSuperseded)
      // Lease released by someone else — stand down.
      .when(sfn.Condition.isNotPresent("$.row.Item.provision_claim_id.S"), succeedSuperseded)
      // The claim changed hands (deliberate reprovision, conversion takeover,
      // admin door) — the runner's superseded rule. Safe to compare: the rule
      // above already established the attribute exists.
      .when(
        sfn.Condition.not(sfn.Condition.stringEqualsJsonPath("$.row.Item.provision_claim_id.S", "$.claim_id")),
        succeedSuperseded,
      )
      // Failed attempt: take the ECS leg. attempt stays 0 and attempt_token
      // stays claim_id — DISTINCT from the inline token ({claim}-inline) — so
      // the runner's first-act claim_attempt re-stamp fences any inline
      // straggler exactly as provision_dispatch.py documents.
      //
      // Deliberately does NOT branch on the row's `retryable` flag to
      // short-cut a deterministic failure straight to Fail. Such a rule would
      // be safe to write (guard $.row.Item.retryable.BOOL with IsPresent —
      // it is absent on rows predating the field), but it buys only the
      // skipping of one ECS attempt: the deterministic case already
      // terminates correctly via the proven route, where the runner exits 10
      // and ClassifyFailure routes to MarkFailedDeterministic. The row is
      // honest either way, so the user-visible error is identical. Add the
      // branch only if that wasted attempt ever shows up as a real cost.
      .when(sfn.Condition.stringEquals("$.row.Item.status.S", "error"), runProvisioner)
      // Dead-runner backstop: still leased to us, still "provisioning", no
      // progress for 40 ticks. The inline child cannot still be alive —
      // 40 x 60s = 2400s exceeds the backend's queue bound + attempt timeout
      // (300 + 1800 = 2100s, provision_dispatch.py) — so running the ECS leg
      // here cannot collide with a live attempt. Move one, move all three.
      .when(sfn.Condition.numberGreaterThanEquals("$.watch_ticks", 40), runProvisioner)
      .otherwise(waitWatch);
    initWatch.next(getRow);
    getRow.next(watchChoice);
    waitWatch.next(tickWatch);
    tickWatch.next(getRow);
    // The read must not be able to kill the watchdog. The retries above cover
    // ~14s; a longer DynamoDB or KMS impairment would exhaust them, and with
    // no Catch the execution FAILS while the row is still leased — precisely
    // the orphaned claim this loop exists to prevent, now caused by the loop
    // itself. Fall back into the wait/tick cycle instead: the read is retried
    // every 60s for as long as the execution lives, ticks keep accruing, and
    // a read that never recovers still reaches the backstop and runs the ECS
    // leg. DISCARD keeps the error off the state so tickWatch's rebuild (and
    // the stale $.row it drops) stay clean.
    getRow.addCatch(waitWatch, {
      errors: ["States.ALL"],
      resultPath: sfn.JsonPath.DISCARD,
    });

    const provisionStateMachine = new sfn.StateMachine(this, "ProvisionStateMachine", {
      stateMachineName: `${resourcePrefix(env)}provision`,
      role: new iam.Role(this, "ProvisionStateMachineRole", {
        roleName: `${resourcePrefix(env)}provision-state-machine`,
        assumedBy: new iam.ServicePrincipal("states.amazonaws.com"),
      }),
      definitionBody: sfn.DefinitionBody.fromChainable(initWatch),
      // 40 watch ticks (~40 min) + up to 3 x 30-min ECS attempts + retry
      // waits ~= 132 min. The old 2h ceiling would time the execution out
      // mid-final-attempt.
      timeout: cdk.Duration.hours(3),
    });

    // The MarkRetrying/MarkFailed* DynamoUpdateItem tasks auto-grant only
    // dynamodb:* — the containers table is CUSTOMER_MANAGED-encrypted and the
    // key crosses stacks as a bare ARN (the deliberate no-auto-grant posture,
    // see the header comment), so without this the state machine's every
    // backstop write dies with KMS AccessDenied. That failure mode is the
    // worst one this machine has: the write that exists to mark a failed
    // provision retryable/error itself fails, the execution hard-fails
    // instead of retrying, and the row wedges at provisioning/claimed until
    // the stale-lease clause frees it — observed on prod 2026-08-10 during a
    // trial->paid conversion. UpdateItem on an SSE-KMS table needs Decrypt
    // (read the row) and GenerateDataKey (write it back).
    provisionStateMachine.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ContainersTableKmsAccess",
        actions: ["kms:Decrypt", "kms:GenerateDataKey"],
        resources: [props.kmsKeyArn],
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ProvisionStateMachineDispatch",
        actions: ["states:StartExecution", "states:DescribeExecution", "states:StopExecution"],
        resources: [
          provisionStateMachine.stateMachineArn,
          // DescribeExecution/StopExecution operate on execution ARNs.
          `arn:aws:states:${this.region}:${this.account}:execution:${resourcePrefix(env)}provision:*`,
        ],
      }),
    );

    // Provisioning failures page. This is the signup path — a failed or timed
    // out execution means a new user never gets a container.
    //
    // The topic is imported from a CONSTRUCTED ARN string rather than a
    // cross-stack reference on purpose. ObservabilityStack (which owns the
    // topic) already takes this stack's backendService as a prop, so a real
    // reference in this direction would close a stack dependency cycle. Same
    // reasoning as the secret-names / KMS-key-ARNs handling elsewhere in this
    // stack: pass the identifier, not the construct. The topic name is
    // deterministic, so this stays correct as long as ObservabilityStack keeps
    // naming it `lightbulb-{env}-alerts-page`.
    const pageAction = new cloudwatch_actions.SnsAction(
      sns.Topic.fromTopicArn(
        this,
        "AlertsPageImported",
        alertTopicArn(this.region, this.account, env, "page"),
      ),
    );

    const provisionFailed = new cloudwatch.Alarm(this, "ProvisionExecutionsFailedAlarm", {
      alarmName: `lightbulb-${env}-provision-executions-failed`,
      alarmDescription:
        "PAGE: a provisioning Step Functions execution FAILED. New signups are " +
        "not getting containers.",
      metric: provisionStateMachine.metricFailed({ period: cdk.Duration.minutes(5) }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    provisionFailed.addAlarmAction(pageAction);

    const provisionTimedOut = new cloudwatch.Alarm(this, "ProvisionExecutionsTimedOutAlarm", {
      alarmName: `lightbulb-${env}-provision-executions-timedout`,
      alarmDescription:
        "PAGE: a provisioning Step Functions execution TIMED OUT. New signups " +
        "are not getting containers.",
      metric: provisionStateMachine.metricTimedOut({ period: cdk.Duration.minutes(5) }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    provisionTimedOut.addAlarmAction(pageAction);

    // A provision that neither fails nor times out can still strand a user:
    // the 2026-08-20 founder outage was a watchdog execution that ran 35
    // minutes and then SUCCEEDED, so neither alarm above fired while the
    // owner's container sat at zero the whole time. ExecutionTime is only
    // published when the execution closes, so this pages at close — it makes
    // a slow-but-eventually-successful provision impossible to miss rather
    // than shortening detection below the execution's own runtime (the P15
    // inline-timeout page covers the in-flight 1800s mark). Normal executions
    // close in 3-10 min; 20 min is comfortably above that.
    const provisionSlow = new cloudwatch.Alarm(this, "ProvisionExecutionSlowAlarm", {
      alarmName: `lightbulb-${env}-provision-executions-slow`,
      alarmDescription:
        "PAGE: a provisioning Step Functions execution took over 20 minutes " +
        "to close. An owner sat without a container for that whole time, " +
        "even though the execution eventually closed cleanly.",
      metric: provisionStateMachine.metricTime({
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 20 * 60 * 1000,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    provisionSlow.addAlarmAction(pageAction);

    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      family: `lightbulb-${env}-backend`,
      cpu: env === "prod" ? 2048 : 1024,
      memoryLimitMiB: env === "prod" ? 4096 : 2048,
      taskRole: this.taskRole,
      executionRole: taskExecutionRole,
    });

    // EFS Volume
    taskDef.addVolume({
      name: "efs-users",
      efsVolumeConfiguration: {
        fileSystemId: props.container.efsFileSystem.fileSystemId,
        transitEncryption: "ENABLED",
        authorizationConfig: { iam: "ENABLED" },
      },
    });

    // Container definition
    const container = taskDef.addContainer("backend", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      // Run as root — backend writes to EFS user directories
      user: "0:0",
      portMappings: [{ containerPort: 8000 }],
      environment: {
        ...backendContainerEnvironment,
        // THE provisioning executor, active on merge — no flag (spec §7 /
        // onboarding-architecture-design.md PR-10). The state machine below
        // is dispatched by every dispatch_provision call via
        // core.services.provision_dispatch.
        PROVISION_STATE_MACHINE_ARN: provisionStateMachine.stateMachineArn,
      },
      secrets: backendContainerSecrets,
      healthCheck: {
        command: [
          "CMD-SHELL",
          "curl -f http://localhost:8000/health || exit 1",
        ],
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "backend",
        logGroup,
      }),
      // SIGTERM->SIGKILL window for graceful shutdown. Sits at the top of the
      // timing ladder: app asyncio budget (20s) < uvicorn --timeout-graceful-shutdown
      // (25s) < this stopTimeout (30s), so the app + uvicorn drain in-flight work
      // before ECS hard-kills the task.
      stopTimeout: cdk.Duration.seconds(30),
    });

    // EFS mount point
    container.addMountPoints({
      containerPath: "/mnt/efs",
      sourceVolume: "efs-users",
      readOnly: false,
    });

    // -------------------------------------------------------------------------
    // Fargate Service
    // -------------------------------------------------------------------------
    this.service = new ecs.FargateService(this, "Service", {
      serviceName: `lightbulb-${env}-backend`,
      cluster: props.container.cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      // Rolling deploy as a clean handoff: at desiredCount=1, keeping 100%
      // healthy forces ECS to bring the new task to healthy BEFORE stopping
      // the old one, and 200% gives the headroom to run both at once. So a
      // deploy has no outage window. Circuit breaker still rolls back a bad
      // rollout.
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      circuitBreaker: { rollback: true },
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [serviceSg],
      assignPublicIp: false,
      enableExecuteCommand: true,
    });

    // Register with ALB target group
    this.service.attachToApplicationTargetGroup(props.targetGroup);

    // -------------------------------------------------------------------------
    // Tags
    // -------------------------------------------------------------------------
    cdk.Tags.of(this.service).add("Name", `lightbulb-${env}-backend`);
    cdk.Tags.of(this.service).add("Project", "lightbulb");
    cdk.Tags.of(this.service).add("Environment", env);
  }
}
