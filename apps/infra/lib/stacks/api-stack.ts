import * as path from "path";
import * as fs from "fs";
import { execSync } from "child_process";
import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import { WebSocketApi, WebSocketStage } from "aws-cdk-lib/aws-apigatewayv2";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { WebSocketLambdaAuthorizer } from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import * as appsync from "aws-cdk-lib/aws-appsync";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as events from "aws-cdk-lib/aws-events";
import * as eventTargets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as logs from "aws-cdk-lib/aws-logs";
import * as aoss from "aws-cdk-lib/aws-opensearchserverless";
import * as pipes from "aws-cdk-lib/aws-pipes";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as sns from "aws-cdk-lib/aws-sns";
import * as sqs from "aws-cdk-lib/aws-sqs";
import {
  alertTopicArn,
  logGroupPath,
  resourcePrefix,
  tablePrefix,
} from "../table-names";
import { Construct } from "constructs";
import { LAUNCH_DOMAIN, ROOT_DOMAIN } from "../domains";
import { superAgentEnabled } from "../super-agent";

// =============================================================================
// ApiStack — HTTP API Gateway + WebSocket API Gateway
// =============================================================================
// HTTP API:  Vercel → API Gateway v2 (HTTP) → VPC Link v2 → ALB → Fargate
// WebSocket: Paired nodes / Paperclip operators → API Gateway v2 → Lambda + ALB
// =============================================================================

export interface ApiStackProps extends cdk.StackProps {
  environment: string;
  /** Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN. */
  rootDomain?: string;
  vpc: ec2.IVpc;
  certificate?: acm.ICertificate;
  hostedZone?: route53.IHostedZone;
  alb: elbv2.IApplicationLoadBalancer;
  albHttpListenerArn: string;
  /**
   * TRANSITIONAL (ALB rename step 1): the named ALB. A CloudFront VPC origin
   * for it is created here one deploy BEFORE the distribution references it,
   * because a freshly created VpcOrigin can still be "Deploying" when a
   * distribution update points at it (the race that broke the prod api
   * stack's first create).
   */
  albV2: elbv2.IApplicationLoadBalancer;
  albSecurityGroup: ec2.ISecurityGroup;
  /**
   * Secrets Manager name containing this environment's Clerk issuer URL. AppSync
   * resolves it as a CloudFormation dynamic reference for OIDC configuration;
   * the value is never synthesized or output.
   */
  clerkIssuerSecretName: string;
  /** Literal issuer URL. When set it wins over the secret (see lightbulb-stage.ts). */
  clerkIssuerUrl?: string;
  /**
   * Secrets Manager name (NOT ISecret) of the Paperclip service-token signing
   * key. The WebSocket Lambda Authorizer fetches this at cold-start and uses
   * it to verify HS256 service-token JWTs minted by the FastAPI backend
   * (core/services/service_token.py). Passed as a plain string to avoid
   * cross-stack KMS auto-grant cycles — see service-stack.ts comments.
   */
  paperclipServiceTokenKeySecretName: string;
  /**
   * AuthStack KMS key ARN (NOT a Key object) used to encrypt the Paperclip
   * service-token secret. The authorizer Lambda needs `kms:Decrypt` on this
   * key in order for `secretsmanager:GetSecretValue` to succeed at cold-start
   * — `secret.grantRead()` only adds the SecretsManager action, not the
   * underlying KMS one. Passed as a string for the same cross-stack reason
   * as `paperclipServiceTokenKeySecretName`.
   */
  paperclipKmsKeyArn: string;
  /**
   * Secrets Manager name (NOT ISecret) of the WS internal shared secret. The
   * three WebSocket Lambdas fetch it at cold-start and attach it as the
   * `x-lightbulb-ws-internal` header so the backend `/ws/*` endpoints can tell them
   * apart from forged callers arriving via the public HTTP API Gateway. Passed
   * as a plain string for the same cross-stack reason as the service-token key;
   * encrypted under the same CMK (`paperclipKmsKeyArn`).
   */
  wsInternalSecretSecretName: string;
  /**
   * Containers DynamoDB table. The WebSocket Lambda Authorizer reads
   * `paired_devices[deviceId].publicKey` off the owner row to verify desktop
   * device-key WS-upgrade tokens (EdDSA proof-of-possession). Read-only grant.
   */
  /**
   * Containers table NAME, not the construct. Taking the ITable creates a
   * cross-stack Export from the database stack, and CloudFormation cannot
   * change an exported value while anything imports it — the deadlock that
   * blocked the table rename. The authorizer only needs the name and a
   * by-name grant.
   */
  containersTableName: string;
}

const THROTTLE_CONFIG: Record<
  string,
  { burstLimit: number; rateLimit: number }
> = {
  dev: { burstLimit: 100, rateLimit: 50 },
  prod: { burstLimit: 500, rateLimit: 200 },
};

// Slack posts every workspace's events to ONE URL. Give that route its own
// bucket so a chatty tenant (or Slack's 3× retries) never eats the stage-wide
// budget every browser/desktop/mobile REST call shares — and vice versa.
const SLACK_ROUTE_KEY = "POST /api/v1/slack/events";
const SLACK_THROTTLE: Record<string, { burstLimit: number; rateLimit: number }> = {
  dev: { burstLimit: 50, rateLimit: 25 },
  prod: { burstLimit: 200, rateLimit: 100 },
};

// Session links open with no sign-in (spec 2026-10-05-public-session-links §6): their own bucket,
// so an unauthenticated flood never eats the stage-wide budget, and the reverse.
const PUBLIC_ROUTE_KEY = "ANY /api/v1/public/{proxy+}";
const PUBLIC_THROTTLE: Record<string, { burstLimit: number; rateLimit: number }> = {
  dev: { burstLimit: 20, rateLimit: 10 },
  prod: { burstLimit: 100, rateLimit: 50 },
};

// One namespace, workspace-scoped paths, tenancy decided here (spec §3). Segments are
// UUID-only; every decision strong-reads the base table. IAM admits publishers to the
// namespace; this handler narrows a per-owner container task role to its own rows.
export const wsHandlerCode = (tableName: string, env: string) => `
import { util } from '@aws-appsync/utils';
const TABLE = '${tableName}';
const OWNER_ROLE_PREFIX = 'lightbulb-${env}-owner-bedrock-';
// String pattern via util.matches: APPSYNC_JS has no regex literals.
const UUID = '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$';
// /ws/{workspaceId}/rooms/{roomId}/{deliveryKey} or /ws/{workspaceId}/users/{userKey}
// /ws/{workspaceId}/frames/{roomId}/{deliveryKey} is a room sub-channel (Mac live view):
// AppSync Event API channel paths cap at 5 segments, so it swaps the 'rooms' segment for
// 'frames' rather than appending a suffix. Same room/deliveryKey authorization, no separate
// meta row — onSubscribe/onPublish don't need to know it exists.
function parsePath(ctx) {
  const p = ctx.info.channel.path.split('/');
  if (p.length < 4 || p[0] !== '' || p[1] !== 'ws' || !util.matches(UUID, p[2])) util.unauthorized();
  if (
    (p[3] === 'rooms' || p[3] === 'frames') &&
    p.length === 6 &&
    util.matches(UUID, p[4]) &&
    util.matches(UUID, p[5])
  ) {
    return { kind: 'rooms', workspaceId: p[2], roomId: p[4], deliveryKey: p[5] };
  }
  if (p[3] === 'users' && p.length === 5 && util.matches(UUID, p[4])) {
    return { kind: 'users', workspaceId: p[2], userKey: p[4] };
  }
  // /ws/{workspaceId}/sessions: one channel per workspace, no key. A subscriber must be a current
  // member (WORKSPACE#<ws>/MEMBER#<sub> active, deleted when they are removed); an open
  // subscription is dropped by the leaver's own client on workspace.access.removed.
  if (p[3] === 'sessions' && p.length === 4) {
    return { kind: 'sessions', workspaceId: p[2] };
  }
  // /ws/{workspaceId}/session-frames/{sessionId}: one shared terminal's screen frames, one publish
  // for every viewer. The same member check, plus the session must be shared now:
  // WORKSPACE#<ws>/SESSION#<sid> exists exactly while it is (local_sessions_repo.put_session).
  if (p[3] === 'session-frames' && p.length === 5 && util.matches(UUID, p[4])) {
    return { kind: 'sessions', workspaceId: p[2], sessionId: p[4] };
  }
${superAgentEnabled(env) ? `  // /ws/{workspaceId}/tasks: super-agent task.changed ids (dev only), the same member check as /sessions.
  if (p[3] === 'tasks' && p.length === 4) {
    return { kind: 'sessions', workspaceId: p[2] };
  }
