import * as path from "path";
import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatch_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as events from "aws-cdk-lib/aws-events";
import * as events_targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as sns from "aws-cdk-lib/aws-sns";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import { superAgentEnabled } from "../super-agent";
import {
  allTableArns,
  alertTopicArn,
  resourcePrefix,
  tableName,
  tablePrefix,
} from "../table-names";
import { Construct } from "constructs";
import { DEV_SPOT } from "./container-stack";
import { LAUNCH_DOMAIN, LEGACY_MAIL_DOMAIN, PLATFORM_MAIL_DOMAIN, ROOT_DOMAIN } from "../domains";

// hermes-version.json is the per-env promotion tag for the Hermes bridge image (M4-R34),
// same pattern as openclaw-version.json's dev.tag/prod.tag: build-hermes-image.yml pushes a
// uniquely-tagged candidate (h-<date>-<sha7>) but never writes this file — a human bumps
// dev.tag (then prod.tag) in a PR to promote it. prod.tag stays "" until M6.
interface HermesVersionConfig {
  dev: { tag: string };
  prod: { tag: string };
  [key: string]: unknown;
}

const HERMES_VERSION: HermesVersionConfig = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "..", "..", "..", "hermes-version.json"), "utf8"),
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
  openaiApiKey: string;
  livekitApiKey: string;
  livekitApiSecret: string;
  recallApiKey: string;
  recallWebhookSecret: string;
  photonApiKey: string;
  photonProjectId: string;
  photonWebhookSecret: string;
  huddleWorkerSecret: string;
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
  /** Seed of the Ed25519 key that signs `lightbulb login` proofs (backend network_proof). */
  networkProofKey: string;
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
    /**
     * The VPC security group every workspace's AgentCore runtime ENI carries.
     * Named for the retired per-user ECS task it was created for, but it is
     * LIVE on the Hermes path: `hermes_provisioning.build_runtime_request_for_owner`
     * puts it (and the private subnets below) into `CreateAgentRuntime`'s
     * `networkConfiguration.networkModeConfig`, so an empty value means every
     * new or rolled runtime gets no VPC config. Renaming it is a separate
     * coordinated change, not part of the deletion program.
     */
    containerSecurityGroup: ec2.ISecurityGroup;
    // Per-owner Bedrock enforcement (Plan 2): the backend reads these ARNs to
    // condition iam:CreateRole on the boundary and to attach the base policy to
    // every minted per-owner role. Live construct refs (same CDK stage), NOT
    // CfnOutput/ImportValue — matching how taskRole is already passed through.
    ownerBedrockBasePolicy: iam.IManagedPolicy;
    ownerBedrockBoundary: iam.IManagedPolicy;
    /** The env's shared AgentCore Memory id (ContainerStack.workspaceMemory). One export, not two:
     *  the ARN is derived here, so the container stack pins as little as possible. */
    workspaceMemoryId: string;
    /** The env's uploaded-sessions AgentCore Memory id (ContainerStack.sessionsMemory): the evaluator's. */
    sessionsMemoryId: string;
    /** The env's workspace browser id (ContainerStack.workspaceBrowser, VPC mode). */
    workspaceBrowserId: string;
  };
  managementApiUrl: string;
  /** Public wss:// endpoint returned by /api/v1/desktop/pair so desktop clients
   * know where to connect. Sourced from ApiStack.webSocketUrl. */
  webSocketUrl: string;
  /** Public wss:// machine relay endpoint (desktop node hosts + Paperclip
   * operators) returned by /api/v1/desktop/pair's relayUrl field. Sourced
   * from ApiStack.gatewayRelayUrl. */
  gatewayRelayUrl: string;
  connectionsTableName: string;
  wsApiId: string;
  wsStage: string;
  messagingTableName: string;
  /** Message search collection; empty/undefined while search is off for this env. */
  messageSearchEndpoint?: string;
  messageSearchCollectionArn?: string;
  openClawWorkQueueUrl: string;
  openClawWorkQueueArn: string;
  appsyncEventsHttpEndpoint: string;
  appsyncEventsRealtimeEndpoint: string;
  appsyncEventsApiArn: string;
  appsyncEventsRegion: string;
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
   * The Lightbulb workspace's Clerk org id (lightbulb-stage.ts VENDOR_ORG_ID_BY_ENV).
   * Every other org gets a channel shared with it. Empty ⇒ feature off.
   */
  vendorOrgId?: string;
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
  /** Apps: hostname suffix (`{name}.{appsDomain}`) and the Route53 zone holding it. */
  appsDomain?: string;
  appsHostedZoneId?: string;
  /** LiveKit server URL for huddles. Empty (default) keeps huddle routes 503 — ships dark. */
  livekitUrl?: string;
  /** Agent name huddle/meeting dispatches target. Unset keeps the backend default (prod's lightbulb-huddle). */
  livekitAgentName?: string;
  /**
   * The Accounts v2 API version pinned on the company account/account-link
   * calls (`Stripe-Version` header). Empty ⇒ the provisioning preview.
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

/** Envs whose workspace sessions run on the VPC-mode AgentCore browser (fixed egress address).
 *  Add an env only after its runtimes are rolled onto an image built from #4391 or later. */
