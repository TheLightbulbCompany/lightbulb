import * as path from "path";
import * as fs from "fs";
import { execSync } from "child_process";
import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import { WebSocketApi, WebSocketStage } from "aws-cdk-lib/aws-apigatewayv2";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { WebSocketLambdaAuthorizer } from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

// =============================================================================
// ApiStack — HTTP API Gateway + WebSocket API Gateway
// =============================================================================
// HTTP API:  Vercel → API Gateway v2 (HTTP) → VPC Link v2 → ALB → Fargate
// WebSocket: Client → API Gateway v2 (WS) → Lambda → DynamoDB + ALB
// =============================================================================

export interface ApiStackProps extends cdk.StackProps {
  environment: string;
  vpc: ec2.IVpc;
  certificate?: acm.ICertificate;
  hostedZone?: route53.IHostedZone;
  alb: elbv2.IApplicationLoadBalancer;
  albHttpListenerArn: string;
  albSecurityGroup: ec2.ISecurityGroup;
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
}

const THROTTLE_CONFIG: Record<
  string,
  { burstLimit: number; rateLimit: number }
> = {
  dev: { burstLimit: 100, rateLimit: 50 },
  prod: { burstLimit: 500, rateLimit: 200 },
};

export class ApiStack extends cdk.Stack {
  public readonly httpApiUrl: string;
  public readonly webSocketUrl: string;
  public readonly managementApiUrl: string;
  public readonly connectionsTableName: string;
  public readonly wsApiId: string;
  public readonly wsStage: string;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    const env = props.environment;
    const isProd = env === "prod";
    const throttle = THROTTLE_CONFIG[env] ?? THROTTLE_CONFIG.dev;

    // =========================================================================
    // HTTP API Gateway
    // =========================================================================

    const httpApiDomain = isProd ? "api.isol8.co" : `api-${env}.isol8.co`;
    // Paperclip ("Teams") used to have its own API Gateway custom-domain
    // mapping here (companyApiDomain → CompanyHttpDomain). It now lives
    // on Vercel — the frontend project has a host-conditional rewrite
    // that proxies {env}.company.isol8.co through to /__paperclip_proxy__
    // on this same API Gateway. No second custom domain, no per-host
    // dispatch in the backend; the proxy is reached at its normal path.
    const frontendUrl = isProd
      ? "https://isol8.co"
      : `https://${env}.isol8.co`;