` : ""}  util.unauthorized();
}
function metaKey(path) {
  if (path.kind === 'rooms') return { pk: 'ROOM#' + path.roomId, sk: 'META' };
  if (path.kind === 'sessions') return { pk: 'WORKSPACE#' + path.workspaceId, sk: 'META' };
  return { pk: 'STREAM#' + path.userKey, sk: 'META' };
}
function denyOnReadFailure(ctx) {
  if (ctx.error || !ctx.result) util.unauthorized();
}
function metaMatchesPath(meta, path) {
  if (!meta || meta.workspaceId !== path.workspaceId) return false;
  if (path.kind === 'rooms') return meta.deliveryKey === path.deliveryKey;
  return path.kind === 'sessions' || meta.active === true;
}
// arn:aws:sts::{account}:assumed-role/{roleName}/{session}; null when the caller is not
// an owner task role (backend service role, projector) — IAM already admitted it.
function callerOwnerId(ctx) {
  const arn = ctx.identity && ctx.identity.userArn;
  if (typeof arn !== 'string') util.unauthorized();
  const parts = arn.split('/');
  if (parts.length !== 3 || parts[0].indexOf(':assumed-role') === -1) util.unauthorized();
  if (parts[1].indexOf(OWNER_ROLE_PREFIX) === 0) return parts[1].substring(OWNER_ROLE_PREFIX.length);
  // Dev and prod share one AWS account: an owner role of the OTHER environment is a
  // stranger, not a trusted platform role. Backend/projector roles never carry the marker.
  if (parts[1].indexOf('-owner-bedrock-') !== -1) util.unauthorized();
  return null;
}
export const onSubscribe = {
  request(ctx) {
    const path = parsePath(ctx);
    const sub = ctx.identity && ctx.identity.sub;
    if (typeof sub !== 'string' || sub.length === 0) util.unauthorized();
    const keys = [util.dynamodb.toMapValues(metaKey(path))];
    if (path.kind === 'rooms') keys.push(util.dynamodb.toMapValues({ pk: 'ROOM#' + path.roomId, sk: 'MEMBER#human#' + sub }));
    if (path.kind === 'sessions') keys.push(util.dynamodb.toMapValues({ pk: 'WORKSPACE#' + path.workspaceId, sk: 'MEMBER#' + sub }));
    if (path.sessionId) keys.push(util.dynamodb.toMapValues({ pk: 'WORKSPACE#' + path.workspaceId, sk: 'SESSION#' + path.sessionId }));
    return { operation: 'BatchGetItem', tables: { [TABLE]: { keys, consistentRead: true } } };
  },
  response(ctx) {
    denyOnReadFailure(ctx);
    const path = parsePath(ctx);
    const rows = ctx.result.data && ctx.result.data[TABLE];
    const pending = ctx.result.unprocessedKeys && ctx.result.unprocessedKeys[TABLE];
    if (!Array.isArray(rows) || (Array.isArray(pending) && pending.length > 0)) util.unauthorized();
    const meta = rows[0];
    if (!metaMatchesPath(meta, path)) util.unauthorized();
    if (path.kind === 'rooms' && !rows[1]) util.unauthorized();
    if (path.kind === 'sessions' && (!rows[1] || rows[1].active !== true)) util.unauthorized();
    if (path.sessionId && !rows[2]) util.unauthorized();
    if (path.kind === 'users' && meta.userId !== ctx.identity.sub) util.unauthorized();
  },
};
export const onPublish = {
  request(ctx) {
    return { operation: 'GetItem', key: util.dynamodb.toMapValues(metaKey(parsePath(ctx))), consistentRead: true };
  },
  response(ctx) {
    denyOnReadFailure(ctx);
    const path = parsePath(ctx);
    if (!metaMatchesPath(ctx.result, path)) util.unauthorized();
    const owner = callerOwnerId(ctx);
    if (owner !== null && ctx.result.ownerId !== owner) util.unauthorized();
    return ctx.events;
  },
};
`;

export class ApiStack extends cdk.Stack {
  public readonly httpApiUrl: string;
  public readonly webSocketUrl: string;
  public readonly managementApiUrl: string;
  public readonly connectionsTableName: string;
  public readonly messagingTableName: string;
  public readonly openClawWorkQueueUrl: string;
  public readonly openClawWorkQueueArn: string;
  public readonly appsyncEventsHttpEndpoint: string;
  public readonly appsyncEventsRealtimeEndpoint: string;
  public readonly appsyncEventsApiArn: string;
  public readonly appsyncEventsRegion: string;
  /**
   * Messaging-plane names the ORR dashboard plots.
   *
   * The three below are assigned from the SAME literal template the construct
   * is named with, never `queue.queueName` / `fn.functionName`. A construct
   * reference is a token, and a token consumed by another stack becomes a
   * CloudFormation Export — which CloudFormation then refuses to change while
   * anything imports it. That is the deadlock the `containersTableName` note
   * above describes, and it is why the house pattern is names-not-constructs.
   */
  public readonly openClawWorkQueueName: string;
  public readonly openClawWorkDlqName: string;
  public readonly messagingProjectorFunctionName: string;
  /**
   * The AppSync Event API id — the ONE deliberate exception, and the one
   * cross-stack Export this stack adds.
   *
   * Unlike a queue or a function, an Event API id is assigned by AppSync at
   * create time: there is no literal form to write down, so the dashboard
   * widget that plots AWS/AppSync metrics can only get it by import. The cost
   * is that CloudFormation will refuse to REPLACE the Event API while the
   * observability stack imports this value — replacing it means first
   * deploying an observability stack without the AppSync widgets, then
   * replacing the API, then putting the widgets back.
   */
  public readonly appsyncEventApiId: string;
  public readonly wsApiId: string;
  public readonly wsStage: string;
  /** Machine relay (desktop node hosts + Paperclip operators) WS endpoint on
   * the bv- door: wss://{browserViewDomain}/api/v1/gateway/ws. Falls back to a
   * fixed dev URL on the no-cert/no-zone synth path (unit tests, unknown envs)
   * so this is never `undefined` in service-stack's ECS env map. */
  public readonly gatewayRelayUrl: string;
  /** Message search (OpenSearch Serverless). Empty strings while search is off for this env. */
  public readonly messageSearchEndpoint: string;
  public readonly messageSearchCollectionArn: string;
  public readonly messageSearchQueueUrl: string;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    const env = props.environment;
    const isProd = env === "prod";
    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    const throttle = THROTTLE_CONFIG[env] ?? THROTTLE_CONFIG.dev;

    // =========================================================================
    // HTTP API Gateway
    // =========================================================================

    const httpApiDomain = isProd ? `api.${rootDomain}` : `api-${env}.${rootDomain}`;
    // Paperclip ("Teams") used to have its own API Gateway custom-domain
    // mapping here (companyApiDomain → CompanyHttpDomain). It now lives
    // on Vercel — the frontend project has a host-conditional rewrite
    // that proxies {env}.company.isol8.co through to /__paperclip_proxy__
    // on this same API Gateway. No second custom domain, no per-host
    // dispatch in the backend; the proxy is reached at its normal path.
    const frontendUrl = isProd
      ? `https://${rootDomain}`
      : `https://${env}.${rootDomain}`;

    // --- HTTP API ---
    const httpApi = new apigatewayv2.CfnApi(this, "HttpApi", {
      name: `lightbulb-${env}-api`,
      protocolType: "HTTP",
      // AWS API Gateway HTTP API handles CORS preflight AT THE GATEWAY LAYER —
      // the backend never sees OPTIONS requests for declared CORS headers.
      // This list MUST match what FastAPI's CORSMiddleware allows, otherwise
      // requests get silently rejected with "preflight failed" before they
      // reach the backend. PATCH was missing here, which broke PATCH
      // /api/v1/config (used by the channels bot-setup wizard) with a
      // confusing "No 'Access-Control-Allow-Origin' header" browser error —
      // FastAPI's middleware was correct, but AWS API Gateway returned its
      // own preflight response with PATCH missing from allow_methods.
      corsConfiguration: {
        // The launch-competition site (LAUNCH_DOMAIN) reads the public /launch
        // endpoints cross-origin; it never holds a Clerk session.
        allowOrigins: isProd
          ? [frontendUrl, `https://www.${rootDomain}`, `https://${LAUNCH_DOMAIN}`, `https://www.${LAUNCH_DOMAIN}`]
          : [frontendUrl, `https://dev.${LAUNCH_DOMAIN}`],
        allowMethods: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allowHeaders: [
          "Content-Type",
          "Authorization",
          "X-Requested-With",
        ],
        // Browser-readable response headers. The gateway's expose-headers is
        // authoritative — it replaces whatever FastAPI's CORSMiddleware sets, so
        // X-Request-ID must be listed HERE (not only in the backend) for
        // cross-origin JS to read it. The frontend attaches recent request IDs
        // to support tickets for log correlation.
        exposeHeaders: ["Content-Type", "X-Request-ID"],
        allowCredentials: true,
        maxAge: 86400,
      },
    });

    // --- VPC Link v2 (for HTTP API → ALB) ---
    const vpcLinkV2 = new apigatewayv2.CfnVpcLink(this, "HttpVpcLink", {
      name: `lightbulb-${env}-vpc-link`,
      securityGroupIds: [props.albSecurityGroup.securityGroupId],
      subnetIds: props.vpc.selectSubnets({
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      }).subnetIds,
    });

    // --- HTTP Integration → ALB via VPC Link ---
    // We set X-Isol8-Public-Host = $context.domainName so the FastAPI backend
    // can dispatch per public hostname (api.isol8.co vs company.isol8.co).
    // API Gateway rewrites the upstream Host to the ALB DNS, so the original
    // requesting host is otherwise lost.
    //
    // NOTE: We deliberately use a custom header name (`X-Isol8-Public-Host`)
    // rather than `X-Forwarded-Host` — API Gateway HTTP API blocks parameter
    // mapping on `x-forwarded-*` headers (they're managed by the gateway
    // itself; AWS rejects with "Operations on header x-forwarded-host are
    // restricted"). The backend's HostDispatcherMiddleware reads this custom
    // header. The same integration (and so the same X-Isol8-Public-Host)
    // serves all custom-domain mappings on this API.
    const httpIntegration = new apigatewayv2.CfnIntegration(
      this,
      "HttpAlbIntegration",
      {
        apiId: httpApi.ref,
        integrationType: "HTTP_PROXY",
        integrationUri: props.albHttpListenerArn,
        integrationMethod: "ANY",
        connectionType: "VPC_LINK",
        connectionId: vpcLinkV2.ref,
        payloadFormatVersion: "1.0",
        timeoutInMillis: 30000,
        requestParameters: {
          "overwrite:header.X-Isol8-Public-Host": "$context.domainName",
          // The WAF per-IP rate rule (network-stack.ts) keys on this. `overwrite`
          // replaces anything the client sent, unlike X-Forwarded-For, which
          // API Gateway only appends to.
          "overwrite:header.X-Lightbulb-Client-Ip": "$context.identity.sourceIp",
        },
      },
    );

    // --- Default route ---
    new apigatewayv2.CfnRoute(this, "HttpDefaultRoute", {
      apiId: httpApi.ref,
      routeKey: "$default",
      target: `integrations/${httpIntegration.ref}`,
    });

    // Explicit route so the stage can attach route-level throttling to it;
    // it forwards to the same ALB integration as $default.
    new apigatewayv2.CfnRoute(this, "HttpSlackEventsRoute", {
      apiId: httpApi.ref,
      routeKey: SLACK_ROUTE_KEY,
      target: `integrations/${httpIntegration.ref}`,
    });

    new apigatewayv2.CfnRoute(this, "HttpPublicRoute", {
      apiId: httpApi.ref,
      routeKey: PUBLIC_ROUTE_KEY,
      target: `integrations/${httpIntegration.ref}`,
    });