const VPC_BROWSER_ENVS = new Set<string>(["dev", "prod"]);

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

    // A desktop node's WebSocket is pinned to the task that accepted it, but the owner's Hermes
    // runtime calls the node-invoke route through the ALB and lands on any task. That task
    // forwards to the socket's owner using the address on the presence row -- which needs the
    // service to be able to reach itself.
    serviceSg.addIngressRule(
      serviceSg,
      ec2.Port.tcp(8000),
      "Peer backend task forwarding (node invoke)",
    );

    // The owner's runtime dials the task holding the Mac's native socket directly (the ticket
    // names that task's address), so the runtime SG must reach 8000 on the service.
    serviceSg.addIngressRule(
      props.container.containerSecurityGroup,
      ec2.Port.tcp(8000),
      "AgentCore runtimes to the ticketed Mac CDP door on the task holding the Macs socket",
    );

    // -------------------------------------------------------------------------
    // IAM Task Role
    // -------------------------------------------------------------------------
    this.taskRole = new iam.Role(this, "TaskRole", {
      roleName: `lightbulb-${env}-backend-task`,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: `Lightbulb ${env} Fargate task role`,
    });

    // Messaging is kept in a dedicated policy so the already size-bound inline
    // role policy does not repack. The backend consumes work and publishes live
    // events; the DynamoDB outbox Pipe remains the sole work-queue producer.
    const messagingPolicy = new iam.ManagedPolicy(this, "BackendMessaging", {
      managedPolicyName: `${resourcePrefix(env)}backend-messaging`,
      roles: [this.taskRole],
      statements: [
        new iam.PolicyStatement({
          sid: "ConsumeOpenClawWork",
          actions: [
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            "sqs:ChangeMessageVisibility",
            "sqs:GetQueueAttributes",
          ],
          resources: [props.openClawWorkQueueArn],
        }),
        new iam.PolicyStatement({
          sid: "PublishMessagingEvents",
          actions: ["appsync:EventPublish"],
          // `pub`: a public session link's frames (session_screen.forward). Owner runtime roles get `ws` only.
          resources: [
            `${props.appsyncEventsApiArn}/channelNamespace/ws`,
            `${props.appsyncEventsApiArn}/channelNamespace/pub`,
          ],
        }),
        ...(props.messageSearchCollectionArn
          ? [
              new iam.PolicyStatement({
                sid: "SearchMessages",
                actions: ["aoss:APIAccessAll"],
                resources: [props.messageSearchCollectionArn],
              }),
            ]
          : []),
      ],
    });

    // Bucket-level LIST on the per-owner state bucket. Hermes needs it: every
    // profile read/write/delete path (hermes_profiles, hermes_provisioning)
    // paginates list_objects_v2 under <owner>/hermes/, and ListBucket is a
    // BUCKET-scoped action, so it cannot be folded into HermesProfilesRW's
    // object-level prefix grant below. The sid keeps its litestream name: the
    // bucket is `lightbulb-{env}-openclaw-replicas`, a naming scar that must
    // not be renamed (every profile lives under it).
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "LitestreamReplicaLifecycle",
        actions: ["s3:ListBucket"],
        resources: [`arn:aws:s3:::lightbulb-${env}-openclaw-replicas`],
      }),
    );
    // IAM PassRole for the per-owner Bedrock roles the backend mints at provision
    // time — each one doubles as its workspace's AgentCore Runtime EXECUTION role,
    // and CreateAgentRuntime fails AccessDenied on PassRole without this.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IamPassRole",
        actions: ["iam:PassRole"],
        resources: [
          `arn:aws:iam::${this.account}:role/lightbulb-${env}-owner-bedrock-*`,
        ],
        conditions: {
          StringEquals: {
            // The one service that may receive it, not a wildcard: a role passable
            // to any service is a privilege-escalation lane.
            "iam:PassedToService": ["bedrock-agentcore.amazonaws.com"],
          },
        },
      }),
    );

    // AgentCore Runtime lifecycle, per workspace. Control plane creates/updates/deletes the
    // owner's runtime; data plane invokes it and stops its session for sleep. Scoped to this
    // account's runtimes; List has no resource-level scoping in the API.
    //
    // CreateAgentRuntime is not one action: AWS also creates the runtime's DEFAULT endpoint
    // and its workload identity ON THE CALLER'S BEHALF (CloudTrail shows CreateWorkloadIdentity
    // with invokedBy bedrock-agentcore.amazonaws.com under the caller's identity), and
    // DeleteAgentRuntime mirrors both. Missing either is an AccessDenied on the create.
    const agentcoreArn = (suffix: string) => `arn:aws:bedrock-agentcore:${this.region}:${this.account}:${suffix}`;
    // Every AgentCore grant lives in its OWN managed policy: the task role's inline default
    // policy sits at IAM's 10,240-byte per-role aggregate (dev 10,137 B on 2026-09-23), and
    // CDK's overflow split misjudges the resolved size -- #2929's dev deploy failed on it.
    const agentCorePolicy = new iam.ManagedPolicy(this, "BackendAgentCore", { roles: [this.taskRole] });
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreRuntimeManage",
        actions: [
          "bedrock-agentcore:CreateAgentRuntime",
          "bedrock-agentcore:GetAgentRuntime",
          "bedrock-agentcore:UpdateAgentRuntime",
          "bedrock-agentcore:DeleteAgentRuntime",
          "bedrock-agentcore:CreateAgentRuntimeEndpoint",
          "bedrock-agentcore:GetAgentRuntimeEndpoint",
          "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
          "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
          "bedrock-agentcore:ListAgentRuntimeEndpoints",
          "bedrock-agentcore:InvokeAgentRuntime",
          "bedrock-agentcore:StopRuntimeSession",
          // The create request tags the runtime (owner, env); Tag/Untag are separate actions.
          "bedrock-agentcore:TagResource",
          "bedrock-agentcore:UntagResource",
          "bedrock-agentcore:ListTagsForResource",
        ],
        resources: [agentcoreArn("runtime/*"), agentcoreArn("runtime/*/runtime-endpoint/*")],
      }),
    );
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreWorkloadIdentityManage",
        actions: [
          "bedrock-agentcore:CreateWorkloadIdentity",
          "bedrock-agentcore:GetWorkloadIdentity",
          "bedrock-agentcore:DeleteWorkloadIdentity",
          // The create request's tags are copied onto the auto-created workload identity
          // too, under the caller's identity: without Tag* here CreateAgentRuntime 403s
          // with "not authorized to perform bedrock-agentcore:TagResource on
          // workload-identity-directory/default/workload-identity/*" (first dev migration).
          "bedrock-agentcore:TagResource",
          "bedrock-agentcore:UntagResource",
          "bedrock-agentcore:ListTagsForResource",
        ],
        resources: [
          agentcoreArn("workload-identity-directory/default"),
          agentcoreArn("workload-identity-directory/default/workload-identity/*"),
        ],
      }),
    );
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreRuntimeList",
        actions: ["bedrock-agentcore:ListAgentRuntimes", "bedrock-agentcore:ListWorkloadIdentities"],
        resources: ["*"],
      }),
    );

    // AgentCore Browser: the backend mints/reads the per-workspace profile and owns the
    // session lifecycle; the session id lives on the workspace's browser row, so it lists none.
    // Sessions start on the env's VPC browser (ContainerStack.workspaceBrowser); the built-in
    // browser stays granted so a session started on it before the move can still be viewed and
    // stopped.
    const browserArns = [
      agentcoreArn(`browser-custom/${props.container.workspaceBrowserId}`),
      `arn:aws:bedrock-agentcore:${this.region}:aws:browser/aws.browser.v1`,
    ];
    // Browser-profile state snapshots use the released agent-browser CLI; no
    // SaveBrowserSessionProfile grant.
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreBrowserManage",
        actions: [
          "bedrock-agentcore:CreateBrowserProfile",
          "bedrock-agentcore:GetBrowserProfile",
          "bedrock-agentcore:ListBrowserProfiles",
          // Account wipe must delete the profile along with the cookies/localStorage it holds
          // (backend PR #2793's reap_owner_bedrock_resources is the caller).
          "bedrock-agentcore:DeleteBrowserProfile",
        ],
        resources: [agentcoreArn("browser-profile/*")],
      }),
    );
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreBrowserSessionView",
        actions: [
          "bedrock-agentcore:GetBrowserSession",
          "bedrock-agentcore:ConnectBrowserLiveViewStream",
        ],
        resources: browserArns,
      }),
    );
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreBrowserSessionStart",
        actions: ["bedrock-agentcore:StartBrowserSession"],
        resources: [...browserArns, agentcoreArn("browser-profile/*")],
      }),
    );
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreBrowserSessionControl",
        actions: [
          "bedrock-agentcore:StopBrowserSession",
          "bedrock-agentcore:UpdateBrowserStream",
          "bedrock-agentcore:ConnectBrowserAutomationStream",
        ],
        resources: browserArns,
      }),
    );

    // Workspace memory wipe (account_wipe / DELETE /debug/user-data -> workspace_memory.
    // delete_owner_memory). This statement is the wipe only; recall has its own below.
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreMemoryWipe",
        actions: [
          "bedrock-agentcore:ListMemoryRecords",
          "bedrock-agentcore:BatchDeleteMemoryRecords",
          "bedrock-agentcore:ListSessions",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:DeleteEvent",
          // Finds each member's private scope (actorId <owner>:<user>) for the owner wipe.
          "bedrock-agentcore:ListActors",
        ],
        resources: [agentcoreArn(`memory/${props.container.workspaceMemoryId}`)],
      }),
    );

    // Memory review (routers/memory.py -> workspace_memory): the backend lists a viewer's two
    // namespaces, reads ONE record to prove it sits in the viewer's own scope before any delete
    // or copy (BatchDelete/BatchCreate take bare ids), and writes the vouched team copy that
    // "Share with team" / "Confirm" produce. Runtimes stay denied every record write, so only this
    // role can mark a fact confirmed.
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreMemoryReview",
        actions: ["bedrock-agentcore:GetMemoryRecord", "bedrock-agentcore:BatchCreateMemoryRecords"],
        resources: [agentcoreArn(`memory/${props.container.workspaceMemoryId}`)],
      }),
    );

    // Memory sync (memory_sync._mark_replaced): an entry the network log replaced is marked
    // `replaced` on its mirrored record, in place.
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreMemorySync",
        actions: ["bedrock-agentcore:BatchUpdateMemoryRecords"],
        resources: [agentcoreArn(`memory/${props.container.workspaceMemoryId}`)],
      }),
    );

    // The evaluator (core/services/evaluator.py) on the sessions memory, which only the backend
    // touches: writes uploaded sessions as events, lists reflections into drafts, and deletes a
    // person's episodes and events on a wipe.
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreSessionsMemory",
        actions: [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListMemoryRecords",
          "bedrock-agentcore:BatchDeleteMemoryRecords",
          "bedrock-agentcore:ListSessions",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:DeleteEvent",
        ],
        resources: [agentcoreArn(`memory/${props.container.sessionsMemoryId}`)],
      }),
    );

    // Workspace recall: the backend now searches the memory (RetrieveMemoryRecords), writes room
    // conversation in as events for extraction (CreateEvent), and reranks the hits with Cohere on
    // Bedrock (foundation-model ARNs carry no account).
    agentCorePolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AgentCoreMemoryRecall",
        actions: ["bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:CreateEvent"],
        resources: [agentcoreArn(`memory/${props.container.workspaceMemoryId}`)],
      }),
      new iam.PolicyStatement({
        sid: "MemoryRerank",
        actions: ["bedrock:Rerank", "bedrock:InvokeModel"],
        resources: [`arn:aws:bedrock:${this.region}::foundation-model/cohere.rerank-v3-5:0`],
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
    // these inline would tip CDK into spilling statements (e.g. the
    // DynamoDbOwnedTables wildcard grant) into an overflow ManagedPolicy —
    // silently relocating them out of the AWS::IAM::Policy resource other
    // features rely on. A standalone, explicitly named ManagedPolicy keeps
    // the inline policy stable and is more auditable. CreateRole is
    // boundary-conditioned so a backend that can CreateRole+PutRolePolicy
    // cannot escalate beyond the OwnerBedrockBoundary (Bedrock+EFS+logs+ssm).
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
            // delete_owner_bedrock_role ENUMERATES inline policies too (#2793 --
            // owner-browser/owner-memory are inline), and IAM refuses DeleteRole while
            // any remain. Without this every owner-role teardown AccessDenies.
            "iam:ListRolePolicies",
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

    // Apps DNS (core/services/apps_dns.py). Standalone ManagedPolicy: the inline
    // task-role policy is near its 10 KB ceiling. Scoped to the one hosted zone,
    // and changes to this env's app names plus Vercel's `_vercel.*` verification
    // TXTs: dev and prod share the zone (R60). The names condition key only
    // exists on ChangeResourceRecordSets, so the list stays unconditioned.
    if (props.appsHostedZoneId) {
      const zone = `arn:aws:route53:::hostedzone/${props.appsHostedZoneId}`;
      const appsDomain = (props.appsDomain ?? "apps.thelightbulb.company").toLowerCase();
      new iam.ManagedPolicy(this, "BackendAppsDns", {
        managedPolicyName: `lightbulb-${env}-backend-apps-dns`,
        roles: [this.taskRole],
        statements: [
          new iam.PolicyStatement({
            sid: "AppsDnsRecords",
            actions: ["route53:ChangeResourceRecordSets"],
            resources: [zone],
            conditions: {
              "ForAllValues:StringLike": {
                // Normalized names: lowercase, no trailing dot.
                "route53:ChangeResourceRecordSetsNormalizedRecordNames": [`*.${appsDomain}`, "_vercel.*"],
              },
            },
          }),
          new iam.PolicyStatement({
            sid: "AppsDnsList",
            actions: ["route53:ListResourceRecordSets"],
            resources: [zone],
          }),
        ],
      });
    }

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

    // Secrets Manager -- its own managed policy for the same 10,240-byte inline ceiling as
    // BackendAgentCore above.
    const secretsPolicy = new iam.ManagedPolicy(this, "BackendSecrets", { roles: [this.taskRole] });
    secretsPolicy.addStatements(
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
    // The ECS execution role injects this key. The task role is also used by
    // provisioner/work Lambdas, so its broad SecretsAccess must not expose it.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "DenyHookGrantKeyRead",
        effect: iam.Effect.DENY,
        actions: ["secretsmanager:GetSecretValue"],
        resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/hook_grant_signing_key-*`],
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
    secretsPolicy.addStatements(
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
    secretsPolicy.addStatements(
      new iam.PolicyStatement({
        sid: "AccountWipePaperclipDbSecretRead",
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb-${env}-paperclip-db-credentials-*`,
        ],
      }),
    );
    secretsPolicy.addStatements(
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
    secretsPolicy.addStatements(
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

    secretsPolicy.addStatements(
      new iam.PolicyStatement({
        sid: "OpenRouterOwnerSecretsRW",
        effect: iam.Effect.ALLOW,
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:DeleteSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/openrouter/owners/*`,
        ],
      }),
    );

    // Per-owner integration credential secrets (credential-layer redesign):
    // paste-key tokens and backend-custodian OAuth credentials live under
    // isol8/{env}/integrations/*. The secret-free integrations DDB index points
    // here. Same tight prefix scoping as the user-keys statement above.
    secretsPolicy.addStatements(
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

    // LEGACY per-agent Paperclip API keys. New keys live Fernet-encrypted on the
    // owner's paperclip-companies row (paperclip_api_key.py); this prefix is only
    // read as a fallback, swept by the wipes, and migrated + scheduled for deletion
    // by scripts/migrate_agent_keys_to_rows.py (needs Get + Delete). Remove this
    // statement, the fallback and the wipe sweeps once the prefix is empty.
    secretsPolicy.addStatements(
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

    // Hermes-on-AgentCore boot secrets: provisioning/migration writes ONE JSON secret per
    // workspace (lightbulb/{env}/hermes/{owner}/boot = hook + gateway tokens + gate bearer)
    // and account wipe / rollback delete it; the runtime role only READS it (container-stack
    // boundary grant). Found by the first dev migration run: the task role could read but
    // not create. Same tight prefix scoping as the statements above. (CDK packs the task
    // role's statements into the inline default policy and overflows into a managed policy
    // automatically — see the overflow note further down — so placement is not asserted.)
    secretsPolicy.addStatements(
      new iam.PolicyStatement({
        sid: "HermesBootSecretsRW",
        effect: iam.Effect.ALLOW,
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecretVersionStage",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:lightbulb/${env}/hermes/*`],
      }),
    );

    // Hermes profiles: the backend is the writer of every agent's S3 profile
    // (<owner>/hermes/<agent>/{config.yaml,.env,SOUL.md,lightbulb-facts.json,.revision,
    // mcp-tokens/...}, plus the root <owner>/hermes/auth.json), the reader behind the
    // config.get/agents.list intercept, and the deleter on agent delete / account wipe.
    // Scoped to the hermes/ sub-prefix: the litestream replica statements above deliberately
    // keep the backend out of the OpenClaw session/state databases.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "HermesProfilesRW",
        actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        resources: [
          `arn:aws:s3:::lightbulb-${env}-openclaw-replicas/*/hermes/*`,
          // The workspace's network member (plan C phase 1): the backend writes
          // <owner>/network/sessions.json, deletes state.db on connect/reconnect and the
          // whole prefix on teardown. The runtime reads and writes the same prefix under its
          // own per-owner role; nothing else of the owner's state is opened to the backend.
          `arn:aws:s3:::lightbulb-${env}-openclaw-replicas/*/network/*`,
          `arn:aws:s3:::lightbulb-${env}-openclaw-replicas/marketplace-installs/*`,
          `arn:aws:s3:::lightbulb-${env}-openclaw-replicas/browser-state/*`,
          // App source archives (core/services/apps_source.py): apps/<owner>/<app>/src/*.tar.gz.
          `arn:aws:s3:::lightbulb-${env}-openclaw-replicas/apps/*`,
        ],
      }),
    );

    // Super-agent per-task VMs (lane V): each task VM writes only its overlay
    // <owner>/hermes-tasks/<task>/<agent>/ under its own per-owner role. The backend's
    // LearningWorkflow reads it (base, seal, learning files) and, at task end, tags its objects
    // lb-kind=task-overlay so the replicas bucket's 14-day TaskOverlays rule expires them. Tagging
    // at task end, not per put, keeps a running or waiting task's overlay out of the rule (S3 counts
    // expiry from object creation). Read + tag only: the backend never writes overlay content.
    if (superAgentEnabled(env)) {
      this.taskRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "HermesTaskOverlaysReadTag",
          actions: ["s3:GetObject", "s3:PutObjectTagging"],
          resources: [`arn:aws:s3:::lightbulb-${env}-openclaw-replicas/*/hermes-tasks/*`],
        }),
      );
    }

    // Browser extensions (core/services/browser_extensions.py): every object a session loads
    // (extension ZIPs, our helper, the Chrome policy file) lives under the backend-only
    // browser-state/<owner>/ prefix -- the agent's runtime can write <owner>/*.
    // StartBrowserSession downloads them with the CALLER's credentials -- AgentCore requires
    // s3:GetObject + s3:GetObjectVersion on the prefix.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BrowserExtensionsRW",
        actions: ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject"],
        resources: [`arn:aws:s3:::lightbulb-${env}-openclaw-replicas/browser-state/*`],
      }),
    );
    // #3287 stored ZIPs under <owner>/browser-extensions/; browser_extensions._current moves
    // them (copy, then delete). Remove this grant once no row names that prefix.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BrowserExtensionsLegacyMove",
        actions: ["s3:GetObject", "s3:DeleteObject"],
        resources: [`arn:aws:s3:::lightbulb-${env}-openclaw-replicas/*/browser-extensions/*`],
      }),
    );

    // Session pick-up: a teammate's raw Claude Code transcript + a git bundle of their
    // working tree, backend-only like browser-state/<owner>/ above. Every workspace's
    // agent runtime role is granted <owner>/*, so this prefix must stay OUTSIDE <owner>/
    // or an agent's shell could read another teammate's transcript. No s3:ListBucket here
    // -- LitestreamReplicaLifecycle already grants it bucket-wide.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SessionPickupRW",
        actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        resources: [`arn:aws:s3:::lightbulb-${env}-openclaw-replicas/session-pickup/*`],
      }),
    );
    // Uploaded coding sessions (session_store.py): sessions/<free|workspace>/<user>/<id>.json, the
    // full uploads the lifecycle rules age out. Backend-only, outside every <owner>/ prefix.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SessionsRW",
        actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        resources: [`arn:aws:s3:::lightbulb-${env}-openclaw-replicas/sessions/*`],
      }),
    );
    // Redacted copies of a shared session for a link's holder (session_links.py): an id-free
    // prefix, so a presigned URL names no owner or session. The sweeper ages them out.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SessionCopiesRW",
        actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        resources: [`arn:aws:s3:::lightbulb-${env}-openclaw-replicas/session-copies/*`],
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
          "dynamodb:TransactWriteItems",
          "dynamodb:DescribeTable",
        ],
        resources: allTableArns(this.region, this.account, env),
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

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "OpenRouterGateUrlParamRead",
        actions: ["ssm:GetParameter"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter/lightbulb/${env}/openrouter-gate-url`,
        ],
      }),
    );

    // Platform-unique company slugs (company-identity Task 1). Deliberately
    // granted here — AFTER the gate-url statement above, not beside
    // the other table grants earlier in this method — because CDK's inline-
    // policy overflow packs statements into TaskRoleDefaultPolicy in
    // add-order and spills whatever doesn't fit into a TaskRoleOverflowPolicy
    // once the ~10 KB ceiling is hit (confirmed via synth: this stack is
    // already at that ceiling). Adding this grant any earlier pushed
    // the gate-url statement itself into the overflow policy, which broke
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
    // env.startsWith, not ===, so this stays correct if a prod-prefixed variant is ever
    // introduced.
    const currentHermesTag = env.startsWith("prod") ? HERMES_VERSION.prod.tag : HERMES_VERSION.dev.tag;
    const desktopOwner = process.env[`DESKTOP_EXECUTION_OWNER_${env.toUpperCase()}`] ?? "";
    if (desktopOwner && !/^(?:user|org)_[A-Za-z0-9_-]{1,123}$/.test(desktopOwner)) {
      throw new Error(`Invalid desktop execution owner for ${env}`);
    }

    // Backend container environment + secrets, declared as consts (rather
    // than inline literals) so the provisioning-executor task definition can
    // attach the exact same objects. core.config.Settings reads env vars only,
    // so identical env/secrets => identical runtime config for both processes,
    // and the two container definitions can never drift.
    const backendContainerEnvironment: Record<string, string> = {
      ENVIRONMENT: env,
      AWS_REGION: "us-east-1",
      // The per-owner state bucket — every Hermes profile lives under
      // <owner>/hermes/. The name is a naming scar from litestream and must NOT be
      // changed: a rename strands every profile.
      OWNER_STATE_BUCKET: `lightbulb-${env}-openclaw-replicas`,
      // The Hermes bridge image a workspace's AgentCore runtime runs. Per-env promotion
      // tag from hermes-version.json#dev.tag/#prod.tag (M4-R34). Empty tag (prod today)
      // makes provision_hermes_runtime refuse, so nothing moves a prod workspace until a
      // human flips prod.tag to an already-pushed h-<date>-<sha7> build.
      AGENTCORE_HERMES_IMAGE: currentHermesTag
        ? `${this.account}.dkr.ecr.${this.region}.amazonaws.com/lightbulb/hermes-agent-arm64:${currentHermesTag}`
        : "",
      // Stamped into every runtime spec as AGENTCORE_MEMORY_ID; the image enables its memory
      // provider only when it is set (N-1 safe: an older image ignores it).
      AGENTCORE_MEMORY_ID: props.container.workspaceMemoryId,
      // The evaluator's own memory (uploaded sessions); backend only, never stamped into a runtime.
      AGENTCORE_SESSIONS_MEMORY_ID: props.container.sessionsMemoryId,
      // The browser every workspace session starts on (bedrock_aip.BROWSER_ID); unset = the
      // built-in one. Per env, and only once every runtime there runs a bridge that accepts a
      // custom browser's CDP URL (#4391): an older bridge refuses it and the browser fails.
      ...(VPC_BROWSER_ENVS.has(env) ? { AGENTCORE_BROWSER_ID: props.container.workspaceBrowserId } : {}),
      WS_MANAGEMENT_API_URL: props.managementApiUrl,
      WS_GATEWAY_URL: props.webSocketUrl,
      GATEWAY_RELAY_URL: props.gatewayRelayUrl,
      WS_CONNECTIONS_TABLE: props.connectionsTableName,
      MESSAGING_TABLE: props.messagingTableName,
      SEARCH_ENDPOINT: props.messageSearchEndpoint ?? "",
      OPENCLAW_WORK_QUEUE_URL: props.openClawWorkQueueUrl,
      APPSYNC_EVENTS_HTTP_ENDPOINT: props.appsyncEventsHttpEndpoint,
      APPSYNC_EVENTS_REALTIME_ENDPOINT: props.appsyncEventsRealtimeEndpoint,
      APPSYNC_EVENTS_REGION: props.appsyncEventsRegion,
      // WS caller-attestation gate, enforced in every env. The
      // x-lightbulb-ws-internal header is verified flowing from the WS Lambdas on
      // both dev and prod (zero secret-fetch failures across real connects),
      // so the backend rejects /ws/* callers that lack it — closing the
      // unauthenticated WebSocket-impersonation hole on the public HTTP
      // API Gateway $default path.
      WS_ENFORCE_INTERNAL_SECRET: "true",
      // One owner per env. An unset synth variable closes new submissions;
      // existing receipts remain readable/cancellable in the backend.
      DESKTOP_EXECUTION_ENABLED: desktopOwner ? "true" : "false",
      DESKTOP_EXECUTION_OWNER_ALLOWLIST: desktopOwner,
      // OpenAI's plugin-portal domain token (public by design). Unset = the path 404s.
      OPENAI_APPS_CHALLENGE_TOKEN: process.env[`OPENAI_APPS_CHALLENGE_TOKEN_${env.toUpperCase()}`] ?? "",
      // Platform admin (/admin, /ops, marketplace review, workspace wipe) is
      // this reviewed list of Clerk USER IDS per env, never an email domain.
      // Empty ⇒ nobody is admin. Comma-separated.
      // Ids are per Clerk instance. Prod: Prasiddha (team + gmail accounts),
      // Leo, Aden, admin@. Dev: Prasiddha (gmail), Aden, admin@ (Leo has no
      // dev account). Read from the founder workspace 2026-10-10.
      PLATFORM_ADMIN_USER_IDS:
        env === "prod"
          ? [
              "user_3IROHYIZPPS1G7ISXhjsnnKBEMX",
              "user_3HmnXQdFUk2pP2Zjw4HEGkefcNM",
              "user_3J8t0Yp76auS131Lk9ov9YCc5We",
              "user_3I8s15Ex1b9xin0keAJTMcoipm2",
              "user_3H3vhcZzM4vaNCe17zs0XXIrTMW",
            ].join(",")
          : [
              "user_3I4REo0WJ0GoGqQFN6gmKAN4Hqs",
              "user_3JISO0QYSpiaupJII105uDWMApg",
              "user_3IEMUT2a1z8za7m4lnFzpdZrccc",
            ].join(","),
      WEBHOOK_DEDUP_TABLE: tableName(env, "webhook-event-dedup"),
      CORS_ORIGINS:
        env === "prod"
          ? `https://${rootDomain},https://www.${rootDomain},https://${LAUNCH_DOMAIN},https://www.${LAUNCH_DOMAIN}`
          : `https://dev.${rootDomain},https://dev.${LAUNCH_DOMAIN}`,
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
      // Stripe flat-fee Price ID is environment-specific: prod is created in
      // Stripe live mode, dev in test mode, and they MUST NOT cross-pollinate
      // (a test-mode Price would silently create test subscriptions in prod).
      // Read per-env from the runner's secrets at synth time.
      STRIPE_WORKSPACE_PRICE_ID:
        env === "prod"
          ? (process.env.STRIPE_WORKSPACE_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_WORKSPACE_PRICE_ID_DEV ?? ""),
      STRIPE_BASE_PRICE_ID:
        env === "prod"
          ? (process.env.STRIPE_BASE_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_BASE_PRICE_ID_DEV ?? ""),
      // Stripe Ultra Price ID — same env-specific live/test split as
      // STRIPE_WORKSPACE_PRICE_ID above (a test-mode Price would silently create
      // test subscriptions in prod). Read per-env from the runner's secrets
      // at synth time. Consumed by the Ultra-tier checkout call site.
      STRIPE_ULTRA_PRICE_ID:
        env === "prod"
          ? (process.env.STRIPE_ULTRA_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_ULTRA_PRICE_ID_DEV ?? ""),
      // Per-member seat Price (T04), same live/test split. Empty fails closed.
      STRIPE_SEAT_PRICE_ID:
        env === "prod"
          ? (process.env.STRIPE_SEAT_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_SEAT_PRICE_ID_DEV ?? ""),
      // Front desk add-on Price, same live/test split. Empty fails closed.
      STRIPE_FRONT_DESK_PRICE_ID:
        env === "prod"
          ? (process.env.STRIPE_FRONT_DESK_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_FRONT_DESK_PRICE_ID_DEV ?? ""),
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
      // Apple IAP (StoreKit 2). Product ids live in apple_iap.PRODUCT_PLANS.
      // SELLING is the one switch that makes the iOS app offer purchases; prod
      // stays off until the Paid Apps Agreement is active. ACCEPT_XCODE lets
      // dev accept Xcode StoreKit Testing purchases (signed locally, not by
      // Apple) — it is a free plan for anyone who can reach it, so it is
      // hard-wired off for prod here AND refused by the backend in prod.
      APPLE_IAP_SELLING: env === "prod" ? "false" : "true",
      APPLE_IAP_ACCEPT_XCODE: env === "prod" ? "false" : "true",
      APPLE_IAP_BUNDLE_ID: "co.isol8.mobile",
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
      // The VPC placement of every workspace's AgentCore runtime. Both names are
      // ECS-era scars, and both are LIVE:
      // `hermes_provisioning.build_runtime_request_for_owner` reads them into
      // `CreateAgentRuntime`'s `networkConfiguration.networkModeConfig` on the fresh
      // provision AND on `roll_runtime_image`. Dropping either one does not fail loudly —
      // it hands the next runtime an empty VPC config. A rename is a coordinated
      // backend+CDK change of its own.
      ECS_SUBNETS: privateSubnetIds,
      ECS_SECURITY_GROUP_ID:
        props.container.containerSecurityGroup.securityGroupId,
      DYNAMODB_TABLE_PREFIX: tablePrefix(env),
      OAUTH_TOKENS_TABLE: tableName(env, "oauth-tokens"),
      // Metronome lifecycle + gating settings. Metronome is the ONLY
      // billing ledger: the backend provisions Metronome customers, funds
      // payment-gated top-up commits, handles the low-balance webhook,
      // lazily refreshes the gate, and displays balances — it ingests
      // voice-call usage (voice calls); the openrouter-settlement Lambda
      // ingests inference usage. Base URL is the same for
      // sandbox + prod (token decides env). Fast-gate table backs the
      // synchronous chat gate's balance projection.
      METRONOME_BASE_URL: "https://api.metronome.com/v1",
      // Literal name (not a cross-stack tableName Ref) — same posture as
      // BEDROCK_AIP_OWNERS_TABLE above: the table is covered by the
      // DynamoDbOwnedTables wildcard statement, so no cross-stack export is
      // created.
      METRONOME_FAST_GATE_TABLE: tableName(env, "metronome-fast-gate"),
      // owner_id -> Paperclip agent id that platform-failed, recovery-blocked issues
      // escalate to (core/services/paperclip_escalation.py). Prod: founder org -> Forge.
      PAPERCLIP_ESCALATION_AGENT_OVERRIDES: env === "prod"
        ? JSON.stringify({ org_3H3viGAfq2SXc9dIdcatmGXg8Td: "f293693d-1ec4-4dc3-bd3c-e54109e324bf" })
        : "{}",
      OPENROUTER_WORKSPACE_ID: env === "dev"
        ? (process.env.OPENROUTER_WORKSPACE_ID_DEV ?? "")
        : (process.env.OPENROUTER_WORKSPACE_ID_PROD ?? ""),
      // Voice debits reuse the managed-credit markup; its ONE copy is openrouter-pricing.json.
      OPENROUTER_MARKUP: JSON.parse(
        fs.readFileSync(path.join(__dirname, "..", "..", "..", "..", "openrouter-pricing.json"), "utf8"),
      ).markup,
      OPENROUTER_MANAGED_MODEL_ID: env === "dev"
        ? (process.env.OPENROUTER_MANAGED_MODEL_ID_DEV ?? "")
        : (process.env.OPENROUTER_MANAGED_MODEL_ID_PROD ?? ""),
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
      // The Stripe integration Metronome delivers invoices through. Dev has two (the legacy
      // live-account one can't be removed without Metronome support), and Metronome refuses a
      // billing config that doesn't name one; prod has one and names it for symmetry.
      METRONOME_STRIPE_DELIVERY_METHOD_ID:
        env === "dev"
          ? "155e12cb-3c9c-4a55-983b-958e65b3b489"
          : env === "prod"
            ? "5cb6c7ce-501b-4760-b35d-8d4bb6cfa8ce"
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
      // Platform publishable key (pk_…, public) for Connect embedded
      // onboarding, from the STRIPE_PUBLISHABLE_KEY_{DEV,PROD} repo variables.
      // Empty ⇒ the Stripe card falls back to the hosted Account Link.
      STRIPE_PUBLISHABLE_KEY: env === "dev"
        ? (process.env.STRIPE_PUBLISHABLE_KEY_DEV ?? "")
        : (process.env.STRIPE_PUBLISHABLE_KEY_PROD ?? ""),
      // Provider catalog partition. dev reads/writes Stripe's `testing`
      // partition, prod the real one. NOT a payment-mode switch — Stripe's own
      // caveat: "Selecting a DEV or TESTING catalog doesn't mean 'no real
      // charges.'"
      STRIPE_PROJECTS_CATALOG: props.stripeProjectsCatalog ?? "prod",
      APPS_DOMAIN: props.appsDomain ?? "apps.thelightbulb.company",
      APPS_HOSTED_ZONE_ID: props.appsHostedZoneId ?? "",
      LIVEKIT_URL: props.livekitUrl ?? "",
      ...(props.livekitAgentName ? { LIVEKIT_AGENT_NAME: props.livekitAgentName } : {}),
      // Accounts v2 API version pin for the company account/account-link
      // calls. Empty ⇒ the provisioning preview.
      STRIPE_ACCOUNTS_V2_VERSION: props.stripeAccountsV2Version ?? "",
      // Super-agent env master: set where the gate is on (dev) and absent elsewhere, so prod's
      // task definition is unchanged and the backend default (false) holds.
      ...(superAgentEnabled(env)
        ? {
            SUPER_AGENT_ENV_ENABLED: "true",
            // Temporal Cloud (contracts-v2 §16); the API key is TEMPORAL_API_KEY in backendContainerSecrets.
            TEMPORAL_ADDRESS: `lightbulb-${env}.nv5xx.tmprl.cloud:7233`,
            TEMPORAL_NAMESPACE: `lightbulb-${env}.nv5xx`,
            TEMPORAL_TASK_QUEUE: "lightbulb",
          }
        : {}),
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
      // The Lightbulb workspace's Clerk org id (lightbulb-stage.ts
      // VENDOR_ORG_ID_BY_ENV). Empty ⇒ the vendor-shared-channel feature is off.
      VENDOR_ORG_ID: props.vendorOrgId ?? "",
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
      // Keys bedrock_gate_tokens.derive_gate_bearer. By name, like the Stripe
      // Connect secret above: AuthStack deploys first and creates it. Same CMK,
      // so KmsDecryptForSecrets covers decrypt.
      GATE_BEARER_HMAC_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportGateBearerHmacKey", `lightbulb/${env}/gate_bearer_hmac_key`),
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
      OPENAI_API_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportOpenaiApiKey", props.secretNames.openaiApiKey),
      ),
      LIVEKIT_API_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportLivekitApiKey", props.secretNames.livekitApiKey),
      ),
      LIVEKIT_API_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportLivekitApiSecret", props.secretNames.livekitApiSecret),
      ),
      RECALL_API_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportRecallApiKey", props.secretNames.recallApiKey),
      ),
      RECALL_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportRecallWebhookSecret", props.secretNames.recallWebhookSecret),
      ),
      PHOTON_API_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportPhotonApiKey", props.secretNames.photonApiKey),
      ),
      PHOTON_PROJECT_ID: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportPhotonProjectId", props.secretNames.photonProjectId),
      ),
      PHOTON_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportPhotonWebhookSecret", props.secretNames.photonWebhookSecret),
      ),
      HUDDLE_WORKER_SECRET: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportHuddleWorkerSecret", props.secretNames.huddleWorkerSecret),
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
      // Signs `lightbulb login` proofs (network_proof). Same CMK as the other auth secrets.
      NETWORK_PROOF_KEY: ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretNameV2(this, "ImportNetworkProofKey", props.secretNames.networkProofKey),
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
      // Temporal Cloud API key (minted with tcld straight into Secrets Manager). Covered by the
      // lightbulb/{env}/* read grants like every other secret here.
      ...(superAgentEnabled(env)
        ? {
            TEMPORAL_API_KEY: ecs.Secret.fromSecretsManager(
              secretsmanager.Secret.fromSecretNameV2(this, "ImportTemporalApiKey", `lightbulb/${env}/temporal_api_key`),
            ),
          }
        : {}),
    };

    // ---------------------------------------------------------------------
    // Provisioning executor: a Lambda running scripts.provision_lambda from the
    // backend image, driven by the state machine below. Same image, env, secrets,
    // role and security group as the backend, so the runner has identical
    // capability OUTSIDE the deployable web process. Lambda, not an ECS RunTask:
    // the task's ~60 s cold start was most of a fresh provision.
    // ---------------------------------------------------------------------
    // Lambda has no ECS `secrets:` injection and a 4 KB env limit the backend's
    // env (~4.5 KB) exceeds, so the handler rebuilds the task def's environment at
    // cold start, before core.config is imported: the plain env from this
    // parameter (Advanced tier: up to 8 KB) and each secret from the map below.
    // Both are derived from the task def's own maps.
    // Lambda reserves AWS_REGION and sets it itself (to the same region).
    const { AWS_REGION: _reservedRegion, ...provisionerEnvironment } = backendContainerEnvironment;
    const provisionerEnvParameter = new ssm.StringParameter(this, "ProvisionerEnvironment", {
      parameterName: `/lightbulb/${env}/provisioner-env`,
      stringValue: this.toJsonString(provisionerEnvironment),
      tier: ssm.ParameterTier.ADVANCED,
    });
    provisionerEnvParameter.grantRead(this.taskRole);
    const secretEnvMap = this.toJsonString(
      Object.fromEntries(Object.entries(backendContainerSecrets).map(([name, secret]) => [name, secret.arn])),
    );
    // The ECS provisioner ran as the backend task role; this keeps that. A Lambda
    // execution role also needs ENI + log writes, which this managed policy adds.
    this.taskRole.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        principals: [new iam.ServicePrincipal("lambda.amazonaws.com")],
      }),
    );
    this.taskRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaVPCAccessExecutionRole"),
    );
    const provisioner = new lambda.DockerImageFunction(this, "Provisioner", {
      functionName: `${resourcePrefix(env)}provisioner`,
      code: lambda.DockerImageCode.fromEcr(backendImage.repository, {
        tagOrDigest: backendImage.imageTag,
        // The image's venv python directly: `uv run` would write a cache to the
        // read-only Lambda filesystem.
        entrypoint: ["/app/.venv/bin/python", "-m", "awslambdaric"],
        cmd: ["scripts.provision_lambda.handler"],
        workingDirectory: "/app",
      }),
      architecture: lambda.Architecture.X86_64,
      role: this.taskRole,
      // 2048 MB matches the ECS task's memory, and Lambda CPU scales with memory
      // (~1.2 vCPU here) -- the cold start imports the whole backend.
      memorySize: 2048,
      // A provision is ~1 min of AWS/Paperclip calls; 5 min is the ceiling, and
      // waits belong in the state machine, not the function.
      timeout: cdk.Duration.minutes(5),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [serviceSg],
      logGroup,
      environment: {
        // Only /tmp is writable on Lambda.
        HOME: "/tmp",
        LAMBDA_ENV_PARAMETER: provisionerEnvParameter.parameterName,
        LAMBDA_SECRET_ENV_MAP: secretEnvMap,
      },
    });
    // The provisioner Lambda reads the task def's secrets through SecretsAccess
    // (lightbulb/{env}/*) on the task role, so no per-secret statement is needed.

    // ---------------------------------------------------------------------
    // Messaging work consumer: openclaw-work.fifo drained by an SQS event source
    // into scripts.work_lambda -- the provisioner's image, env, secrets, role and
    // SG. It replaced an in-process long-poll loop that awaited a whole 10-message
    // receive at once, so one cold AgentCore wake (up to 90 s) held every other
    // workspace's work behind it. Lambda scales per FIFO message group instead.
    // ---------------------------------------------------------------------
    const workConsumer = new lambda.DockerImageFunction(this, "WorkConsumer", {
      functionName: `${resourcePrefix(env)}openclaw-work-consumer`,
      code: lambda.DockerImageCode.fromEcr(backendImage.repository, {
        tagOrDigest: backendImage.imageTag,
        entrypoint: ["/app/.venv/bin/python", "-m", "awslambdaric"],
        cmd: ["scripts.work_lambda.handler"],
        workingDirectory: "/app",
      }),
      architecture: lambda.Architecture.X86_64,
      role: this.taskRole,
      // Same as the provisioner: CPU scales with memory and a cold start imports
      // the whole backend. The work itself is I/O-bound.
      memorySize: 2048,
      // Keep the queue's 20 allowed invocations available even when unrelated
      // Lambdas consume the account's unreserved concurrency.
      reservedConcurrentExecutions: 20,
      // One message's worst case: Jev decide + loser wait (~19 s) + a steer (10 s) +
      // the 90 s AgentCore ingress budget + the first invoke's env rebuild. The
      // queue's visibility timeout (api-stack) must stay >= this -- Lambda refuses
      // the mapping otherwise.
      timeout: cdk.Duration.seconds(180),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [serviceSg],
      logGroup,
      environment: {
        HOME: "/tmp",
        LAMBDA_ENV_PARAMETER: provisionerEnvParameter.parameterName,
        LAMBDA_SECRET_ENV_MAP: secretEnvMap,
      },
    });
    // Dev keeps one consumer warm (Walk 14, 6 Nov 2026): every "queue wait" over 3 s on dev was a cold
    // start, 6.3-10.2 s of the handler's first-invoke setup (env rebuild, backend import, Temporal
    // connect), while a warm one waits ~35 ms. FIFO queues have no batching window to tune. A
    // provisioned environment runs that setup at init (scripts/work_lambda.py), where Lambda allows it
    // well past 10 s. ponytail: one warm instance; a second concurrent room still cold-starts.
    const workTarget: lambda.IFunction =
      env === "dev"
        ? new lambda.Alias(this, "WorkConsumerLive", {
            aliasName: "live",
            version: workConsumer.currentVersion,
            provisionedConcurrentExecutions: 1,
          })
        : workConsumer;
    const workMapping = workTarget.addEventSourceMapping("OpenClawWorkQueue", {
      eventSourceArn: props.openClawWorkQueueArn,
      // One message per invoke: a FIFO batch holds one group, run in order, so a
      // bigger batch would multiply the worst case past the timeout and keep the
      // group's later messages in flight behind a retry. Items are sparse (one per
      // agent per message) and room_dispatch coalesces through throughSeq.
      batchSize: 1,
      reportBatchItemFailures: true,
      // Caps DynamoDB/AgentCore fan-out from a burst across many rooms.
      maxConcurrency: 20,
    });
    // The mapping is validated against the role's SQS grants at create time.
    workMapping.node.addDependency(messagingPolicy);

    const runProvisioner = new tasks.LambdaInvoke(this, "RunProvisioner", {
      lambdaFunction: provisioner,
      // No payload: the Lambda gets the state input, i.e. the execution input.
      payloadResponseOnly: true,
      resultPath: sfn.JsonPath.DISCARD,
      retryOnServiceExceptions: false,
      taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(6)),
    });
    // The runner's typed transient error and Lambda's own service errors.
    const transientErrors = [
      "ProvisionTransient",
      "Lambda.ServiceException",
      "Lambda.AWSLambdaException",
      "Lambda.SdkClientException",
      "Lambda.TooManyRequestsException",
    ];
    runProvisioner.addRetry({
      errors: transientErrors,
      maxAttempts: 3,
      interval: cdk.Duration.seconds(10),
      backoffRate: 2,
    });

    const succeedProvisioned = new sfn.Succeed(this, "Provisioned");
    const succeedSuperseded = new sfn.Succeed(this, "Superseded");
    const failProvision = new sfn.Fail(this, "ProvisionFailed", {
      error: "ProvisionFailed",
      cause: "provisioner exhausted retries or failed deterministically",
    });

    // Status-conditioned row writes — the crash backstop. On a clean runner
    // failure the runner already wrote these values; these writes are idempotent
    // re-assertions that also cover a runner that died mid-write (timeout, OOM).
    // Condition accepts BOTH "provisioning" (runner died before its own
    // error write) AND "error" (runner wrote it, this re-asserts). Rows at "running"/"stopped" (a concurrent execution
    // already finished, or someone stopped the box) or gone are never
    // touched (routes to Superseded via the catch below). No claim: a RUNNING
    // execution owns the provision, and every provision step is idempotent.
    const ddbWrite = (id: string, updateExpr: string, values: Record<string, tasks.DynamoAttributeValue>) => {
      const write = new tasks.DynamoUpdateItem(this, id, {
        table: containersTableRef,
        key: {
          owner_id: tasks.DynamoAttributeValue.fromString(sfn.JsonPath.stringAt("$.owner_id")),
        },
        updateExpression: updateExpr,
        conditionExpression: "#s = :prov OR #s = :err",
        expressionAttributeNames: { "#s": "status" },
        expressionAttributeValues: {
          ...values,
          ":prov": tasks.DynamoAttributeValue.fromString("provisioning"),
          ":ts": tasks.DynamoAttributeValue.fromString(sfn.JsonPath.stringAt("$$.State.EnteredTime")),
        },
        resultPath: sfn.JsonPath.DISCARD,
      });
      // These writes are the crash backstop — without a Retry policy a
      // single DDB throttle fails the whole execution and the row wedges at
      // "provisioning". Retriers evaluate IN ORDER, so the condition miss
      // (ConditionalCheckFailed = the row moved on) is excluded FIRST
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

    // The runner already wrote last_error for its own failures; only a crash,
    // timeout or unknown error needs the machine to write it.
    const markFailed = (id: string, retryable: boolean, lastError?: string) =>
      ddbWrite(
        id,
        `SET #s = :err, substatus = :sub, retryable = :r, updated_at = :ts${lastError ? ", last_error = :le" : ""}`,
        {
          ":err": tasks.DynamoAttributeValue.fromString("error"),
          ":sub": tasks.DynamoAttributeValue.fromString("provision_failed"),
          ":r": tasks.DynamoAttributeValue.fromBoolean(retryable),
          ...(lastError ? { ":le": tasks.DynamoAttributeValue.fromString(lastError) } : {}),
        },
      );
    const markFailedExhausted = markFailed("MarkFailedExhausted", true);
    const markFailedDeterministic = markFailed("MarkFailedDeterministic", false);
    const markFailedUnhandled = markFailed(
      "MarkFailedUnhandled",
      false,
      sfn.JsonPath.format("provisioner {}: {}", sfn.JsonPath.stringAt("$.error.Error"), sfn.JsonPath.stringAt("$.error.Cause")),
    );
    // ConditionalCheckFailed on any write means the row moved on (running,
    // stopped or gone) — stop quietly.
    // (Optimized-integration exception prefix is "DynamoDB", uppercase DB.)
    for (const write of [markFailedExhausted, markFailedDeterministic, markFailedUnhandled]) {
      write.addCatch(succeedSuperseded, {
        errors: ["DynamoDB.ConditionalCheckFailedException"],
        resultPath: sfn.JsonPath.DISCARD,
      });
      write.next(failProvision);
    }

    // A reconcile execution ({mode: "reconcile"}, dispatch_reconcile) keeps the retries above
    // but never reaches the row writes: their condition admits a PROVISIONING row, and a
    // reconcile can run while an unrelated provision execution owns it. It ends here instead;
    // reconcile_runtime already paged (provision.reconcile.failed) and the running workspace is
    // untouched. A Succeed, not a Fail: the executions-failed page below means "a signup got
    // no container", which a failed reconcile is not.
    const reconcileGaveUp = new sfn.Succeed(this, "ReconcileGaveUp");
    const unlessReconcile = (id: string, target: sfn.IChainable) =>
      new sfn.Choice(this, id)
        .when(sfn.Condition.and(sfn.Condition.isPresent("$.mode"), sfn.Condition.stringEquals("$.mode", "reconcile")), reconcileGaveUp)
        .otherwise(target);
    runProvisioner.addCatch(unlessReconcile("RouteExhausted", markFailedExhausted), { errors: transientErrors, resultPath: "$.error" });
    runProvisioner.addCatch(unlessReconcile("RouteDeterministic", markFailedDeterministic), { errors: ["ProvisionFailed"], resultPath: "$.error" });
    runProvisioner.addCatch(unlessReconcile("RouteUnhandled", markFailedUnhandled), { errors: ["States.ALL"], resultPath: "$.error" });

    runProvisioner.next(succeedProvisioned);

    const provisionStateMachine = new sfn.StateMachine(this, "ProvisionStateMachine", {
      stateMachineName: `${resourcePrefix(env)}provision`,
      role: new iam.Role(this, "ProvisionStateMachineRole", {
        roleName: `${resourcePrefix(env)}provision-state-machine`,
        assumedBy: new iam.ServicePrincipal("states.amazonaws.com"),
      }),
      // The provisioner runs at once: the backend no longer runs an inline
      // attempt, so there is no row to watch first. (The watch loop that used
      // to sit here keyed on the provision claim, which is gone.)
      definitionBody: sfn.DefinitionBody.fromChainable(runProvisioner),
      // 3 x 6-min attempts + backoff, with room to spare.
      timeout: cdk.Duration.minutes(30),
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

    // Its own policy, not the task role's default one: the provisioner Lambda
    // runs as the task role and depends on that default policy, while this
    // statement references the machine that invokes the Lambda -- a cycle.
    new iam.ManagedPolicy(this, "ProvisionDispatchPolicy", {
      roles: [this.taskRole],
      statements: [
        new iam.PolicyStatement({
          sid: "ProvisionStateMachineDispatch",
          // ListExecutions (RUNNING, by name prefix) is dispatch's whole
          // single-flight check — provision_dispatch.py.
          actions: ["states:StartExecution", "states:ListExecutions"],
          resources: [provisionStateMachine.stateMachineArn],
        }),
      ],
    });

    // Status changes -> the owner's open clients. The machine's own row writes
    // (MarkFailed*) and the provisioner Lambda's never reach the backend's
    // write-site publisher, so EventBridge carries every execution status change
    // to an internal route that publishes the runtime.resource.invalidated nudge
    // (routers/container.py provision_execution_event). A nudge, never state:
    // a lost event costs one refetch, and the poll still backstops until Phase 3
    // retires it. The connection sends the WS internal shared secret as a header;
    // rotating that secret needs a redeploy of this stack.
    const statusEventsConnection = new events.Connection(this, "ProvisionStatusEventsConnection", {
      authorization: events.Authorization.apiKey(
        "x-lightbulb-ws-internal",
        cdk.SecretValue.secretsManager(props.secretNames.wsInternalSecret),
      ),
    });
    const statusEventsDestination = new events.ApiDestination(this, "ProvisionStatusEventsDestination", {
      connection: statusEventsConnection,
      endpoint: `${
        env === "prod" ? `https://api.${rootDomain}` : `https://api-${env}.${rootDomain}`
      }/api/v1/internal/provisioning/execution-events`,
      httpMethod: events.HttpMethod.POST,
    });
    new events.Rule(this, "ProvisionStatusEventsRule", {
      eventPattern: {
        source: ["aws.states"],
        detailType: ["Step Functions Execution Status Change"],
        detail: { stateMachineArn: [provisionStateMachine.stateMachineArn] },
      },
      targets: [new events_targets.ApiDestination(statusEventsDestination)],
    });

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
      alarmName: `${resourcePrefix(env)}provision-executions-failed`,
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
      alarmName: `${resourcePrefix(env)}provision-executions-timedout`,
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
      alarmName: `${resourcePrefix(env)}provision-executions-slow`,
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
      // Sized from 30-day CloudWatch (2026-10-27 cost review): prod peaked at 0.52 GB of 4 GB
      // and 0.9 vCPU hourly-average of 2; dev peaked at 1 GB once (p99 0.35 GB). 2 GB is 4x
      // prod's and 2x dev's high-water; CPU bursts are what autoscaling (2-4 / 1-4) is for.
      cpu: env === "prod" ? 1024 : 512,
      memoryLimitMiB: 2048,
      taskRole: this.taskRole,
      executionRole: taskExecutionRole,
    });

    // Container definition
    taskDef.addContainer("backend", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      // Runs as the image's own non-root USER (appuser). Root was only ever needed to
      // write the shared EFS mount, which is gone.
      portMappings: [{ containerPort: 8000 }],
      environment: {
        ...backendContainerEnvironment,
        // THE provisioning executor, active on merge — no flag (spec §7 /
        // onboarding-architecture-design.md PR-10). The state machine below
        // is dispatched by every dispatch_provision call via
        // core.services.provision_dispatch.
        PROVISION_STATE_MACHINE_ARN: provisionStateMachine.stateMachineArn,
      },
      secrets: {
        ...backendContainerSecrets,
        HOOK_GRANT_SIGNING_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportHookGrantSigningKey", `lightbulb/${env}/hook_grant_signing_key`),
        ),
      },
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

    // -------------------------------------------------------------------------
    // Fargate Service
    // -------------------------------------------------------------------------
    this.service = new ecs.FargateService(this, "Service", {
      serviceName: `lightbulb-${env}-backend`,
      cluster: props.container.cluster,
      taskDefinition: taskDef,
      // Dev runs one task on Spot (no HA to keep; a Spot reclaim is a brief dev gap). With
      // one task, min 100 / max 200 still starts the new task before stopping the old one.
      desiredCount: env === "prod" ? 2 : 1,
      ...(env === "prod" ? {} : { capacityProviderStrategies: DEV_SPOT }),
      // Rolling deploy as a clean handoff: keeping 100% healthy forces ECS to
      // bring new tasks to healthy BEFORE stopping old ones, and 200% gives
      // the headroom to run both generations at once. So a deploy has no
      // outage window. Circuit breaker still rolls back a bad rollout.
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      circuitBreaker: { rollback: true },
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [serviceSg],
      assignPublicIp: false,
      enableExecuteCommand: true,
    });
    // The first prod promotion may still have pre-#3454 pollers (60 s visibility).
    // Let ECS finish replacing them before Lambda starts consuming. SQS holds
    // work during the brief handoff; rollback deletes the mapping first.
    workMapping.node.addDependency(this.service.node.defaultChild as ecs.CfnService);

    // Register with ALB target group
    this.service.attachToApplicationTargetGroup(props.targetGroup);

    // -------------------------------------------------------------------------
    // Temporal worker (super-agent, dev + prod; contracts-v2 §16, wave-1 plan T4)
    // -------------------------------------------------------------------------
    // The backend image run as `scripts.temporal_worker` on task queue `lightbulb`. No port and no
    // load balancer: it only dials out to Temporal Cloud (serviceSg allows all egress). SIGTERM ->
    // worker.shutdown() with a 20 s graceful window, inside the 30 s stopTimeout.
    if (superAgentEnabled(env)) {
      const workerTaskDef = new ecs.FargateTaskDefinition(this, "TemporalWorkerTaskDef", {
        family: `lightbulb-${env}-temporal-worker`,
        cpu: 512,
        memoryLimitMiB: 1024,
        taskRole: this.taskRole,
        executionRole: taskExecutionRole,
      });
      workerTaskDef.addContainer("temporal-worker", {
        image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
        // The venv python directly, as the Lambdas do: no `uv run` sync at start.
        command: ["/app/.venv/bin/python", "-m", "scripts.temporal_worker"],
        environment: backendContainerEnvironment,
        secrets: backendContainerSecrets,
        logging: ecs.LogDrivers.awsLogs({ streamPrefix: "temporal-worker", logGroup }),
        stopTimeout: cdk.Duration.seconds(30),
      });
      new ecs.FargateService(this, "TemporalWorkerService", {
        serviceName: `lightbulb-${env}-temporal-worker`,
        cluster: props.container.cluster,
        taskDefinition: workerTaskDef,
        desiredCount: 1,
        ...(env === "prod" ? {} : { capacityProviderStrategies: DEV_SPOT }),
        circuitBreaker: { rollback: true },
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [serviceSg],
        assignPublicIp: false,
        enableExecuteCommand: true,
      });

      // Watchdog (spec 11, wave-2 plan W7): the same image on its own task queue and service, so the
      // watchdog keeps ticking when the main worker pool is stuck. The `#watchdog` ids are Lightbulb's own
      // dev workspace and room (created by hand); empty means the digest logs instead of posting. Inside
      // superAgentEnabled(env).
      const watchdogTaskDef = new ecs.FargateTaskDefinition(this, "TemporalWatchdogTaskDef", {
        family: `lightbulb-${env}-temporal-watchdog`,
        cpu: 256,
        memoryLimitMiB: 512,
        taskRole: this.taskRole,
        executionRole: taskExecutionRole,
      });
      watchdogTaskDef.addContainer("temporal-watchdog", {
        image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
        command: ["/app/.venv/bin/python", "-m", "scripts.temporal_worker", "--queue", "lightbulb-watchdog"],
        environment: {
          ...backendContainerEnvironment,
          TEMPORAL_WATCHDOG_TASK_QUEUE: "lightbulb-watchdog",
          WATCHDOG_OWNER_ID: env === "dev" ? "org_3LgB9twuJbYCjGILfOL5JjgQV0p" : "", // Super Agent Test (dev); prod logs until a prod #watchdog room exists
          WATCHDOG_ROOM_ID: env === "dev" ? "b6619138-2379-5b3c-af96-f28b791d7d8f" : "", // its #watchdog room
        },
        secrets: backendContainerSecrets,
        logging: ecs.LogDrivers.awsLogs({ streamPrefix: "temporal-watchdog", logGroup }),
        stopTimeout: cdk.Duration.seconds(30),
      });
      new ecs.FargateService(this, "TemporalWatchdogService", {
        serviceName: `lightbulb-${env}-temporal-watchdog`,
        cluster: props.container.cluster,
        taskDefinition: watchdogTaskDef,
        desiredCount: 1,
        ...(env === "prod" ? {} : { capacityProviderStrategies: DEV_SPOT }),
        circuitBreaker: { rollback: true },
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [serviceSg],
        assignPublicIp: false,
        enableExecuteCommand: true,
      });
    }

    // Horizontal autoscaling: the machine relay (sub-project D) moved every
    // remaining piece of per-connection state that made a second instance
    // unsafe (node/operator sockets, RPC waiters) out of process and onto
    // DynamoDB (TTL presence rows, claim leases) — see CLAUDE.md's
    // WebSocket Architecture section. 2-4 tasks, scaling on CPU.
    this.service
      .autoScaleTaskCount({ minCapacity: env === "prod" ? 2 : 1, maxCapacity: 4 })
      .scaleOnCpuUtilization("Cpu", { targetUtilizationPercent: 60 });

    // -------------------------------------------------------------------------
    // Tags
    // -------------------------------------------------------------------------
    cdk.Tags.of(this.service).add("Name", `lightbulb-${env}-backend`);
    cdk.Tags.of(this.service).add("Project", "lightbulb");
    cdk.Tags.of(this.service).add("Environment", env);
  }
}