    // --- HTTP API ---
    const httpApi = new apigatewayv2.CfnApi(this, "HttpApi", {
      name: `isol8-${env}-api`,
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
        allowOrigins: [frontendUrl],
        allowMethods: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allowHeaders: [
          "Content-Type",
          "Authorization",
          "X-Requested-With",
        ],
        exposeHeaders: ["Content-Type"],
        allowCredentials: true,
        maxAge: 86400,
      },
    });

    // --- VPC Link v2 (for HTTP API → ALB) ---
    const vpcLinkV2 = new apigatewayv2.CfnVpcLink(this, "HttpVpcLink", {
      name: `isol8-${env}-vpc-link`,
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
        },
      },
    );

    // --- Default route ---
    new apigatewayv2.CfnRoute(this, "HttpDefaultRoute", {
      apiId: httpApi.ref,
      routeKey: "$default",
      target: `integrations/${httpIntegration.ref}`,
    });

    // --- HTTP API Log Group ---
    const httpApiLogGroup = new logs.LogGroup(this, "HttpApiLogs", {
      logGroupName: `/aws/api-gateway/isol8-${env}-http`,
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
      tableName: `isol8-${env}-ws-connections`,
      partitionKey: { name: "connectionId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: "ttl",
    });

    // GSI to fan out realtime events from the Teams BFF: given a user_id,
    // return every active browser WS connection for that user. KEYS_ONLY
    // projection — broker only needs connectionId, full row lives at the
    // base table. Spec: docs/superpowers/specs/2026-05-04-teams-realtime-design.md
    connectionsTable.addGlobalSecondaryIndex({
      indexName: "by-user-id",
      partitionKey: { name: "userId", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    this.connectionsTableName = connectionsTable.tableName;

    // =========================================================================
    // Lambda Authorizer (Clerk JWT validation for WebSocket $connect)
    // =========================================================================

    const authorizerFn = new lambda.Function(this, "WsAuthorizer", {
      functionName: `isol8-${env}-ws-authorizer`,
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
              "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
            ],
            local: {
              tryBundle(outputDir: string): boolean {
                try {
                  const srcDir = path.join(__dirname, "..", "..", "lambda", "websocket-authorizer");
                  execSync(`pip3 install -r ${srcDir}/requirements.txt -t ${outputDir} -q`, { stdio: "inherit" });
                  for (const f of fs.readdirSync(srcDir)) {
                    fs.copyFileSync(path.join(srcDir, f), path.join(outputDir, f));
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
        CLERK_ISSUER: isProd
          ? "https://clerk.isol8.co"
          : "https://up-moth-55.clerk.accounts.dev",
        CLERK_JWKS_URL: isProd
          ? "https://clerk.isol8.co/.well-known/jwks.json"
          : "https://up-moth-55.clerk.accounts.dev/.well-known/jwks.json",
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

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
    // and the service-token branch silently falls through to Clerk-only.
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
      logRetention: logs.RetentionDays.ONE_MONTH,
    };

    // --- ws-connect Lambda ---
    const connectFn = new lambda.Function(this, "WsConnectFn", {
      ...lambdaDefaults,
      functionName: `isol8-${env}-ws-connect`,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "ws-connect"),
      ),
    });
    connectionsTable.grantReadWriteData(connectFn);

    // --- ws-disconnect Lambda ---
    const disconnectFn = new lambda.Function(this, "WsDisconnectFn", {
      ...lambdaDefaults,
      functionName: `isol8-${env}-ws-disconnect`,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "ws-disconnect"),
      ),
    });
    connectionsTable.grantReadWriteData(disconnectFn);

    // --- ws-message Lambda ---
    const messageFn = new lambda.Function(this, "WsMessageFn", {
      ...lambdaDefaults,
      functionName: `isol8-${env}-ws-message`,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "ws-message"),
      ),
    });

    // =========================================================================
    // WebSocket API Gateway (L2)
    // =========================================================================

    const wsDomain = isProd ? "ws.isol8.co" : `ws-${env}.isol8.co`;

    const wsAuthorizer = new WebSocketLambdaAuthorizer("ClerkAuthorizer", authorizerFn, {
      identitySource: ["route.request.querystring.token"],
    });

    const wsApi = new WebSocketApi(this, "WebSocketApi", {
      apiName: `isol8-${env}-websocket`,
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
      logGroupName: `/aws/api-gateway/isol8-${env}-websocket`,
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
    wsCfnStage.defaultRouteSettings = {
      loggingLevel: "INFO",
      dataTraceEnabled: false,
      detailedMetricsEnabled: true,
    };

    this.wsApiId = wsApi.apiId;
    this.wsStage = env;

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

      new apigatewayv2.CfnApiMapping(this, "WsApiMapping", {
        apiId: wsApi.apiId,
        domainName: wsDomainName.ref,
        stage: env,
      });

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
      exportName: `isol8-${env}-http-api-url`,
    });

    new cdk.CfnOutput(this, "WebSocketUrlOutput", {
      value: this.webSocketUrl,
      description: "WebSocket API Gateway URL",
      exportName: `isol8-${env}-websocket-url`,
    });

    new cdk.CfnOutput(this, "ManagementApiUrlOutput", {
      value: this.managementApiUrl,
      description: "WebSocket Management API URL (for pushing messages)",
      exportName: `isol8-${env}-management-api-url`,
    });

    new cdk.CfnOutput(this, "ConnectionsTableOutput", {
      value: connectionsTable.tableName,
      description: "DynamoDB connections table name",
      exportName: `isol8-${env}-connections-table`,
    });

    new cdk.CfnOutput(this, "WsApiIdOutput", {
      value: this.wsApiId,
      description: "WebSocket API ID (for IAM ManageConnections ARN)",
      exportName: `isol8-${env}-ws-api-id`,
    });
  }
}