    // --- HTTP API Log Group ---
    const httpApiLogGroup = new logs.LogGroup(this, "HttpApiLogs", {
      logGroupName: `/aws/api-gateway/lightbulb-${env}-http`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // --- Stage ---
    const httpStage = new apigatewayv2.CfnStage(this, "HttpStage", {
      apiId: httpApi.ref,
      stageName: env,
      autoDeploy: true,
      defaultRouteSettings: {
        throttlingBurstLimit: throttle.burstLimit,
        throttlingRateLimit: throttle.rateLimit,
      },
      // NOTE: CfnStage.routeSettings is typed `any` in the L1 construct (it's
      // a free-form route-key -> settings map), so CDK's usual camelCase ->
      // PascalCase property translation does NOT run on it — the nested
      // keys must already be CloudFormation-cased.
      routeSettings: {
        [SLACK_ROUTE_KEY]: {
          ThrottlingBurstLimit: (SLACK_THROTTLE[env] ?? SLACK_THROTTLE.dev).burstLimit,
          ThrottlingRateLimit: (SLACK_THROTTLE[env] ?? SLACK_THROTTLE.dev).rateLimit,
        },
        [PUBLIC_ROUTE_KEY]: {
          ThrottlingBurstLimit: (PUBLIC_THROTTLE[env] ?? PUBLIC_THROTTLE.dev).burstLimit,
          ThrottlingRateLimit: (PUBLIC_THROTTLE[env] ?? PUBLIC_THROTTLE.dev).rateLimit,
        },
      },
      accessLogSettings: {
        destinationArn: httpApiLogGroup.logGroupArn,
        format: JSON.stringify({
          requestId: "$context.requestId",
          ip: "$context.identity.sourceIp",
          requestTime: "$context.requestTime",
          httpMethod: "$context.httpMethod",
          routeKey: "$context.routeKey",
          status: "$context.status",
          protocol: "$context.protocol",
          responseLength: "$context.responseLength",
          integrationError: "$context.integrationErrorMessage",
        }),
      },
    });

    // --- Custom domain for HTTP API (only when certificate + hosted zone provided) ---
    if (props.certificate && props.hostedZone) {
      const httpDomainName = new apigatewayv2.CfnDomainName(
        this,
        "HttpDomain",
        {
          domainName: httpApiDomain,
          domainNameConfigurations: [
            {
              certificateArn: props.certificate.certificateArn,
              endpointType: "REGIONAL",
              securityPolicy: "TLS_1_2",
            },
          ],
        },
      );

      const httpMapping = new apigatewayv2.CfnApiMapping(this, "HttpApiMapping", {
        apiId: httpApi.ref,
        domainName: httpDomainName.ref,
        stage: env,
      });
      httpMapping.addDependency(httpStage);

      // --- Route53 A record for HTTP API ---
      new route53.ARecord(this, "HttpApiDnsRecord", {
        zone: props.hostedZone,
        recordName: httpApiDomain,
        target: route53.RecordTarget.fromAlias({
          bind: () => ({
            dnsName: cdk.Fn.getAtt(httpDomainName.logicalId, "RegionalDomainName").toString(),
            hostedZoneId: cdk.Fn.getAtt(httpDomainName.logicalId, "RegionalHostedZoneId").toString(),
          }),
        }),
      });

      // The Paperclip ("Teams") public hostname is served via Vercel
      // rewrites, not a dedicated API Gateway custom domain. Vercel
      // routes
      //   {env}.company.isol8.co → api[-{env}].isol8.co/__paperclip_proxy__/...
      // so the request still arrives at this same API Gateway, just on
      // the api[-{env}].isol8.co host with the proxy prefix in the URL
      // path. The DNS A-record for the Paperclip hostname lives in
      // DnsStack and points at Vercel's anycast IP.
      this.httpApiUrl = `https://${httpApiDomain}`;
    } else {
      this.httpApiUrl = `https://${httpApi.ref}.execute-api.${this.region}.amazonaws.com/${env}`;
    }

    // =========================================================================
    // DynamoDB Connections Table
    // =========================================================================

    const connectionsTable = new dynamodb.Table(this, "ConnectionsTable", {
      tableName: `${tablePrefix(env)}ws-connections`,
      partitionKey: { name: "connectionId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: "ttl",
    });

    // GSI for owner-keyed WS event fan-out: given an ownerId (org_id for org
    // members, user_id for personal owners), return every active connection
    // for that owner so any backend instance can deliver to all of an owner's
    // tabs/devices. INCLUDE (not KEYS_ONLY): the gateway fan-out needs the
    // member userId for per-member demux and connectionType to filter to chat
    // connections (excluding node/operator transparent pipes).
    connectionsTable.addGlobalSecondaryIndex({
      indexName: "by-owner-id",
      partitionKey: { name: "ownerId", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["userId", "connectionType"],
    });

    // The NAME as a literal, not connectionsTable.tableName. A construct ref
    // exports `api:ExportsOutputRefConnectionsTable…`, which service and
    // observability import — and an imported export cannot have its value
    // changed, so it would block renaming ws-connections exactly the way the
    // database stack's exports blocked the other 22.
    this.connectionsTableName = `${tablePrefix(env)}ws-connections`;

    // =========================================================================
    // Canonical messaging store, live projection, and durable OpenClaw work
    // =========================================================================

    const messagingKey = kms.Key.fromKeyArn(
      this,
      "MessagingKey",
      props.paperclipKmsKeyArn,
    );
    const messagingTable = new dynamodb.Table(this, "MessagingTable", {
      tableName: `${tablePrefix(env)}messaging`,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: messagingKey,
      pointInTimeRecovery: true,
      stream: dynamodb.StreamViewType.NEW_IMAGE,
      timeToLiveAttribute: "expiresAt",
      removalPolicy: isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });
    messagingTable.addGlobalSecondaryIndex({
      indexName: "by-member",
      partitionKey: { name: "memberKey", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "memberSort", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    messagingTable.addGlobalSecondaryIndex({
      indexName: "by-run-deadline",
      // Sparse: only open run-ledger rows carry `sweep`; the sweeper queries
      // sweep = "open" AND deadlineAt < now. ponytail: one constant partition —
      // fine while open runs stay in the low thousands.
      partitionKey: { name: "sweep", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "deadlineAt", type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });
    this.messagingTableName = `${tablePrefix(env)}messaging`;

    // Super-agent tasks table (contracts-v2 §3; dev only until a founder-approved rollout). RETAIN in
    // every env (spec 10 C27). Both GSIs are created with the table because CloudFormation adds at most
    // one per later update; names and keys mirror core/tasks_schema.py GSIS (api-stack.test.ts reads
    // the exported tasks-schema.json). Stream feeds MessagingProjector's task.changed events; TTL `expiresAt`
    // is set only on RTURN# rows (spec 01 §3.10).
    let tasksTable: dynamodb.Table | undefined;
    if (superAgentEnabled(env)) {
      tasksTable = new dynamodb.Table(this, "TasksTable", {
        tableName: `${tablePrefix(env)}tasks`,
        partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
        encryptionKey: messagingKey,
        pointInTimeRecovery: true,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
        stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
        timeToLiveAttribute: "expiresAt",
      });
      tasksTable.addGlobalSecondaryIndex({
        indexName: "by-status",
        partitionKey: { name: "statusKey", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "updatedAt", type: dynamodb.AttributeType.STRING },
        projectionType: dynamodb.ProjectionType.ALL,
      });
      tasksTable.addGlobalSecondaryIndex({
        indexName: "ws-meta",
        partitionKey: { name: "metaKind", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "pk", type: dynamodb.AttributeType.STRING },
        projectionType: dynamodb.ProjectionType.ALL,
      });
    }

    const workDlq = new sqs.Queue(this, "OpenClawWorkDlq", {
      queueName: `${resourcePrefix(env)}openclaw-work-dlq.fifo`,
      fifo: true,
      contentBasedDeduplication: false,
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: messagingKey,
    });
    const workQueue = new sqs.Queue(this, "OpenClawWorkQueue", {
      queueName: `${resourcePrefix(env)}openclaw-work.fifo`,
      fifo: true,
      contentBasedDeduplication: false,
      retentionPeriod: cdk.Duration.days(7),
      // Must be >= the consumer Lambda's 180 s timeout (service-stack
      // `WorkConsumer`) or Lambda refuses the event source mapping; the margin
      // covers receive-to-invoke lag. AWS's "6x the timeout" is advice for
      // throttled consumers — maxConcurrency on the mapping covers that — and on
      // a FIFO this is also how long an unexpected error holds its group, so it
      // stays tight. WorkRetry re-times its message to 10 s itself.
      visibilityTimeout: cdk.Duration.seconds(240),
      receiveMessageWaitTime: cdk.Duration.seconds(20),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: messagingKey,
      // The plugin acks within milliseconds (async turn contract); a pointer
      // not acked before the visibility timeout is redelivered. ≈5 min at the consumer's 10 s retry
      // visibility — a parked container's cold start, not a plugin failure, is
      // what the budget must cover; ingress is effect-once so repeated POSTs
      // are idempotent.
      deadLetterQueue: { queue: workDlq, maxReceiveCount: 30 },
    });
    const pipeFailureQueue = new sqs.Queue(this, "OpenClawWorkPipeFailures", {
      queueName: `${resourcePrefix(env)}openclaw-work-pipe-failures`,
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: messagingKey,
    });
    this.openClawWorkQueueUrl = workQueue.queueUrl;
    this.openClawWorkQueueArn = workQueue.queueArn;
    // Literals, matching the queueName templates above exactly — see the
    // field declarations for why these are not `workQueue.queueName`.
    this.openClawWorkQueueName = `${resourcePrefix(env)}openclaw-work.fifo`;
    this.openClawWorkDlqName = `${resourcePrefix(env)}openclaw-work-dlq.fifo`;

    const projectorFailureBucket = new s3.Bucket(
      this,
      "MessagingProjectorFailures",
      {
        bucketName: `${resourcePrefix(env)}messaging-projector-fail-${this.account}-${this.region}`,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        encryption: s3.BucketEncryption.KMS,
        encryptionKey: messagingKey,
        enforceSSL: true,
        lifecycleRules: [{ expiration: cdk.Duration.days(30) }],
        removalPolicy: isProd
          ? cdk.RemovalPolicy.RETAIN
          : cdk.RemovalPolicy.DESTROY,
        autoDeleteObjects: !isProd,
      },
    );

    const clerkIssuerSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "ClerkIssuerForAppSync",
      props.clerkIssuerSecretName,
    );
    // Event APIs have no excludeVerboseContent knob (unlike GraphQL APIs) — CDK's
    // EventApi.logConfig silently drops that field because the underlying
    // CfnApi.EventLogConfigProperty only has cloudWatchLogsRoleArn/logLevel, so a
    // logged request's headers/identity ride along regardless of level. logLevel
    // ERROR keeps that to just the erroring requests (today's 68 unattributable
    // SubscribeClientErrors), and the two-week LogRetention below bounds how long
    // that exposure sits in CloudWatch — same irreducible-verbosity tradeoff the
    // Bedrock invocation log takes (see the Billing section of CLAUDE.md).
    // An "Anyone with the link" session link's live terminal (owner ruling 2026-10-08): the `pub`
    // namespace, subscribed with a 45 s lease the backend mints for a link holder
    // (session_links.viewing). The authorizer reads that one row and nothing else.
    const pubAuthorizerFn = new lambda.Function(this, "PubAuthorizer", {
      functionName: `${resourcePrefix(env)}pub-authorizer`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "..", "lambda", "pub-authorizer"), {
        exclude: ["test_*.py"],
      }),
      timeout: cdk.Duration.seconds(5),
      memorySize: 512,
      environment: { MESSAGING_TABLE: messagingTable.tableName },
      logGroup: new logs.LogGroup(this, "PubAuthorizerLogs", {
        logGroupName: logGroupPath(env, "pub-authorizer"),
        retention: logs.RetentionDays.TWO_WEEKS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    pubAuthorizerFn.addToRolePolicy(
      new iam.PolicyStatement({
        // A connect and a subscribe each consume the lease with one conditional write, so one
        // lease is one connection and one subscription. The authorizer reads nothing else.
        actions: ["dynamodb:UpdateItem"],
        resources: [messagingTable.tableArn],
        conditions: { "ForAllValues:StringLike": { "dynamodb:LeadingKeys": ["PUBWATCH#*"] } },
      }),
    );
    messagingKey.grantEncryptDecrypt(pubAuthorizerFn);
    const eventApi = new appsync.EventApi(this, "MessagingEventApi", {
      apiName: `${resourcePrefix(env)}messaging-events`,
      logConfig: {
        fieldLogLevel: appsync.AppSyncFieldLogLevel.ERROR,
        retention: logs.RetentionDays.TWO_WEEKS,
      },
      authorizationConfig: {
        authProviders: [
          {
            authorizationType: appsync.AppSyncAuthorizationType.OIDC,
            openIdConnectConfig: {
              oidcProvider: props.clerkIssuerUrl ?? clerkIssuerSecret.secretValue.unsafeUnwrap(),
              clientId: "^lightbulb-appsync$",
            },
          },
          { authorizationType: appsync.AppSyncAuthorizationType.IAM },
          {
            authorizationType: appsync.AppSyncAuthorizationType.LAMBDA,
            lambdaAuthorizerConfig: { handler: pubAuthorizerFn, resultsCacheTtl: cdk.Duration.seconds(0) },
          },
        ],
        connectionAuthModeTypes: [
          appsync.AppSyncAuthorizationType.OIDC,
          appsync.AppSyncAuthorizationType.LAMBDA,
        ],
        defaultPublishAuthModeTypes: [appsync.AppSyncAuthorizationType.IAM],
        defaultSubscribeAuthModeTypes: [appsync.AppSyncAuthorizationType.OIDC],
      },
    });
    const messagingDataSource = new appsync.AppSyncDynamoDbDataSource(
      this,
      "MessagingEventDataSource",
      {
        api: eventApi,
        table: messagingTable,
        readOnlyAccess: true,
      },
    );
    const wsNamespace = eventApi.addChannelNamespace("WsNamespace", {
      channelNamespaceName: "ws",
      code: appsync.Code.fromInline(wsHandlerCode(messagingTable.tableName, env)),
      publishHandlerConfig: { dataSource: messagingDataSource },
      subscribeHandlerConfig: { dataSource: messagingDataSource },
      authorizationConfig: {
        publishAuthModeTypes: [appsync.AppSyncAuthorizationType.IAM],
        subscribeAuthModeTypes: [appsync.AppSyncAuthorizationType.OIDC],
      },
    });

    // No handler: the Lambda authorizer already bound the subscribe to its lease's one channel.
    // The `ws` namespace above stays OIDC-only, so a lease subscribes to nothing in it.
    eventApi.addChannelNamespace("PubNamespace", {
      channelNamespaceName: "pub",
      authorizationConfig: {
        publishAuthModeTypes: [appsync.AppSyncAuthorizationType.IAM],
        subscribeAuthModeTypes: [appsync.AppSyncAuthorizationType.LAMBDA],
      },
    });

    this.appsyncEventsHttpEndpoint = `https://${eventApi.httpDns}/event`;
    this.appsyncEventsRealtimeEndpoint = `wss://${eventApi.realtimeDns}/event/realtime`;
    this.appsyncEventsApiArn = eventApi.apiArn;
    this.appsyncEventsRegion = this.region;
    this.appsyncEventApiId = eventApi.apiId;

    const projectorFn = new lambda.Function(this, "MessagingProjector", {
      functionName: `${resourcePrefix(env)}messaging-projector`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "messaging-projector"),
      ),
      timeout: cdk.Duration.seconds(30),
      // One full vCPU: a hot room is one serial stream lane, and the member fan-out's
      // signing/TLS/JSON work is GIL-bound CPU, so wall time per record scales with
      // CPU, and Lambda allots CPU by memory (1769 MB = 1 vCPU; 512 MB was ~0.3).
      memorySize: 1769,
      environment: {
        MESSAGING_TABLE: messagingTable.tableName,
        USERS_TABLE: `${tablePrefix(env)}users`,
        APPSYNC_EVENTS_HTTP_ENDPOINT: this.appsyncEventsHttpEndpoint,
        APPSYNC_EVENTS_REGION: this.appsyncEventsRegion,
      },
      logGroup: new logs.LogGroup(this, "MessagingProjectorLogs", {
        logGroupName: logGroupPath(env, "messaging-projector"),
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    this.messagingProjectorFunctionName = `${resourcePrefix(env)}messaging-projector`;
    messagingTable.grantReadData(projectorFn);
    // Read-only otherwise; the one write is the conditional workspaceId heal
    // stamp on a legacy META row (attribute_not_exists guarded, so this can
    // never clobber a concurrent writer) — see _workspace_id_for_meta.
    messagingTable.grant(projectorFn, "dynamodb:UpdateItem");
    // Activity feed pointer rows (ACTIVITY#<home>#<user>) and away-email checks
    // (AWAY#<user>, T15), written by this stream consumer after the message commits
    // -- not inside the message transaction, as conditional puts (idempotent on
    // replay). A denied AWAY# put fails the record before its room event and push,
    // which silenced every DM and mention from 2026-10-14 to this fix. The LeadingKeys
    // condition scopes THIS PutItem grant to those partitions; it is not a table-wide write
    // fence, because the UpdateItem grant above is unscoped and an UpdateItem can
    // itself create an item.
    projectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:PutItem"],
        resources: [messagingTable.tableArn],
        conditions: {
          "ForAllValues:StringLike": { "dynamodb:LeadingKeys": ["ACTIVITY#*", "AWAY#*"] },
        },
      }),
    );
    // Expo push tokens live on the users table, owned by DatabaseStack. Imported
    // by name so the projector needs no cross-stack prop (read-only: token
    // pruning would need a writer).
    dynamodb.Table.fromTableName(
      this,
      "UsersTableForProjector",
      `${tablePrefix(env)}users`,
    ).grantReadData(projectorFn);
    wsNamespace.grantPublish(projectorFn);
    const projectorEventSource = new lambdaEventSources.DynamoEventSource(
      messagingTable,
      {
        startingPosition: lambda.StartingPosition.TRIM_HORIZON,
        batchSize: 10,
        // Ten concurrent batches per shard, hashed by partition key: one room
        // (pk ROOM#<id>) stays in order on one lane, other rooms stop queuing
        // behind it. ponytail: a single busy room is still one lane.
        parallelizationFactor: 10,
        maxBatchingWindow: cdk.Duration.seconds(0),
        reportBatchItemFailures: true,
        bisectBatchOnError: true,
        maxRecordAge: cdk.Duration.hours(23),
        retryAttempts: 3,
        metricsConfig: { metrics: [lambda.MetricType.EVENT_COUNT] },
        onFailure: new lambdaEventSources.S3OnFailureDestination(
          projectorFailureBucket,
        ),
      },
    );
    projectorFn.addEventSource(projectorEventSource);
    if (tasksTable) {
      // task.changed (ids only) on /ws/{wsChannel}/tasks; index.py routes by the stream ARN.
      projectorFn.addEventSource(
        new lambdaEventSources.DynamoEventSource(tasksTable, {
          startingPosition: lambda.StartingPosition.TRIM_HORIZON,
          batchSize: 25,
          reportBatchItemFailures: true,
          bisectBatchOnError: true,
          maxRecordAge: cdk.Duration.hours(1),
          retryAttempts: 3,
          onFailure: new lambdaEventSources.S3OnFailureDestination(projectorFailureBucket),
        }),
      );
    }

    // ---- Message search: OpenSearch Serverless NextGen ----
    // The projector only ENQUEUES a row pointer; a VPC indexer re-reads the row
    // and writes the index, so an indexing cold start (10-30 s) can never stall
    // live delivery on this stream shard. DynamoDB stays the truth:
    // scripts/reindex_messages.py rebuilds an index.
    // The collection's endpoint and ARN cross to ServiceStack as CloudFormation
    // exports, and Name/Tags/EncryptionConfig/CollectionGroupName force
    // REPLACEMENT: a new stage-wide tag (lightbulb-stage.ts applies cdk.Tags)
    // replaces the collection, blocks on the export, and empties the index.
    // Plan such a change as: drop the import, deploy, change, re-import, reindex.
    // Owner-chosen: the indexing OCU sleeps between drains; new messages are searchable within
    // this many minutes; edits/deletes stay immediate.
    const MESSAGE_SEARCH_BATCH_MINUTES = 39;
    const collectionName = `lightbulb-${env}-messages`;
    const group = new aoss.CfnCollectionGroup(this, "MessageSearchGroup", {
      name: `lightbulb-${env}-search`,
      generation: "NEXTGEN",
      // NEXTGEN rejects DISABLED ("StandbyReplicas cannot be set to DISABLED for
      // NEXTGEN collection groups"); compute still scales to zero when idle.
      standbyReplicas: "ENABLED",
      capacityLimits: {
        minIndexingCapacityInOcu: 0,
        minSearchCapacityInOcu: 0,
        maxIndexingCapacityInOcu: 2,
        maxSearchCapacityInOcu: 2,
      },
    });
    const endpointSg = new ec2.SecurityGroup(this, "MessageSearchEndpointSg", {
      vpc: props.vpc,
      description: "OpenSearch Serverless VPC endpoint (message search)",
      allowAllOutbound: false,
    });
    endpointSg.addIngressRule(ec2.Peer.ipv4(props.vpc.vpcCidrBlock), ec2.Port.tcp(443), "VPC to message search");
    // NextGen (`on.aws`) collections need a standard PrivateLink endpoint (`aoss-data`, private
    // DNS). The aoss-managed (Classic) endpoint's DNS covers only `*.aoss.amazonaws.com`, so
    // traffic went public and the VPC-only network policy answered 401.
    const vpce = new ec2.InterfaceVpcEndpoint(this, "MessageSearchVpce", {
      vpc: props.vpc,
      service: new ec2.InterfaceVpcEndpointService(`com.amazonaws.${this.region}.aoss-data`, 443),
      privateDnsEnabled: true,
      // Dev: one AZ ($7.30/mo an AZ; cross-AZ callers still resolve it). Prod keeps every AZ.
      subnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        ...(env === "prod" ? {} : { availabilityZones: [props.vpc.availabilityZones[0]] }),
      },
      securityGroups: [endpointSg],
      open: false,
    });
    const network = new aoss.CfnSecurityPolicy(this, "MessageSearchNetwork", {
      name: `lightbulb-${env}-msg-net`,
      type: "network",
      policy: cdk.Stack.of(this).toJsonString([{
        Rules: [{ ResourceType: "collection", Resource: [`collection/${collectionName}`] }],
        AllowFromPublic: false,
        SourceVPCEs: [vpce.vpcEndpointId],
      }]),
    });
    const collection = new aoss.CfnCollection(this, "MessageSearchCollection", {
      name: collectionName,
      type: "SEARCH",
      collectionGroupName: group.name, // standby replicas follow the group
      // Same key as the messaging table: search holds the same data.
      encryptionConfig: { awsOwnedKey: false, kmsKeyArn: props.paperclipKmsKeyArn },
    });
    collection.addDependency(group);
    collection.addDependency(network);

    const indexDlq = new sqs.Queue(this, "MessageSearchIndexDlq", {
      queueName: `${resourcePrefix(env)}message-search-index-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });
    // Immediate queue: edits and deletes, so removed or pre-edit text never lingers.
    const indexQueue = new sqs.Queue(this, "MessageSearchIndexQueue", {
      queueName: `${resourcePrefix(env)}message-search-index`,
      // AWS guidance: visibility >= 6x the consumer's timeout (300 s).
      visibilityTimeout: cdk.Duration.minutes(30),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: { queue: indexDlq, maxReceiveCount: 5 },
    });
    // Batch queue: new messages. No event source; MessageSearchBatchDrain empties it.
    const batchQueue = new sqs.Queue(this, "MessageSearchBatchQueue", {
      queueName: `${resourcePrefix(env)}message-search-index-batch`,
      // No consumer receives from it until a drain runs, so a dead drain would otherwise
      // let messages expire silently; the stale alarm below pages first.
      retentionPeriod: cdk.Duration.days(14),
      visibilityTimeout: cdk.Duration.minutes(30),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: { queue: indexDlq, maxReceiveCount: 5 },
    });

    const indexer = new lambda.Function(this, "MessageSearchIndexer", {
      functionName: `${resourcePrefix(env)}message-search-indexer`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "..", "lambda", "messaging-search-indexer")),
      // An immediate job (cold start <=30 s plus a write/verify loop) fits; a drain can run longer.
      timeout: cdk.Duration.seconds(300),
      memorySize: 256,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      environment: {
        MESSAGING_TABLE: messagingTable.tableName,
        SEARCH_ENDPOINT: collection.attrCollectionEndpoint,
        SEARCH_INDEX_BATCH_QUEUE_URL: batchQueue.queueUrl,
      },
      logGroup: new logs.LogGroup(this, "MessageSearchIndexerLogs", {
        logGroupName: logGroupPath(env, "message-search-indexer"),
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    messagingTable.grantReadData(indexer);
    indexer.addToRolePolicy(new iam.PolicyStatement({ actions: ["aoss:APIAccessAll"], resources: [collection.attrArn] }));
    indexer.addEventSource(new lambdaEventSources.SqsEventSource(indexQueue, {
      batchSize: 1,
      reportBatchItemFailures: true,
      maxConcurrency: 5, // a backfill must not fan out past the OCU cap
    }));
    batchQueue.grantConsumeMessages(indexer);
    new events.Rule(this, "MessageSearchBatchDrain", {
      schedule: events.Schedule.rate(cdk.Duration.minutes(MESSAGE_SEARCH_BATCH_MINUTES)),
      targets: [new eventTargets.LambdaFunction(indexer, { event: events.RuleTargetInput.fromObject({ drain: true }) })],
    });
    indexQueue.grantSendMessages(projectorFn);
    batchQueue.grantSendMessages(projectorFn);
    projectorFn.addEnvironment("SEARCH_INDEX_QUEUE_URL", indexQueue.queueUrl);
    projectorFn.addEnvironment("SEARCH_INDEX_BATCH_QUEUE_URL", batchQueue.queueUrl);

    // MUST match service-stack's `roleName: lightbulb-${env}-backend-task`. The
    // provisioner Lambda runs as this role too, so wipes reach DeleteIndex.
    const backendRoleArn = `arn:aws:iam::${this.account}:role/lightbulb-${env}-backend-task`;
    const indexes = [`index/${collectionName}/messages-*`];
    new aoss.CfnAccessPolicy(this, "MessageSearchAccess", {
      name: `lightbulb-${env}-msg-access`,
      type: "data",
      policy: cdk.Stack.of(this).toJsonString([
        {
          // UpdateIndex: session search adds its fields to an existing index (PUT _mapping).
          Rules: [{ ResourceType: "index", Resource: indexes, Permission: ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:UpdateIndex", "aoss:WriteDocument"] }],
          Principal: [indexer.role!.roleArn],
        },
        {
          Rules: [{ ResourceType: "index", Resource: indexes, Permission: ["aoss:ReadDocument", "aoss:DescribeIndex", "aoss:DeleteIndex"] }],
          Principal: [backendRoleArn],
        },
      ]),
    });

    this.messageSearchEndpoint = collection.attrCollectionEndpoint;
    this.messageSearchCollectionArn = collection.attrArn;
    this.messageSearchQueueUrl = indexQueue.queueUrl;

    const pipeRole = new iam.Role(this, "OpenClawWorkPipeRole", {
      roleName: `${resourcePrefix(env)}openclaw-work-pipe`,
      assumedBy: new iam.ServicePrincipal("pipes.amazonaws.com"),
    });
    // DynamoDB ListStreams has no resource-level IAM scope; isolate its wildcard.
    pipeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:ListStreams"],
        resources: ["*"],
      }),
    );
    pipeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:DescribeStream",
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
        ],
        resources: [messagingTable.tableStreamArn!],
      }),
    );
    pipeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["sqs:SendMessage"],
        resources: [workQueue.queueArn, pipeFailureQueue.queueArn],
      }),
    );
    pipeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["kms:Decrypt", "kms:GenerateDataKey"],
        resources: [messagingKey.keyArn],
      }),
    );

    const workPipe = new pipes.CfnPipe(this, "OpenClawWorkPipe", {
      name: `${resourcePrefix(env)}openclaw-work`,
      desiredState: "RUNNING",
      roleArn: pipeRole.roleArn,
      source: messagingTable.tableStreamArn!,
      sourceParameters: {
        filterCriteria: {
          filters: [
            {
              pattern: JSON.stringify({
                eventName: ["INSERT"],
                dynamodb: {
                  NewImage: { sk: { S: [{ prefix: "WORK#" }] } },
                },
              }),
            },
          ],
        },
        dynamoDbStreamParameters: {
          batchSize: 10,
          maximumBatchingWindowInSeconds: 0,
          startingPosition: "TRIM_HORIZON",
          onPartialBatchItemFailure: "AUTOMATIC_BISECT",
          maximumRecordAgeInSeconds: 23 * 60 * 60,
          maximumRetryAttempts: 185,
          deadLetterConfig: { arn: pipeFailureQueue.queueArn },
        },
      },
      target: workQueue.queueArn,
      // No inputTemplate: an object placeholder such as
      // `<$.dynamodb.NewImage.payload.M>` is rendered by the Pipes input
      // transformer as Java-map text (`{kind={S=…}}`), not JSON — every work
      // item then failed the worker's json.loads (dev, 2026-09-10). Without a
      // template the target receives the raw stream record as JSON and the
      // worker unwraps `dynamodb.NewImage.payload.M` itself.
      targetParameters: {
        sqsQueueParameters: {
          messageGroupId: "$.dynamodb.NewImage.messageGroupId.S",
          messageDeduplicationId:
            "$.dynamodb.NewImage.messageDeduplicationId.S",
        },
      },
    });
    // CloudFormation can otherwise create the Pipe after the role but before its
    // separately synthesized inline policy, and Pipes rejects the unready role.
    workPipe.addResourceDependency(
      pipeRole.node.findChild("DefaultPolicy").node.defaultChild as iam.CfnPolicy,
    );

    // Pipes has execution metrics but no state metric. Poll DescribePipe and emit
    // the one missing signal through EMF; missing monitor data also fails closed.
    const pipeStateMonitor = new lambda.Function(this, "OpenClawWorkPipeStateMonitor", {
      functionName: `${resourcePrefix(env)}openclaw-work-pipe-state`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromInline(`
import boto3
import json
import os
import time


def handler(event, context):
    del event, context
    pipe = boto3.client("pipes").describe_pipe(Name=os.environ["PIPE_NAME"])
    not_running = int(
        pipe.get("CurrentState") != "RUNNING"
        or pipe.get("DesiredState") != "RUNNING"
    )
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "LightbulbAI/Messaging",
                "Dimensions": [["PipeName"]],
                "Metrics": [{"Name": "PipeNotRunning", "Unit": "Count"}],
            }],
        },
        "PipeName": os.environ["PIPE_NAME"],
        "PipeNotRunning": not_running,
    }))
`),
      timeout: cdk.Duration.seconds(10),
      environment: { PIPE_NAME: workPipe.name! },
      logGroup: new logs.LogGroup(this, "OpenClawWorkPipeStateMonitorLogs", {
        logGroupName: logGroupPath(env, "openclaw-work-pipe-state"),
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    pipeStateMonitor.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["pipes:DescribePipe"],
        resources: [workPipe.attrArn],
      }),
    );
    new events.Rule(this, "OpenClawWorkPipeStateSchedule", {
      schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
      targets: [new eventTargets.LambdaFunction(pipeStateMonitor)],
    });

    const pageAction = new cloudwatchActions.SnsAction(
      sns.Topic.fromTopicArn(
        this,
        "MessagingAlertsPage",
        alertTopicArn(this.region, this.account, env, "page"),
      ),
    );
    const pageAlarm = (
      id: string,
      props: cloudwatch.AlarmProps,
    ): cloudwatch.Alarm => {
      const alarm = new cloudwatch.Alarm(this, id, props);
      alarm.addAlarmAction(pageAction);
      return alarm;
    };
    pageAlarm("OpenClawWorkDlqVisibleAlarm", {
      alarmName: `${resourcePrefix(env)}openclaw-work-dlq-visible`,
      alarmDescription:
        "PAGE: durable OpenClaw work exhausted queue retries and needs replay.",
      metric: workDlq.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(1),
        statistic: "Maximum",
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pageAlarm("OpenClawWorkOldestAgeAlarm", {
      alarmName: `${resourcePrefix(env)}openclaw-work-oldest-age`,
      alarmDescription:
        "PAGE: durable OpenClaw work has waited longer than five minutes.",
      metric: workQueue.metricApproximateAgeOfOldestMessage({
        period: cdk.Duration.minutes(1),
        statistic: "Maximum",
      }),
      threshold: 5 * 60,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pageAlarm("OpenClawWorkPipeDlqVisibleAlarm", {
      alarmName: `${resourcePrefix(env)}openclaw-work-pipe-dlq-visible`,
      alarmDescription:
        "PAGE: the messaging work Pipe exhausted stream retries and needs replay.",
      metric: pipeFailureQueue.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(1),
        statistic: "Maximum",
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pageAlarm("MessageSearchBatchStaleAlarm", {
      alarmName: `${resourcePrefix(env)}message-search-batch-stale`,
      alarmDescription: "PAGE: message search batch drain is not running; new messages are not becoming searchable.",
      metric: batchQueue.metricApproximateAgeOfOldestMessage({ period: cdk.Duration.minutes(5), statistic: "Maximum" }),
      threshold: 5400, // 90 minutes, about 2 x the 39-minute drain
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pageAlarm("MessageSearchIndexDlqVisibleAlarm", {
      alarmName: `${resourcePrefix(env)}message-search-index-dlq-visible`,
      alarmDescription: "PAGE: message search index jobs exhausted retries; search may show stale or deleted text until replayed.",
      metric: indexDlq.metricApproximateNumberOfMessagesVisible({ period: cdk.Duration.minutes(1), statistic: "Maximum" }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pageAlarm("OpenClawWorkPipeFailuresAlarm", {
      alarmName: `${resourcePrefix(env)}openclaw-work-pipe-failures`,
      alarmDescription:
        "PAGE: the direct messaging stream-to-work-queue Pipe is failing.",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Pipes",
        metricName: "ExecutionFailed",
        dimensionsMap: { PipeName: `${resourcePrefix(env)}openclaw-work` },
        statistic: "Sum",
        period: cdk.Duration.minutes(1),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pageAlarm("OpenClawWorkPipeStoppedAlarm", {
      alarmName: `${resourcePrefix(env)}openclaw-work-pipe-stopped`,
      alarmDescription:
        "PAGE: the direct messaging stream-to-work-queue Pipe is not running.",
      metric: new cloudwatch.Metric({
        namespace: "LightbulbAI/Messaging",
        metricName: "PipeNotRunning",
        dimensionsMap: { PipeName: `${resourcePrefix(env)}openclaw-work` },
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    pageAlarm("MessagingProjectorErrorsAlarm", {
      alarmName: `${resourcePrefix(env)}messaging-projector-errors`,
      alarmDescription:
        "PAGE: canonical messaging records are failing live projection.",
      metric: projectorFn.metricErrors({
        period: cdk.Duration.minutes(1),
        statistic: "Sum",
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pageAlarm("MessagingProjectorFailuresStoredAlarm", {
      alarmName: `${resourcePrefix(env)}messaging-projector-failures-stored`,
      alarmDescription:
        "PAGE: messaging stream records exhausted projector retries and were stored for replay.",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "OnFailureDestinationDeliveredEventCount",
        dimensionsMap: {
          FunctionName: projectorFn.functionName,
          EventSourceMappingUUID: projectorEventSource.eventSourceMappingId,
        },
        period: cdk.Duration.minutes(1),
        statistic: "Sum",
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pageAlarm("MessagingProjectorDestinationFailuresAlarm", {
      alarmName: `${resourcePrefix(env)}messaging-projector-s3-failures`,
      alarmDescription:
        "PAGE: failed messaging stream records could not reach the replay bucket.",
      metric: projectorFn.metric("DestinationDeliveryFailures", {
        period: cdk.Duration.minutes(1),
        statistic: "Sum",
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pageAlarm("MessagingProjectorIteratorAgeAlarm", {
      alarmName: `${resourcePrefix(env)}messaging-projector-iterator-age`,
      alarmDescription:
        "PAGE: live messaging projection is more than five minutes behind DynamoDB.",
      metric: projectorFn.metric("IteratorAge", {
        period: cdk.Duration.minutes(1),
        statistic: "Maximum",
      }),
      threshold: 5 * 60 * 1000,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // -------------------------------------------------------------------
    // AppSync Event API — the live-delivery leg, previously unwatched
    // -------------------------------------------------------------------
    // Every alarm above covers the write path (stream → Pipe → queue → worker)
    // and the projector Lambda. Nothing covered AppSync itself, so the whole
    // browser/mobile delivery leg could fail — subscriptions refused, events
    // dropped on broadcast — while every existing alarm stayed green and the
    // UI just went quiet. Clients do reconcile over HTTP after a gap, which is
    // why dropped/failed events warn while a hard server error pages.
    const warnAction = new cloudwatchActions.SnsAction(
      sns.Topic.fromTopicArn(
        this,
        "MessagingAlertsWarn",
        alertTopicArn(this.region, this.account, env, "warn"),
      ),
    );
    const appsyncMetric = (metricName: string) =>
      new cloudwatch.Metric({
        namespace: "AWS/AppSync",
        metricName,
        // Event APIs publish under EventAPIId, NOT the GraphQLAPIId every
        // AppSync doc example uses — a GraphQLAPIId-dimensioned alarm here
        // would never receive a datapoint.
        dimensionsMap: { EventAPIId: eventApi.apiId },
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
      });
    const appsyncAlarm = (
      id: string,
      metricName: string,
      severity: "page" | "warn",
      description: string,
    ) => {
      const alarm = new cloudwatch.Alarm(this, id, {
        alarmName: `${resourcePrefix(env)}messaging-appsync-${metricName.toLowerCase()}`,
        alarmDescription: description,
        metric: appsyncMetric(metricName),
        threshold: 0,
        evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      alarm.addAlarmAction(severity === "page" ? pageAction : warnAction);
      return alarm;
    };

    appsyncAlarm(
      "MessagingAppSyncSubscribeServerErrorAlarm",
      "SubscribeServerError",
      "page",
      "PAGE: AppSync failed a room/user subscription server-side - clients get no live messages.",
    );
    appsyncAlarm(
      "MessagingAppSyncConnectServerErrorAlarm",
      "ConnectServerError",
      "page",
      "PAGE: AppSync failed a realtime connection server-side - clients cannot open the live socket.",
    );
    appsyncAlarm(
      "MessagingAppSyncBroadcastServerErrorAlarm",
      "BroadcastEventServerError",
      "page",
      "PAGE: AppSync failed to broadcast published events - live delivery is down.",
    );
    appsyncAlarm(
      "MessagingAppSyncBroadcastDroppedAlarm",
      "BroadcastDroppedEvents",
      "warn",
      "AppSync dropped events during broadcast - some clients missed a message until they reconcile over HTTP.",
    );
    appsyncAlarm(
      "MessagingAppSyncFailedEventsAlarm",
      "FailedEvents",
      "warn",
      "AppSync rejected published events - the publish never reached subscribers.",
    );
    appsyncAlarm(
      "MessagingAppSync5xxAlarm",
      "5XXError",
      "warn",
      "AppSync Event API is returning 5xx to clients.",
    );

    // =========================================================================
    // Lambda Authorizer (machine credentials only for WebSocket $connect)
    // =========================================================================

    const authorizerFn = new lambda.Function(this, "WsAuthorizer", {
      functionName: `${resourcePrefix(env)}ws-authorizer`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "websocket-authorizer"),
        {
          bundling: {
            image: lambda.Runtime.PYTHON_3_12.bundlingImage,
            command: [
              "bash",
              "-c",
              // Same target pins as the local bundler below; the flags are
              // redundant inside the 3.12 image but they are part of the
              // bundling cache key, so changing them is what invalidates a
              // stale cached bundle on a persistent runner.
              "pip install -r requirements.txt -t /asset-output --only-binary=:all: --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 && cp -au . /asset-output",
            ],
            local: {
              tryBundle(outputDir: string): boolean {
                try {
                  const srcDir = path.join(__dirname, "..", "..", "lambda", "websocket-authorizer");
                  // Target the LAMBDA's interpreter, not the host's. A bare
                  // `pip3 install -t` resolves wheels for whatever Python the
                  // runner has (3.14 on the self-hosted box), which shipped a
                  // _cffi_backend.cpython-314 .so into a python3.12 function:
                  // Runtime.ImportModuleError on every WebSocket connect. With
                  // an explicit version/platform pip fetches manylinux cp312
                  // binary wheels from any host, matching the Docker path.
                  execSync(
                    `pip3 install -r ${srcDir}/requirements.txt -t ${outputDir} -q ` +
                      "--only-binary=:all: --platform manylinux2014_x86_64 --implementation cp --python-version 3.12",
                    { stdio: "inherit" },
                  );
                  for (const f of fs.readdirSync(srcDir)) {
                    // Files only: backend pytest imports index.py in place
                    // (tests/unit/test_ws_authorizer_*.py) and leaves a
                    // __pycache__/ dir behind; copyFileSync throws EISDIR on
                    // it, silently demoting every synth to the docker path.
                    const src = path.join(srcDir, f);
                    if (fs.statSync(src).isFile()) {
                      fs.copyFileSync(src, path.join(outputDir, f));
                    }
                  }
                  return true;
                } catch {
                  return false;
                }
              },
            },
          },
        },
      ),
      timeout: cdk.Duration.seconds(10),
      environment: {
        // Owner row holding paired_devices[deviceId].publicKey for device-key
        // WS-upgrade auth. Clerk browser sessions are intentionally not an
        // authorization provider for this machine-only API Gateway socket.
        CONTAINERS_TABLE: props.containersTableName,
      },
      logGroup: new logs.LogGroup(this, "WsAuthorizerLogs", {
        logGroupName: logGroupPath(env, "ws-authorizer"),
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });

    // Read-only access to the containers row for device-token pubkey lookup.
    authorizerFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ContainersTableRead",
        actions: ["dynamodb:GetItem"],
        resources: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${props.containersTableName}`,
        ],
      }),
    );

    // --- Paperclip service-token signing key for the WS authorizer ---
    // The Lambda fetches this secret value at cold-start (via boto3) and
    // uses it to verify HS256 service-token JWTs minted by the FastAPI
    // backend. We pass the secret ARN as an env var (rather than the
    // value) so the secret never appears in CFN templates / deploy logs.
    const paperclipServiceTokenSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "PaperclipServiceTokenKeyForAuthorizer",
      props.paperclipServiceTokenKeySecretName,
    );
    paperclipServiceTokenSecret.grantRead(authorizerFn);
    // `grantRead` only adds `secretsmanager:GetSecretValue` — for KMS-encrypted
    // secrets the caller also needs `kms:Decrypt` on the underlying CMK.
    // Without this, cold-start get_secret_value returns AccessDeniedException
    // and the service-token machine path fails closed.
    authorizerFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "KmsDecryptForServiceTokenSecret",
        effect: iam.Effect.ALLOW,
        actions: ["kms:Decrypt"],
        resources: [props.paperclipKmsKeyArn],
      }),
    );
    authorizerFn.addEnvironment(
      "PAPERCLIP_SERVICE_TOKEN_KEY_SECRET_ARN",
      paperclipServiceTokenSecret.secretArn,
    );

    // =========================================================================
    // WebSocket Lambda Functions
    // =========================================================================

    // DynamoDB Gateway Endpoint — free, avoids NAT for DynamoDB traffic
    new ec2.GatewayVpcEndpoint(this, "DynamoDbEndpoint", {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
      vpc: props.vpc,
    });

    // Shared security group for WebSocket Lambdas — allows outbound to ALB + DynamoDB
    const wsLambdaSg = new ec2.SecurityGroup(this, "WsLambdaSg", {
      vpc: props.vpc,
      description: "Security group for WebSocket Lambda functions",
      allowAllOutbound: false,
    });

    wsLambdaSg.addEgressRule(
      props.albSecurityGroup,
      ec2.Port.tcp(80),
      "Allow Lambda to reach ALB on port 80",
    );

    wsLambdaSg.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "Allow HTTPS for DynamoDB and AWS APIs",
    );

    const lambdaDefaults: Omit<lambda.FunctionProps, "functionName" | "code" | "handler"> = {
      runtime: lambda.Runtime.PYTHON_3_12,
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [wsLambdaSg],
      environment: {
        ALB_DNS_NAME: props.alb.loadBalancerDnsName,
        CONNECTIONS_TABLE: connectionsTable.tableName,
      },
    };

    // Explicit lightbulb-named log groups (replaces the per-stack LogRetention
    // singleton Lambda the old logRetention prop shipped).
    const wsLogGroup = (id: string, name: string) =>
      new logs.LogGroup(this, id, {
        logGroupName: logGroupPath(env, name),
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

    // --- ws-connect Lambda ---
    const connectFn = new lambda.Function(this, "WsConnectFn", {
      ...lambdaDefaults,
      functionName: `${resourcePrefix(env)}ws-connect`,
      logGroup: wsLogGroup("WsConnectFnLogs", "ws-connect"),
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "ws-connect"),
      ),
    });
    connectionsTable.grantReadWriteData(connectFn);

    // --- ws-disconnect Lambda ---
    const disconnectFn = new lambda.Function(this, "WsDisconnectFn", {
      ...lambdaDefaults,
      functionName: `${resourcePrefix(env)}ws-disconnect`,
      logGroup: wsLogGroup("WsDisconnectFnLogs", "ws-disconnect"),
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "ws-disconnect"),
      ),
    });
    connectionsTable.grantReadWriteData(disconnectFn);

    // --- ws-message Lambda ---
    const messageFn = new lambda.Function(this, "WsMessageFn", {
      ...lambdaDefaults,
      functionName: `${resourcePrefix(env)}ws-message`,
      logGroup: wsLogGroup("WsMessageFnLogs", "ws-message"),
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "ws-message"),
      ),
    });

    // --- WS internal shared secret for the three Lambdas ---
    // The backend /api/v1/ws/* endpoints derive identity from request headers
    // and are reachable both via these trusted Lambdas AND via the public HTTP
    // API Gateway $default route (which has no authorizer). Each Lambda fetches
    // this secret at cold-start (via boto3, mirroring the authorizer's
    // service-token handling) and attaches it as the `x-lightbulb-ws-internal`
    // header that the public route cannot supply; the backend's
    // require_ws_internal_caller dependency rejects callers that lack it. ARN
    // (not value) goes in the env so the secret never lands in CFN/deploy logs.
    const wsInternalSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "WsInternalSecretForLambdas",
      props.wsInternalSecretSecretName,
    );
    for (const fn of [connectFn, disconnectFn, messageFn]) {
      wsInternalSecret.grantRead(fn);
      // grantRead adds only secretsmanager:GetSecretValue — KMS-encrypted
      // secrets also need kms:Decrypt on the CMK (same as the authorizer).
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "KmsDecryptForWsInternalSecret",
          effect: iam.Effect.ALLOW,
          actions: ["kms:Decrypt"],
          resources: [props.paperclipKmsKeyArn],
        }),
      );
      fn.addEnvironment("WS_INTERNAL_SECRET_SECRET_ARN", wsInternalSecret.secretArn);
    }

    // =========================================================================
    // WebSocket API Gateway (L2)
    // =========================================================================

    const wsDomain = isProd ? `ws.${rootDomain}` : `ws-${env}.${rootDomain}`;

    const wsAuthorizer = new WebSocketLambdaAuthorizer("MachineAuthorizer", authorizerFn, {
      identitySource: ["route.request.querystring.token"],
    });

    const wsApi = new WebSocketApi(this, "WebSocketApi", {
      apiName: `lightbulb-${env}-websocket`,
      routeSelectionExpression: "$request.body.action",
      connectRouteOptions: {
        integration: new WebSocketLambdaIntegration("ConnectIntegration", connectFn),
        authorizer: wsAuthorizer,
      },
      disconnectRouteOptions: {
        integration: new WebSocketLambdaIntegration("DisconnectIntegration", disconnectFn),
      },
      defaultRouteOptions: {
        integration: new WebSocketLambdaIntegration("DefaultIntegration", messageFn),
      },
    });

    // --- WebSocket API Log Group ---
    const wsApiLogGroup = new logs.LogGroup(this, "WsApiLogs", {
      logGroupName: `/aws/api-gateway/lightbulb-${env}-websocket`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // --- WebSocket Stage ---
    const wsStageResource = new WebSocketStage(this, "WsStage", {
      webSocketApi: wsApi,
      stageName: env,
      autoDeploy: true,
    });

    // Wire access logs + execution-log settings to the underlying CfnStage.
    // The L2 WebSocketStage doesn't expose these, but they're required to
    // surface API Gateway integration errors that are otherwise invisible —
    // notably Management API @connections POST failures (PayloadTooLargeException,
    // LimitExceededException) where boto3 returns an empty Error.Message field
    // to the caller.
    //
    // dataTraceEnabled is intentionally false: it logs full message bodies,
    // which contain user chat content. INFO-level logs without dataTrace
    // capture status codes + integrationErrorMessage but not payload bodies.
    const wsCfnStage = wsStageResource.node.defaultChild as apigatewayv2.CfnStage;
    wsCfnStage.accessLogSettings = {
      destinationArn: wsApiLogGroup.logGroupArn,
      format: JSON.stringify({
        requestId: "$context.requestId",
        connectionId: "$context.connectionId",
        eventType: "$context.eventType",
        routeKey: "$context.routeKey",
        status: "$context.status",
        requestTime: "$context.requestTime",
        integrationLatency: "$context.integrationLatency",
        integrationError: "$context.integrationErrorMessage",
        error: "$context.error.message",
        messageId: "$context.messageId",
      }),
    };
    // ERROR, not INFO: INFO wrote ~1.8 GB/mo of execution logs into API Gateway's own
    // `/aws/apigateway/<id>/<stage>` group, which CloudFormation does not own and so has no
    // retention. Integration errors still log at ERROR, and the access log keeps
    // integrationError for every request.
    wsCfnStage.defaultRouteSettings = {
      loggingLevel: "ERROR",
      dataTraceEnabled: false,
      detailedMetricsEnabled: true,
    };

    this.wsApiId = wsApi.apiId;
    this.wsStage = env;

    // =========================================================================
    // API Gateway account-level CloudWatch role (ACCOUNT singleton)
    // =========================================================================
    // Access/execution log delivery for every API Gateway in the account —
    // including the WS stage's accessLogSettings above — silently delivers
    // NOTHING when the account-level CloudWatch role is missing or points
    // at a deleted IAM role (the account setting referenced a dangling
    // role and both envs' access-log groups stayed empty despite traffic).
    // Owned by the PROD stack only: this is an account-wide setting, and
    // letting a non-prod stack own it would mean a dev teardown deletes
    // the role out from under every API in the account.
    if (env === "prod") {
      const apigwAccountRole = new iam.Role(this, "ApiGatewayAccountCloudWatchRole", {
        assumedBy: new iam.ServicePrincipal("apigateway.amazonaws.com"),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName(
            "service-role/AmazonAPIGatewayPushToCloudWatchLogs",
          ),
        ],
      });
      const apigwAccount = new apigateway.CfnAccount(this, "ApiGatewayAccount", {
        cloudWatchRoleArn: apigwAccountRole.roleArn,
      });
      apigwAccount.node.addDependency(apigwAccountRole);
    }

    // =========================================================================
    // WebSocket Custom Domain
    // =========================================================================
    // L2 does not yet support custom domains for WebSocket APIs,
    // so we continue using CfnDomainName + CfnApiMapping.

    if (props.certificate && props.hostedZone) {
      const wsDomainName = new apigatewayv2.CfnDomainName(
        this,
        "WsDomain",
        {
          domainName: wsDomain,
          domainNameConfigurations: [
            {
              certificateArn: props.certificate.certificateArn,
              endpointType: "REGIONAL",
              securityPolicy: "TLS_1_2",
            },
          ],
        },
      );

      const wsMapping = new apigatewayv2.CfnApiMapping(this, "WsApiMapping", {
        apiId: wsApi.apiId,
        domainName: wsDomainName.ref,
        stage: env,
      });
      // `stage` is a literal, so nothing ties the mapping to the Stage
      // resource and CloudFormation creates them concurrently. On a fresh
      // stack the mapping then races the stage and fails with "Invalid stage
      // identifier" (prod api recreate, 2026-08-29, twice at the same second).
      // The HTTP mapping above already carries this edge.
      wsMapping.addDependency(wsStageResource.node.defaultChild as apigatewayv2.CfnStage);

      // --- Route53 A record for WebSocket API ---
      new route53.ARecord(this, "WsApiDnsRecord", {
        zone: props.hostedZone,
        recordName: wsDomain,
        target: route53.RecordTarget.fromAlias({
          bind: () => ({
            dnsName: cdk.Fn.getAtt(wsDomainName.logicalId, "RegionalDomainName").toString(),
            hostedZoneId: cdk.Fn.getAtt(wsDomainName.logicalId, "RegionalHostedZoneId").toString(),
          }),
        }),
      });

      this.webSocketUrl = `wss://${wsDomain}`;
    } else {
      this.webSocketUrl = `wss://${wsApi.apiId}.execute-api.${this.region}.amazonaws.com/${env}`;
    }

    // =========================================================================
    // Management API URL
    // =========================================================================
    this.managementApiUrl = `https://${wsApi.apiId}.execute-api.${this.region}.amazonaws.com/${env}`;

    // =========================================================================
    // CloudFormation Outputs
    // =========================================================================

    new cdk.CfnOutput(this, "HttpApiUrlOutput", {
      value: this.httpApiUrl,
      description: "HTTP API Gateway URL",
      exportName: `lightbulb-${env}-http-api-url`,
    });

    new cdk.CfnOutput(this, "WebSocketUrlOutput", {
      value: this.webSocketUrl,
      description: "WebSocket API Gateway URL",
      exportName: `lightbulb-${env}-websocket-url`,
    });

    new cdk.CfnOutput(this, "ManagementApiUrlOutput", {
      value: this.managementApiUrl,
      description: "WebSocket Management API URL (for pushing messages)",
      exportName: `lightbulb-${env}-management-api-url`,
    });

    new cdk.CfnOutput(this, "ConnectionsTableOutput", {
      value: connectionsTable.tableName,
      description: "DynamoDB connections table name",
      exportName: `lightbulb-${env}-connections-table`,
    });

    new cdk.CfnOutput(this, "MessagingTableOutput", {
      value: this.messagingTableName,
      description: "Canonical messaging DynamoDB table name",
      exportName: `lightbulb-${env}-messaging-table`,
    });

    new cdk.CfnOutput(this, "OpenClawWorkQueueUrlOutput", {
      value: this.openClawWorkQueueUrl,
      description: "Durable OpenClaw work FIFO queue URL",
      exportName: `lightbulb-${env}-openclaw-work-queue-url`,
    });

    new cdk.CfnOutput(this, "OpenClawWorkQueueArnOutput", {
      value: this.openClawWorkQueueArn,
      description: "Durable OpenClaw work FIFO queue ARN",
      exportName: `lightbulb-${env}-openclaw-work-queue-arn`,
    });

    new cdk.CfnOutput(this, "AppSyncEventsHttpEndpointOutput", {
      value: this.appsyncEventsHttpEndpoint,
      description: "AppSync Events HTTP publish endpoint",
      exportName: `lightbulb-${env}-appsync-events-http-endpoint`,
    });

    new cdk.CfnOutput(this, "AppSyncEventsRealtimeEndpointOutput", {
      value: this.appsyncEventsRealtimeEndpoint,
      description: "AppSync Events realtime subscription endpoint",
      exportName: `lightbulb-${env}-appsync-events-realtime-endpoint`,
    });

    new cdk.CfnOutput(this, "AppSyncEventsApiIdOutput", {
      value: eventApi.apiId,
      description: "AppSync messaging Event API ID",
      exportName: `lightbulb-${env}-appsync-events-api-id`,
    });

    new cdk.CfnOutput(this, "AppSyncEventsApiArnOutput", {
      value: this.appsyncEventsApiArn,
      description: "AppSync messaging Event API ARN",
      exportName: `lightbulb-${env}-appsync-events-api-arn`,
    });

    new cdk.CfnOutput(this, "WsApiIdOutput", {
      value: this.wsApiId,
      description: "WebSocket API ID (for IAM ManageConnections ARN)",
      exportName: `lightbulb-${env}-ws-api-id`,
    });

    // =========================================================================
    // bv- door: CloudFront → VPC origin → internal ALB :8081
    //
    // The machine gateway relay (routers/gateway_relay.py) is a raw binary
    // WebSocket relay, and NEITHER existing public door can carry it: the HTTP
    // API above does not pass WebSocket upgrades at all, and the WebSocket
    // API's Management-API push model is per-message HTTP. CloudFront passes
    // WebSockets natively, and a VPC origin reaches the internal ALB without
    // making the ALB public. The construct ids keep their BrowserView names —
    // renaming one replaces the distribution.
    //
    // Path scoping is enforced at the ALB listener (network-stack.ts,
    // BrowserViewListener: fixed-404 default, one /api/v1/gateway/* rule), not
    // here — the listener is what an in-VPC caller can reach directly, so the
    // cap must live there to mean anything.
    //
    // Same env gate as the HTTP API custom domain: no cert/zone (unit-test
    // synth, unknown envs) → no distribution.
    // =========================================================================
    if (props.certificate && props.hostedZone) {
      const browserViewDomain = isProd ? `bv.${rootDomain}` : `bv-${env}.${rootDomain}`;

      // The VPC origin for the named ALB (created one deploy before the
      // distribution referenced it — see the albV2 prop).
      const browserViewVpcOriginV2 = new cloudfront.VpcOrigin(this, "BrowserViewVpcOriginV2", {
        endpoint: cloudfront.VpcOriginEndpoint.applicationLoadBalancer(props.albV2),
        vpcOriginName: `lightbulb-${env}-browser-view`,
        httpPort: 8081,
        protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      });

      const distribution = new cloudfront.Distribution(this, "BrowserViewDistribution", {
        comment: `lightbulb ${env} machine gateway relay`,
        domainNames: [browserViewDomain],
        // The wildcard *.isol8.co cert works here because the deploy region IS
        // us-east-1 (CloudFront only accepts us-east-1 ACM certs).
        certificate: props.certificate,
        defaultBehavior: {
          // The dedicated :8081 listener — NOT :80 — is fixed on the VpcOrigin
          // above. Hitting :80 would bypass the path cap and hand CloudFront
          // the whole FastAPI surface.
          origin: origins.VpcOrigin.withVpcOrigin(browserViewVpcOriginV2, {
            // A WS connection is long-lived but CloudFront's origin read
            // timeout only bounds quiet gaps between reads; the relay's own
            // keepalive traffic keeps a live machine session well under 60s.
            readTimeout: cdk.Duration.seconds(60),
          }),
          // A relay must never be cached, and the viewer's headers (cookies,
          // websocket negotiation) must reach the backend verbatim.
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
        },
      });

      new route53.ARecord(this, "BrowserViewDnsRecord", {
        zone: props.hostedZone,
        recordName: browserViewDomain,
        target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(distribution)),
      });

      this.gatewayRelayUrl = `wss://${browserViewDomain}/api/v1/gateway/ws`;
      new cdk.CfnOutput(this, "GatewayRelayUrlOutput", {
        value: this.gatewayRelayUrl,
        description: "Machine relay (desktop node hosts + Paperclip operators) WS endpoint",
        exportName: `lightbulb-${env}-gateway-relay-url`,
      });
    } else {
      // No cert/zone (unit-test synth, unknown envs): a fixed dev-shaped
      // fallback so service-stack's ECS env map never sees `undefined`.
      this.gatewayRelayUrl = "wss://bv-dev.thelightbulb.company/api/v1/gateway/ws";
    }
  }
}
