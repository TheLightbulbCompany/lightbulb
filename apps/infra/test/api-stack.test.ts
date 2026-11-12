import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Match, Template } from "aws-cdk-lib/assertions";
import { ApiStack, wsHandlerCode } from "../lib/stacks/api-stack";
import { NetworkStack } from "../lib/stacks/network-stack";
import { LAUNCH_DOMAIN, ROOT_DOMAIN } from "../lib/domains";
import { buildApiStack, synthApiStack } from "./helpers";

// Hoist a single ApiStack synthesis and reuse across tests.
const API_TEMPLATE = synthApiStack();

function statementsForRole(template: Template, roleLogicalId: string): any[] {
  const resources = [
    ...Object.values(template.findResources("AWS::IAM::Policy")),
    ...Object.values(template.findResources("AWS::IAM::ManagedPolicy")),
  ] as any[];
  return resources.flatMap((resource) => {
    const roles = resource.Properties?.Roles ?? [];
    const attached = roles.some(
      (role: any) => role?.Ref === roleLogicalId,
    );
    return attached
      ? (resource.Properties?.PolicyDocument?.Statement ?? [])
      : [];
  });
}

function actions(statement: any): string[] {
  return Array.isArray(statement.Action)
    ? statement.Action
    : [statement.Action].filter(Boolean);
}

describe("ApiStack — ws-connections owner-keyed fan-out", () => {
  const template: Template = API_TEMPLATE;

  test("ws-connections has a by-owner-id GSI projecting userId + connectionType", () => {
    // Owner-keyed fan-out reads recipients from this GSI: given an ownerId
    // (org_id for orgs, user_id for personal), return every active connection
    // for that owner. INCLUDE projection carries the member userId (per-member
    // demux) + connectionType (chat filter excludes node/operator pipes).
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-owner-id",
          KeySchema: [{ AttributeName: "ownerId", KeyType: "HASH" }],
          Projection: {
            ProjectionType: "INCLUDE",
            NonKeyAttributes: Match.arrayWith(["userId", "connectionType"]),
          },
        }),
      ]),
    });
  });
});

describe("ApiStack — machine-only WebSocket authorizer", () => {
  test.each(["dev", "prod"] as const)(
    "%s authorizer has no Clerk browser configuration",
    (envName) => {
      const template = envName === "dev" ? API_TEMPLATE : synthApiStack("prod");
      const functions = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ) as any[];
      const authorizer = functions.find(
        (resource) =>
          resource.Properties?.FunctionName ===
          `lightbulb-${envName}-ws-authorizer`,
      );
      expect(authorizer).toBeDefined();
      const variables = authorizer.Properties?.Environment?.Variables ?? {};
      expect(variables.CONTAINERS_TABLE).toBeDefined();
      expect(
        Object.keys(variables).filter((key) => key.startsWith("CLERK_")),
      ).toEqual([]);
      const roleId = authorizer.Properties.Role["Fn::GetAtt"][0];
      const tableRead = statementsForRole(template, roleId).find(
        (statement) => statement.Sid === "ContainersTableRead",
      );
      expect(tableRead).toBeDefined();
      expect(actions(tableRead!)).toEqual(["dynamodb:GetItem"]);
      expect(JSON.stringify(tableRead!.Resource)).not.toContain("/index/");
      expect(JSON.stringify(tableRead!.Resource)).not.toContain('"*"');
    },
  );
});


describe("ApiStack — production browser CORS origins", () => {
  test("prod API Gateway CORS accepts apex and www frontend origins", () => {
    const prod = synthApiStack("prod");
    prod.hasResourceProperties("AWS::ApiGatewayV2::Api", {
      CorsConfiguration: Match.objectLike({
        AllowOrigins: [
          `https://${ROOT_DOMAIN}`,
          `https://www.${ROOT_DOMAIN}`,
          `https://${LAUNCH_DOMAIN}`,
          `https://www.${LAUNCH_DOMAIN}`,
        ],
      }),
    });
  });
});

describe("ApiStack — API Gateway account CloudWatch role (access/execution logging)", () => {
  // The WS stage's accessLogSettings silently deliver NOTHING when the
  // account-level CloudWatch role is missing or points at a deleted IAM
  // role (observed live: cloudwatchRoleArn referenced a NoSuchEntity role
  // and both env's access-log groups stayed empty despite real traffic).
  // The role + AWS::ApiGateway::Account setting are ACCOUNT-level
  // singletons: only the prod stack may own them, so tearing down a dev
  // stack can never delete the role out from under every API in the
  // account.

  test("prod stack owns the account-level CloudWatch role", () => {
    const prod = synthApiStack("prod");
    prod.hasResourceProperties("AWS::IAM::Role", {
      AssumeRolePolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: { Service: "apigateway.amazonaws.com" },
          }),
        ]),
      }),
      ManagedPolicyArns: Match.arrayWith([
        Match.objectLike({
          "Fn::Join": Match.arrayWith([
            Match.arrayWith([
              Match.stringLikeRegexp("AmazonAPIGatewayPushToCloudWatchLogs"),
            ]),
          ]),
        }),
      ]),
    });
    prod.resourceCountIs("AWS::ApiGateway::Account", 1);
  });

  test("dev stack does NOT own the account-level singleton", () => {
    API_TEMPLATE.resourceCountIs("AWS::ApiGateway::Account", 0);
  });
});

describe("ApiStack — browser-view CloudFront door", () => {
  // The distribution only synthesizes when cert + zone are provided (same
  // gate as the HTTP API custom domain), so this needs its own harness —
  // the shared one deliberately passes neither.
  function synthWithDomain(environment: "dev" | "prod"): Template {
    const app = new cdk.App();
    const env = { account: "877352799272", region: "us-east-1" };
    const network = new NetworkStack(app, `Network-${environment}`, { env, environment });
    const support = new cdk.Stack(app, `Support-${environment}`, { env });
    const containersTable = new dynamodb.Table(support, "Containers", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });
    const api = new ApiStack(app, `Api-${environment}`, {
      env,
      environment,
      vpc: network.vpc,
      alb: network.alb,
      albV2: network.albV2,
      albHttpListenerArn: network.albHttpListenerArn,
      albSecurityGroup: network.albSecurityGroup,
      certificate: acm.Certificate.fromCertificateArn(
        support,
        "Cert",
        "arn:aws:acm:us-east-1:877352799272:certificate/test-cert",
      ),
      hostedZone: route53.HostedZone.fromHostedZoneAttributes(support, "Zone", {
        hostedZoneId: "Z0TEST",
        zoneName: ROOT_DOMAIN,
      }),
      clerkIssuerSecretName: `lightbulb/${environment}/clerk_issuer`,
      paperclipKmsKeyArn: `arn:aws:kms:us-east-1:877352799272:key/test-key`,
      wsInternalSecretSecretName: `isol8/${environment}/ws_internal_secret`,
      containersTableName: containersTable.tableName,
    });
    return Template.fromStack(api);
  }

  test("distribution targets the ALB VPC origin on 8081, never :80", () => {
    // :80's default action is the whole backend target group; the path cap
    // lives only on the 8081 listener. A distribution pointed at 80 would
    // silently hand CloudFront the entire FastAPI surface.
    const template = synthWithDomain("dev");
    template.hasResourceProperties("AWS::CloudFront::VpcOrigin", {
      VpcOriginEndpointConfig: Match.objectLike({
        HTTPPort: 8081,
        OriginProtocolPolicy: "http-only",
      }),
    });
    template.hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({
        Aliases: [`bv-dev.${ROOT_DOMAIN}`],
        DefaultCacheBehavior: Match.objectLike({
          // CachingDisabled + AllViewer managed policy ids — a cached or
          // header-stripped relay is a broken relay.
          CachePolicyId: "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
          OriginRequestPolicyId: "216adef6-5c7f-47e4-b989-5492eafa07d3",
          ViewerProtocolPolicy: "https-only",
        }),
      }),
    });
  });

  test("prod alias is bv.<root domain>", () => {
    synthWithDomain("prod").hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({ Aliases: [`bv.${ROOT_DOMAIN}`] }),
    });
  });

  test("no cert/zone -> no distribution (unknown-env synth stays clean)", () => {
    API_TEMPLATE.resourceCountIs("AWS::CloudFront::Distribution", 0);
  });
});

describe("ApiStack — Slack events route has its own throttle bucket", () => {
  const template: Template = API_TEMPLATE;

  test("explicit POST /api/v1/slack/events route exists", () => {
    template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
      RouteKey: "POST /api/v1/slack/events",
    });
  });

  test("stage carries route-level throttling for the Slack route", () => {
    template.hasResourceProperties("AWS::ApiGatewayV2::Stage", {
      RouteSettings: Match.objectLike({
        "POST /api/v1/slack/events": Match.objectLike({
          ThrottlingBurstLimit: Match.anyValue(),
          ThrottlingRateLimit: Match.anyValue(),
        }),
      }),
    });
  });
});

describe("ApiStack — public session-link routes have their own throttle bucket", () => {
  test("route and stage settings exist", () => {
    API_TEMPLATE.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: "ANY /api/v1/public/{proxy+}" });
    API_TEMPLATE.hasResourceProperties("AWS::ApiGatewayV2::Stage", {
      RouteSettings: Match.objectLike({
        "ANY /api/v1/public/{proxy+}": Match.objectLike({ ThrottlingBurstLimit: 20, ThrottlingRateLimit: 10 }),
      }),
    });
  });
});

describe("ApiStack — canonical messaging storage and work delivery", () => {
  test("messaging table has the frozen keys, stream, TTL, GSI, PITR, on-demand billing, and CMK", () => {
    API_TEMPLATE.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-messaging",
      BillingMode: "PAY_PER_REQUEST",
      KeySchema: [
        { AttributeName: "pk", KeyType: "HASH" },
        { AttributeName: "sk", KeyType: "RANGE" },
      ],
      StreamSpecification: { StreamViewType: "NEW_IMAGE" },
      TimeToLiveSpecification: { AttributeName: "expiresAt", Enabled: true },
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      SSESpecification: Match.objectLike({
        SSEEnabled: true,
        SSEType: "KMS",
        KMSMasterKeyId: Match.anyValue(),
      }),
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-member",
          KeySchema: [
            { AttributeName: "memberKey", KeyType: "HASH" },
            { AttributeName: "memberSort", KeyType: "RANGE" },
          ],
          Projection: { ProjectionType: "ALL" },
        }),
        Match.objectLike({
          IndexName: "by-run-deadline",
          KeySchema: [
            { AttributeName: "sweep", KeyType: "HASH" },
            { AttributeName: "deadlineAt", KeyType: "RANGE" },
          ],
          Projection: { ProjectionType: "KEYS_ONLY" },
        }),
      ]),
    });
  });

  test("production retains the canonical table and projector failure bucket", () => {
    const prod = synthApiStack("prod");
    const retained = [
      ...Object.values(prod.findResources("AWS::DynamoDB::Table")),
      ...Object.values(prod.findResources("AWS::S3::Bucket")),
    ].filter((resource: any) =>
      [
        "lightbulb-prod-messaging",
        "lightbulb-prod-messaging-projector-fail-877352799272-us-east-1",
      ].includes(resource.Properties?.TableName ?? resource.Properties?.BucketName),
    ) as any[];
    expect(retained).toHaveLength(2);
    expect(retained.every((resource) => resource.DeletionPolicy === "Retain")).toBe(true);
  });

  test("creates one application FIFO plus its FIFO DLQ, one standard Pipe DLQ without search queues", () => {
    const queues = Object.values(API_TEMPLATE.findResources("AWS::SQS::Queue")) as any[];
    const byName = Object.fromEntries(
      queues.map((queue) => [queue.Properties.QueueName, queue.Properties]),
    );
    expect(Object.keys(byName).sort()).toEqual([
      "lightbulb-dev-openclaw-work-dlq.fifo",
      "lightbulb-dev-openclaw-work-pipe-failures",
      "lightbulb-dev-openclaw-work.fifo",
    ]);
    expect(byName["lightbulb-dev-openclaw-work.fifo"]).toEqual(
      expect.objectContaining({
        FifoQueue: true,
        ContentBasedDeduplication: false,
        MessageRetentionPeriod: 604800,
        VisibilityTimeout: 240,
        ReceiveMessageWaitTimeSeconds: 20,
        KmsMasterKeyId: expect.anything(),
        RedrivePolicy: expect.objectContaining({ maxReceiveCount: 30 }),
      }),
    );
    expect(byName["lightbulb-dev-openclaw-work-dlq.fifo"]).toEqual(
      expect.objectContaining({
        FifoQueue: true,
        MessageRetentionPeriod: 1209600,
        KmsMasterKeyId: expect.anything(),
      }),
    );
    expect(byName["lightbulb-dev-openclaw-work-pipe-failures"]).toEqual(
      expect.objectContaining({
        MessageRetentionPeriod: 1209600,
        KmsMasterKeyId: expect.anything(),
      }),
    );
    expect(byName["lightbulb-dev-openclaw-work-pipe-failures"].FifoQueue).toBeUndefined();
  });

  test("projector failure bucket is private, CMK encrypted, and expires failures", () => {
    API_TEMPLATE.hasResourceProperties("AWS::S3::Bucket", {
      BucketName:
        "lightbulb-dev-messaging-projector-fail-877352799272-us-east-1",
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: Match.objectLike({
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({
            ServerSideEncryptionByDefault: Match.objectLike({
              SSEAlgorithm: "aws:kms",
              KMSMasterKeyID: Match.anyValue(),
            }),
          }),
        ]),
      }),
      LifecycleConfiguration: Match.objectLike({
        Rules: Match.arrayWith([
          Match.objectLike({ ExpirationInDays: 30, Status: "Enabled" }),
        ]),
      }),
    });
  });

  test("direct Pipe filters only WORK inserts and sends the raw stream record to FIFO", () => {
    API_TEMPLATE.resourceCountIs("AWS::Pipes::Pipe", 1);
    API_TEMPLATE.hasResourceProperties("AWS::Pipes::Pipe", {
      Name: "lightbulb-dev-openclaw-work",
      DesiredState: "RUNNING",
      SourceParameters: {
        FilterCriteria: {
          Filters: [
            {
              Pattern: JSON.stringify({
                eventName: ["INSERT"],
                dynamodb: {
                  NewImage: { sk: { S: [{ prefix: "WORK#" }] } },
                },
              }),
            },
          ],
        },
        DynamoDBStreamParameters: Match.objectLike({
          BatchSize: 10,
          MaximumBatchingWindowInSeconds: 0,
          StartingPosition: "TRIM_HORIZON",
          OnPartialBatchItemFailure: "AUTOMATIC_BISECT",
          MaximumRecordAgeInSeconds: 82800,
          MaximumRetryAttempts: 185,
          DeadLetterConfig: { Arn: Match.anyValue() },
        }),
      },
      TargetParameters: {
        // An object placeholder renders as Java-map text, not JSON.
        InputTemplate: Match.absent(),
        SqsQueueParameters: {
          MessageGroupId: "$.dynamodb.NewImage.messageGroupId.S",
          MessageDeduplicationId:
            "$.dynamodb.NewImage.messageDeduplicationId.S",
        },
      },
    });
  });

  test("direct Pipe waits for its role policy", () => {
    const roles = API_TEMPLATE.findResources("AWS::IAM::Role");
    const roleId = Object.entries(roles).find(
      ([, role]: [string, any]) =>
        role.Properties?.RoleName === "lightbulb-dev-openclaw-work-pipe",
    )![0];
    const policies = API_TEMPLATE.findResources("AWS::IAM::Policy");
    const policyId = Object.entries(policies).find(
      ([, policy]: [string, any]) =>
        policy.Properties?.Roles?.some((role: any) => role.Ref === roleId),
    )![0];
    const pipe = Object.values(
      API_TEMPLATE.findResources("AWS::Pipes::Pipe"),
    )[0] as any;
    const dependencies = Array.isArray(pipe.DependsOn)
      ? pipe.DependsOn
      : [pipe.DependsOn];
    expect(dependencies).toContain(policyId);
  });

  test("Pipe role is the sole work-queue producer and has only stream/send/KMS actions", () => {
    const roleEntry = Object.entries(
      API_TEMPLATE.findResources("AWS::IAM::Role"),
    ).find(([, role]: [string, any]) =>
      role.Properties?.RoleName === "lightbulb-dev-openclaw-work-pipe",
    );
    expect(roleEntry).toBeDefined();
    const roleId = roleEntry![0];
    const statements = statementsForRole(API_TEMPLATE, roleId);
    const granted = new Set(statements.flatMap(actions));
    expect(granted).toEqual(
      new Set([
        "dynamodb:DescribeStream",
        "dynamodb:GetRecords",
        "dynamodb:GetShardIterator",
        "dynamodb:ListStreams",
        "sqs:SendMessage",
        "kms:Decrypt",
        "kms:GenerateDataKey",
      ]),
    );
    expect(granted.has("sqs:ReceiveMessage")).toBe(false);
    expect(granted.has("sqs:DeleteMessage")).toBe(false);
    const wildcardStatements = statements.filter((statement) =>
      JSON.stringify(statement.Resource).includes('"*"'),
    );
    expect(wildcardStatements).toHaveLength(1);
    expect(actions(wildcardStatements[0])).toEqual(["dynamodb:ListStreams"]);

    const queues = API_TEMPLATE.findResources("AWS::SQS::Queue");
    const workQueueId = Object.entries(queues).find(
      ([, queue]: [string, any]) =>
        queue.Properties.QueueName === "lightbulb-dev-openclaw-work.fifo",
    )![0];
    const pipeFailureQueueId = Object.entries(queues).find(
      ([, queue]: [string, any]) =>
        queue.Properties.QueueName ===
        "lightbulb-dev-openclaw-work-pipe-failures",
    )![0];
    const sendStatement = statements.find((statement) =>
      actions(statement).includes("sqs:SendMessage"),
    );
    expect(sendStatement).toBeDefined();
    expect(sendStatement!.Resource).toHaveLength(2);
    expect(JSON.stringify(sendStatement!.Resource)).toContain(workQueueId);
    expect(JSON.stringify(sendStatement!.Resource)).toContain(pipeFailureQueueId);
    const policies = [
      ...Object.values(API_TEMPLATE.findResources("AWS::IAM::Policy")),
      ...Object.values(API_TEMPLATE.findResources("AWS::IAM::ManagedPolicy")),
    ] as any[];
    for (const policy of policies) {
      const sendsToWork = (policy.Properties?.PolicyDocument?.Statement ?? []).some(
        (statement: any) =>
          actions(statement).includes("sqs:SendMessage") &&
          JSON.stringify(statement.Resource).includes(workQueueId),
      );
      if (sendsToWork) {
        expect(policy.Properties.Roles).toEqual([{ Ref: roleId }]);
      }
    }
  });

  test("messaging stream has exactly the projector and direct Pipe consumers", () => {
    const streamMappings = Object.values(
      API_TEMPLATE.findResources("AWS::Lambda::EventSourceMapping"),
    ).filter((m: any) => m.Properties.EventSourceArn["Fn::GetAtt"]?.[0]?.startsWith("MessagingTable"));
    expect(streamMappings).toHaveLength(1);
    API_TEMPLATE.resourceCountIs("AWS::Pipes::Pipe", 1);
    API_TEMPLATE.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      BatchSize: 10,
      MaximumBatchingWindowInSeconds: 0,
      StartingPosition: "TRIM_HORIZON",
      BisectBatchOnFunctionError: true,
      FunctionResponseTypes: ["ReportBatchItemFailures"],
      MaximumRecordAgeInSeconds: 82800,
      MaximumRetryAttempts: 3,
      MetricsConfig: { Metrics: ["EventCount"] },
      DestinationConfig: { OnFailure: { Destination: Match.anyValue() } },
    });
  });
});

describe("ApiStack — messaging AppSync Events (OIDC subscribe / IAM publish)", () => {
  test("Event API has only Clerk OIDC and IAM with the frozen defaults", () => {
    API_TEMPLATE.resourceCountIs("AWS::AppSync::Api", 1);
    API_TEMPLATE.resourceCountIs("AWS::AppSync::ApiKey", 0);
    API_TEMPLATE.hasResourceProperties("AWS::AppSync::Api", {
      Name: "lightbulb-dev-messaging-events",
      EventConfig: Match.objectLike({
        AuthProviders: Match.arrayWith([
          Match.objectLike({
            AuthType: "OPENID_CONNECT",
            OpenIDConnectConfig: Match.objectLike({
              ClientId: "^lightbulb-appsync$",
              Issuer: Match.anyValue(),
            }),
          }),
          { AuthType: "AWS_IAM" },
        ]),
        ConnectionAuthModes: [{ AuthType: "OPENID_CONNECT" }, { AuthType: "AWS_LAMBDA" }],
        DefaultPublishAuthModes: [{ AuthType: "AWS_IAM" }],
        DefaultSubscribeAuthModes: [{ AuthType: "OPENID_CONNECT" }],
      }),
    });
    const eventApi = Object.values(
      API_TEMPLATE.findResources("AWS::AppSync::Api"),
    )[0] as any;
    expect(eventApi.Properties.EventConfig.LogConfig).toMatchObject({
      LogLevel: "ERROR",
    });
    expect(
      eventApi.Properties.EventConfig.LogConfig.CloudWatchLogsRoleArn,
    ).toBeDefined();
    // The L2 EventApi construct sets retention via a Custom::LogRetention
    // resource (not a plain AWS::Logs::LogGroup) since /aws/appsync/apis/<id>
    // may already exist once the API starts logging.
    API_TEMPLATE.hasResourceProperties("Custom::LogRetention", {
      LogGroupName: Match.objectLike({
        "Fn::Join": Match.arrayWith([
          Match.arrayWith(["/aws/appsync/apis/"]),
        ]),
      }),
      RetentionInDays: 14,
    });
  });

  test("Event API logs to a dedicated role with only the AppSync-to-CloudWatch managed policy", () => {
    const roleEntry = Object.entries(
      API_TEMPLATE.findResources("AWS::IAM::Role"),
    ).find(([, role]: [string, any]) =>
      role.Properties?.AssumeRolePolicyDocument?.Statement?.some(
        (stmt: any) => stmt.Principal?.Service === "appsync.amazonaws.com",
      ),
    );
    expect(roleEntry).toBeDefined();
    const [, role] = roleEntry!;
    expect(
      JSON.stringify((role as any).Properties.ManagedPolicyArns),
    ).toContain("AWSAppSyncPushToCloudWatchLogs");
  });

  test("one read-only DynamoDB data source backs the ws namespace handler", () => {
    API_TEMPLATE.resourceCountIs("AWS::AppSync::DataSource", 1);
    API_TEMPLATE.hasResourceProperties("AWS::AppSync::DataSource", {
      Type: "AMAZON_DYNAMODB",
      DynamoDBConfig: Match.objectLike({
        AwsRegion: "us-east-1",
      }),
      ServiceRoleArn: Match.anyValue(),
    });
    const dataSource = Object.values(
      API_TEMPLATE.findResources("AWS::AppSync::DataSource"),
    )[0] as any;
    const dataSourceRole = dataSource.Properties.ServiceRoleArn["Fn::GetAtt"][0];
    const dataSourceActions = new Set(
      statementsForRole(API_TEMPLATE, dataSourceRole).flatMap(actions),
    );
    expect(dataSourceActions.has("dynamodb:GetItem")).toBe(true);
    expect(dataSourceActions.has("dynamodb:BatchGetItem")).toBe(true);
    for (const forbidden of [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:TransactWriteItems",
    ]) {
      expect(dataSourceActions.has(forbidden)).toBe(false);
    }
    // `ws` for members, `pub` for an "Anyone with the link" session link's live terminal.
    API_TEMPLATE.resourceCountIs("AWS::AppSync::ChannelNamespace", 2);
    API_TEMPLATE.hasResourceProperties("AWS::AppSync::ChannelNamespace", {
      Name: "pub",
      PublishAuthModes: [{ AuthType: "AWS_IAM" }],
      SubscribeAuthModes: [{ AuthType: "AWS_LAMBDA" }],
    });
    // The authorizer answers fresh every time and can read only link-viewer leases.
    API_TEMPLATE.hasResourceProperties("AWS::AppSync::Api", {
      EventConfig: Match.objectLike({
        AuthProviders: Match.arrayWith([
          Match.objectLike({
            AuthType: "AWS_LAMBDA",
            LambdaAuthorizerConfig: Match.objectLike({ AuthorizerResultTtlInSeconds: 0 }),
          }),
        ]),
      }),
    });
    expect(JSON.stringify(API_TEMPLATE.findResources("AWS::IAM::Policy"))).toContain('"dynamodb:LeadingKeys":["PUBWATCH#*"]');
    API_TEMPLATE.hasResourceProperties("AWS::AppSync::ChannelNamespace", {
      Name: "ws",
      PublishAuthModes: [{ AuthType: "AWS_IAM" }],
      SubscribeAuthModes: [{ AuthType: "OPENID_CONNECT" }],
      HandlerConfigs: {
        OnPublish: Match.objectLike({
          Behavior: "CODE",
          Integration: Match.objectLike({ DataSourceName: Match.anyValue() }),
        }),
        OnSubscribe: Match.objectLike({
          Behavior: "CODE",
          Integration: Match.objectLike({ DataSourceName: Match.anyValue() }),
        }),
      },
    });
    const namespaces = Object.values(
      API_TEMPLATE.findResources("AWS::AppSync::ChannelNamespace"),
    ) as any[];
    const code = JSON.stringify(namespaces.map((resource) => resource.Properties.CodeHandlers));
    expect(code).toContain("BatchGetItem");
    expect(code).toContain("consistentRead");
    expect(code).toContain("MEMBER#human#");
    expect(code).toContain("STREAM#");
    expect(code).toContain("deliveryKey");
    expect(code).toContain("util.unauthorized");
    // APPSYNC_JS rejects regex literals at CreateChannelNamespace time
    // (@aws-appsync/no-regex); UUID segments must go through util.matches.
    expect(code).toContain("util.matches(UUID,");
    expect(code).not.toContain("= /^");
    expect(code).toContain("'ws'");
    expect(code).toContain("userArn");
    expect(code).toContain("owner-bedrock-");
    // A foreign environment's owner role (same AWS account) is refused, not trusted.
    expect(code).toContain("'-owner-bedrock-'");
    expect(code).toContain("indexOf('-owner-bedrock-') !== -1) util.unauthorized()");
    expect(code).toContain("workspaceId");
  });

  test("outputs both endpoints, API ID, and API ARN without channel keys", () => {
    const outputs = API_TEMPLATE.toJSON().Outputs as Record<string, any>;
    const rendered = JSON.stringify(outputs);
    expect(rendered).toContain("AppSync Events HTTP publish endpoint");
    expect(rendered).toContain("AppSync Events realtime subscription endpoint");
    expect(rendered).toContain("AppSync messaging Event API ID");
    expect(rendered).toContain("AppSync messaging Event API ARN");
    expect(rendered).not.toContain("deliveryKey");
    expect(rendered).not.toContain("channelKey");
  });
});

describe("ApiStack — ws frames/{roomId}/{deliveryKey} sub-channel (parsePath)", () => {
  // The handler code is an AppSync JS resolver template, not an ES module: strip the
  // import (no @aws-appsync/utils package here) and the `export` keywords, then eval
  // it with a stub `util` to exercise parsePath directly.
  //
  // The sub-channel is a SIBLING of `rooms` (`/ws/{workspaceId}/frames/{roomId}/{deliveryKey}`),
  // not a suffix on it: an AppSync Event API channel path caps at 5 segments including the
  // namespace, so `/ws/{workspaceId}/rooms/{roomId}/{deliveryKey}/frames` (6) is rejected by
  // the service before parsePath ever runs.
  function loadParsePath(): (ctx: unknown) => unknown {
    const code = wsHandlerCode("t", "dev")
      .replace(/^import .*;\n/m, "")
      .replace(/^export /gm, "");
    const util = {
      matches: (re: string, s: string) => new RegExp(re).test(s),
      unauthorized: () => {
        throw 0;
      },
    };
    const factory = new Function("util", `${code}\nreturn parsePath;`);
    return factory(util);
  }

  const WORKSPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const ROOM = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const DELIVERY_KEY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const ctxFor = (path: string) => ({ info: { channel: { path } } });

  test("accepts /rooms/ and /frames/ identically", () => {
    const parsePath = loadParsePath();
    const expected = {
      kind: "rooms",
      workspaceId: WORKSPACE,
      roomId: ROOM,
      deliveryKey: DELIVERY_KEY,
    };
    expect(
      parsePath(ctxFor(`/ws/${WORKSPACE}/rooms/${ROOM}/${DELIVERY_KEY}`)),
    ).toEqual(expected);
    expect(
      parsePath(ctxFor(`/ws/${WORKSPACE}/frames/${ROOM}/${DELIVERY_KEY}`)),
    ).toEqual(expected);
  });

  test("rejects a 6-segment /rooms/.../frames suffix, a deeper /frames path, a too-short /frames path, and frames appended to the users namespace", () => {
    const parsePath = loadParsePath();
    for (const path of [
      `/ws/${WORKSPACE}/rooms/${ROOM}/${DELIVERY_KEY}/frames`,
      `/ws/${WORKSPACE}/frames/${ROOM}/${DELIVERY_KEY}/x`,
      `/ws/${WORKSPACE}/frames/${ROOM}`,
      `/ws/${WORKSPACE}/users/${ROOM}/frames`,
    ]) {
      expect(() => parsePath(ctxFor(path))).toThrow();
    }
  });
});

describe("ApiStack — ws sessions workspace channel", () => {
  const WORKSPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const OTHER = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const handlers = () => {
    const code = wsHandlerCode("t", "dev").replace(/^import .*;\n/m, "").replace(/^export /gm, "");
    const util = {
      matches: (re: string, s: string) => new RegExp(re).test(s),
      unauthorized: () => {
        throw 0;
      },
      dynamodb: { toMapValues: (v: unknown) => v },
    };
    return new Function("util", `${code}\nreturn { parsePath, onSubscribe, onPublish };`)(util);
  };
  const path = `/ws/${WORKSPACE}/sessions`;
  const ctx = (rows: unknown[], extra: Record<string, unknown> = {}) => ({
    info: { channel: { path } },
    identity: { sub: "user_1" },
    result: { data: { t: rows } },
    ...extra,
  });
  const meta = { workspaceId: WORKSPACE, ownerId: "org_1" };

  test("subscribe needs the subscriber's active WORKSPACE#<ws>/MEMBER#<sub> row", () => {
    const { onSubscribe, parsePath } = handlers();
    expect(onSubscribe.request(ctx([])).tables.t.keys).toEqual([
      { pk: `WORKSPACE#${WORKSPACE}`, sk: "META" },
      { pk: `WORKSPACE#${WORKSPACE}`, sk: "MEMBER#user_1" },
    ]);
    expect(() => onSubscribe.response(ctx([meta, { active: true }]))).not.toThrow();
    expect(() => onSubscribe.response(ctx([meta, null]))).toThrow(); // not a member / removed
    expect(() => onSubscribe.response(ctx([meta, { active: false }]))).toThrow();
    expect(() => onSubscribe.response(ctx([null, { active: true }]))).toThrow();
    expect(() => onSubscribe.response(ctx([{ ...meta, workspaceId: OTHER }, { active: true }]))).toThrow();
    for (const bad of [`/ws/${WORKSPACE}/sessions/${OTHER}`, `/ws/${WORKSPACE}/*`, `/ws/${WORKSPACE}`]) {
      expect(() => parsePath({ info: { channel: { path: bad } } })).toThrow();
    }
  });

  test("an owner task role cannot publish to another workspace's sessions channel", () => {
    const { onPublish } = handlers();
    const role = (name: string) => ({ userArn: `arn:aws:sts::1:assumed-role/${name}/s` });
    const publish = (identity: unknown) => onPublish.response({ info: { channel: { path } }, identity, result: meta, events: [1] });
    expect(publish(role("lightbulb-dev-backend"))).toEqual([1]);
    expect(publish(role("lightbulb-dev-owner-bedrock-org_1"))).toEqual([1]);
    expect(() => publish(role("lightbulb-dev-owner-bedrock-org_2"))).toThrow();
  });

  test("a session frames channel admits a member while the session is shared, and no one else", () => {
    const { onSubscribe, parsePath } = handlers();
    const SESSION = "eeeeeeee-eeee-5eee-8eee-eeeeeeeeeeee";
    const frames = (rows: unknown[]) => ({ ...ctx(rows), info: { channel: { path: `/ws/${WORKSPACE}/session-frames/${SESSION}` } } });
    expect(onSubscribe.request(frames([])).tables.t.keys).toEqual([
      { pk: `WORKSPACE#${WORKSPACE}`, sk: "META" },
      { pk: `WORKSPACE#${WORKSPACE}`, sk: "MEMBER#user_1" },
      { pk: `WORKSPACE#${WORKSPACE}`, sk: `SESSION#${SESSION}` },
    ]);
    const shared = { ownerId: "org_1" };
    expect(() => onSubscribe.response(frames([meta, { active: true }, shared]))).not.toThrow();
    expect(() => onSubscribe.response(frames([meta, null, shared]))).toThrow(); // not a member
    expect(() => onSubscribe.response(frames([meta, { active: true }, null]))).toThrow(); // not shared
    for (const bad of [`/ws/${WORKSPACE}/session-frames`, `/ws/${WORKSPACE}/session-frames/${SESSION}/x`, `/ws/${WORKSPACE}/session-frames/nope`]) {
      expect(() => parsePath({ info: { channel: { path: bad } } })).toThrow();
    }
  });
});

describe("ApiStack — messaging projector permissions and alarms", () => {
  test("projector retains bounded canonical live delivery without search queue authority", () => {
    API_TEMPLATE.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "lightbulb-dev-messaging-projector",
      Runtime: "python3.13",
      Timeout: 30,
      MemorySize: 1769, // one full vCPU: signing and fan-out are CPU-bound (#3901)
      Environment: {
        Variables: Match.objectLike({
          MESSAGING_TABLE: Match.anyValue(),
          USERS_TABLE: "lightbulb-dev-users",
          APPSYNC_EVENTS_HTTP_ENDPOINT: Match.anyValue(),
          APPSYNC_EVENTS_REGION: "us-east-1",
        }),
      },
    });
    const projector = Object.values(
      API_TEMPLATE.findResources("AWS::Lambda::Function"),
    ).find(
      (resource: any) =>
        resource.Properties?.FunctionName ===
        "lightbulb-dev-messaging-projector",
    ) as any;
    const roleId = projector.Properties.Role["Fn::GetAtt"][0];
    const statements = statementsForRole(API_TEMPLATE, roleId);
    const granted = new Set(statements.flatMap(actions));
    expect(granted.has("dynamodb:GetItem")).toBe(true);
    expect(granted.has("s3:PutObject")).toBe(true);
    expect(granted.has("kms:Decrypt")).toBe(true);
    expect([...granted].some((action) => action.startsWith("kms:GenerateDataKey"))).toBe(true);
    expect(granted.has("appsync:EventPublish")).toBe(true);
    const sqsActions = [...granted].filter((action) => action.startsWith("sqs:"));
    for (const a of sqsActions) {
      expect(["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]).toContain(a);
    }
    const sqsResources = JSON.stringify(
      statements.filter((st) => actions(st).some((a) => a.startsWith("sqs:"))).map((st) => st.Resource),
    );
    expect(sqsActions).toEqual([]);
    expect(sqsResources).not.toContain("MessageSearch");
    expect(sqsResources).not.toContain("OpenClawWork");
    const publishResources = statements
      .filter((statement) => actions(statement).includes("appsync:EventPublish"))
      .flatMap((statement) =>
        Array.isArray(statement.Resource)
          ? statement.Resource
          : [statement.Resource],
      );
    expect(JSON.stringify(publishResources)).toContain("channelNamespace/ws");
    expect(JSON.stringify(publishResources)).not.toContain("channelNamespace/rooms");
    // Its row-creating writes are the Activity pointer and the away-email check, fenced to their partitions.
    const puts = statements.filter((statement) => actions(statement).includes("dynamodb:PutItem"));
    expect(puts).toHaveLength(1);
    expect(actions(puts[0])).toEqual(["dynamodb:PutItem"]);
    const leading = ["ACTIVITY#*", "AWAY#*"];
    expect(puts[0].Condition).toEqual({ "ForAllValues:StringLike": { "dynamodb:LeadingKeys": leading } });
    // Every partition the projector puts must be granted: a denied put fails the record before its
    // room event and push (AWAY# was missed on 2026-10-14 and silenced every DM and mention).
    const source = fs.readFileSync(path.join(__dirname, "../lambda/messaging-projector/index.py"), "utf8");
    const written = [...source.matchAll(/"pk": f"([A-Z_]+)#/g)].map((m) => `${m[1]}#*`);
    expect(written.length).toBeGreaterThan(0);
    for (const prefix of written) expect(leading).toContain(prefix);
    for (const forbidden of ["dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:TransactWriteItems"]) {
      expect(granted.has(forbidden)).toBe(false);
    }
  });

  test("Pipe state monitor polls the one Pipe every minute", () => {
    API_TEMPLATE.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "lightbulb-dev-openclaw-work-pipe-state",
      Runtime: "python3.13",
      Timeout: 10,
      Environment: {
        Variables: Match.objectLike({
          PIPE_NAME: "lightbulb-dev-openclaw-work",
        }),
      },
      Code: Match.objectLike({
        ZipFile: Match.stringLikeRegexp("describe_pipe"),
      }),
    });
    API_TEMPLATE.hasResourceProperties("AWS::Events::Rule", {
      ScheduleExpression: "rate(1 minute)",
      State: "ENABLED",
      Targets: Match.arrayWith([
        Match.objectLike({ Arn: Match.anyValue() }),
      ]),
    });

    const monitor = Object.values(
      API_TEMPLATE.findResources("AWS::Lambda::Function"),
    ).find(
      (resource: any) =>
        resource.Properties?.FunctionName ===
        "lightbulb-dev-openclaw-work-pipe-state",
    ) as any;
    const roleId = monitor.Properties.Role["Fn::GetAtt"][0];
    const statements = statementsForRole(API_TEMPLATE, roleId);
    const describe = statements.find((statement) =>
      actions(statement).includes("pipes:DescribePipe"),
    );
    expect(describe).toBeDefined();
    expect(JSON.stringify(describe.Resource)).toContain("OpenClawWorkPipe");
  });

  test("delivery alarms page the existing environment topic", () => {
    const alarms = Object.values(
      API_TEMPLATE.findResources("AWS::CloudWatch::Alarm"),
    ) as any[];
    const names = alarms.map((alarm) => alarm.Properties.AlarmName);
    // Named explicitly, not derived from `names`: filtering the alarm list by
    // the list of its own names is a no-op, so this used to assert that EVERY
    // alarm in the stack pages — which quietly became wrong the moment a
    // warn-tier alarm was added.
    const pagingAlarms = [
      "lightbulb-dev-openclaw-work-dlq-visible",
      "lightbulb-dev-openclaw-work-oldest-age",
      "lightbulb-dev-openclaw-work-pipe-dlq-visible",
      "lightbulb-dev-openclaw-work-pipe-failures",
      "lightbulb-dev-openclaw-work-pipe-stopped",
      "lightbulb-dev-messaging-projector-errors",
      "lightbulb-dev-messaging-projector-failures-stored",
      "lightbulb-dev-messaging-projector-s3-failures",
      "lightbulb-dev-messaging-projector-iterator-age",
    ];
    expect(names).toEqual(expect.arrayContaining(pagingAlarms));
    for (const alarm of alarms.filter((item) =>
      pagingAlarms.includes(item.Properties.AlarmName),
    )) {
      expect(alarm.Properties.AlarmActions).toEqual([
        expect.stringContaining("lightbulb-dev-alerts-page"),
      ]);
    }
    const oldest = alarms.find(
      (alarm) => alarm.Properties.AlarmName === "lightbulb-dev-openclaw-work-oldest-age",
    );
    const iterator = alarms.find(
      (alarm) => alarm.Properties.AlarmName === "lightbulb-dev-messaging-projector-iterator-age",
    );
    const stopped = alarms.find(
      (alarm) => alarm.Properties.AlarmName === "lightbulb-dev-openclaw-work-pipe-stopped",
    );
    expect(oldest.Properties.Threshold).toBe(300);
    expect(iterator.Properties.Threshold).toBe(300000);
    expect(stopped.Properties).toEqual(
      expect.objectContaining({
        Namespace: "LightbulbAI/Messaging",
        MetricName: "PipeNotRunning",
        Period: 300,
        Threshold: 1,
        EvaluationPeriods: 1,
        TreatMissingData: "breaching",
      }),
    );
  });

  // The dashboard names these three by LITERAL, so the observability stack
  // takes no CloudFormation Export on them (an export cannot be changed while
  // anything imports it). A literal is only safe while it matches the resource
  // it names, and nothing else would notice if the two drifted apart.
  test("the names the dashboard imports match the resources they name", () => {
    const api = buildApiStack();
    const template = Template.fromStack(api);

    const queueNames = Object.values(
      template.findResources("AWS::SQS::Queue"),
    ).map((q: any) => q.Properties.QueueName);
    expect(queueNames).toContain(api.openClawWorkQueueName);
    expect(queueNames).toContain(api.openClawWorkDlqName);

    const functionNames = Object.values(
      template.findResources("AWS::Lambda::Function"),
    ).map((f: any) => f.Properties.FunctionName);
    expect(functionNames).toContain(api.messagingProjectorFunctionName);
  });

  // AppSync is the whole browser/mobile delivery leg and had no alarm at all:
  // subscriptions could be refused and events dropped while every queue and
  // projector alarm stayed green. EventAPIId (not the GraphQLAPIId every
  // AppSync example uses) is the load-bearing detail — the wrong dimension
  // would never receive a datapoint.
  test("AppSync Event API server errors page and delivery losses warn", () => {
    const byName = Object.fromEntries(
      (Object.values(
        API_TEMPLATE.findResources("AWS::CloudWatch::Alarm"),
      ) as any[]).map((alarm) => [alarm.Properties.AlarmName, alarm.Properties]),
    );

    const tiers: [string, string, string][] = [
      ["subscribeservererror", "SubscribeServerError", "page"],
      ["connectservererror", "ConnectServerError", "page"],
      ["broadcasteventservererror", "BroadcastEventServerError", "page"],
      ["broadcastdroppedevents", "BroadcastDroppedEvents", "warn"],
      ["failedevents", "FailedEvents", "warn"],
      ["5xxerror", "5XXError", "warn"],
    ];
    for (const [suffix, metricName, tier] of tiers) {
      const alarm = byName[`lightbulb-dev-messaging-appsync-${suffix}`];
      expect(alarm).toBeDefined();
      expect(alarm.Namespace).toBe("AWS/AppSync");
      expect(alarm.MetricName).toBe(metricName);
      expect(alarm.Threshold).toBe(0);
      expect(alarm.Period).toBe(300);
      expect(alarm.Dimensions.map((d: any) => d.Name)).toEqual(["EventAPIId"]);
      expect(alarm.AlarmActions).toEqual([
        expect.stringContaining(`lightbulb-dev-alerts-${tier}`),
      ]);
    }
  });
});

describe("ApiStack — retired advanced search", () => {
  test.each(["dev", "prod"] as const)("%s keeps canonical messaging without an indexer or search network", (env) => {
    const template = env === "dev" ? API_TEMPLATE : synthApiStack(env);
    const resources = template.toJSON().Resources;
    expect(Object.values(resources).some((r: any) => r.Type.startsWith("AWS::OpenSearchServerless::"))).toBe(false);
    expect(Object.keys(resources).filter((id) => id.startsWith("MessageSearch"))).toEqual([]);
    expect(JSON.stringify(resources)).not.toContain("SEARCH_INDEX_QUEUE_URL");
    expect(JSON.stringify(resources)).not.toContain("SEARCH_INDEX_BATCH_QUEUE_URL");
    template.hasResourceProperties("AWS::Lambda::Function", { FunctionName: `lightbulb-${env}-messaging-projector` });
    expect(Object.values(resources).some((r: any) => r.Type === "AWS::DynamoDB::Table")).toBe(true);
  });
});

describe("ApiStack — super-agent tasks table (contracts-v2 §3, dev + prod)", () => {
  const SCHEMA = JSON.parse(
    fs.readFileSync(
      path.join(__dirname, "../../../packages/shared/fixtures/super-agent/tasks-schema.json"),
      "utf8",
    ),
  ) as { gsis: { name: string; pk: string; sk: string }[] };

  const tasksTable = (template: Template) =>
    Object.values(template.findResources("AWS::DynamoDB::Table")).filter((r: any) =>
      String(r.Properties?.TableName ?? "").endsWith("-tasks"),
    ) as any[];

  test("tasks table exists in dev with pk/sk, PITR, CMK, RETAIN", () => {
    const tables = tasksTable(API_TEMPLATE);
    expect(tables).toHaveLength(1);
    const [t] = tables;
    expect(t.Properties.TableName).toBe("lightbulb-dev-tasks");
    expect(t.Properties.KeySchema).toEqual([
      { AttributeName: "pk", KeyType: "HASH" },
      { AttributeName: "sk", KeyType: "RANGE" },
    ]);
    expect(t.Properties.BillingMode).toBe("PAY_PER_REQUEST");
    expect(t.Properties.PointInTimeRecoverySpecification.PointInTimeRecoveryEnabled).toBe(true);
    expect(t.Properties.SSESpecification.KMSMasterKeyId).toBeDefined();
    expect(t.DeletionPolicy).toBe("Retain");
    expect(t.UpdateReplacePolicy).toBe("Retain");
  });

  test("tasks table GSIs match tasks-schema.json", () => {
    const [t] = tasksTable(API_TEMPLATE);
    const gsis = t.Properties.GlobalSecondaryIndexes.map((g: any) => ({
      name: g.IndexName,
      pk: g.KeySchema.find((k: any) => k.KeyType === "HASH").AttributeName,
      sk: g.KeySchema.find((k: any) => k.KeyType === "RANGE").AttributeName,
      projection: g.Projection.ProjectionType,
    }));
    expect(gsis).toEqual(SCHEMA.gsis.map((g) => ({ ...g, projection: "ALL" })));
    expect(SCHEMA.gsis.map((g) => g.name)).toEqual(["by-status", "ws-meta"]);
  });

  test("prod synth has the tasks table", () => {
    expect(tasksTable(synthApiStack("prod"))).toHaveLength(1);
  });

  const projectorSources = (template: Template) =>
    Object.values(template.findResources("AWS::Lambda::EventSourceMapping")).filter(
      (m: any) => m.Properties.FunctionName?.Ref?.startsWith("MessagingProjector"),
    ) as any[];

  test("tasks table has stream and TTL and projector source in dev", () => {
    const [t] = tasksTable(API_TEMPLATE);
    expect(t.Properties.StreamSpecification).toEqual({ StreamViewType: "NEW_AND_OLD_IMAGES" });
    expect(t.Properties.TimeToLiveSpecification).toEqual({ AttributeName: "expiresAt", Enabled: true });
    const tasksId = Object.entries(API_TEMPLATE.findResources("AWS::DynamoDB::Table")).find(
      ([, r]: [string, any]) => r.Properties.TableName === "lightbulb-dev-tasks",
    )![0];
    const sources = projectorSources(API_TEMPLATE).map((m) => m.Properties.EventSourceArn["Fn::GetAtt"][0]);
    expect(sources).toEqual(expect.arrayContaining([tasksId, expect.stringMatching(/^MessagingTable/)]));
    expect(sources).toHaveLength(2);
  });

  test("ws handler admits a member to /ws/{ws}/tasks in dev", () => {
    const WS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const code = wsHandlerCode("t", "dev").replace(/^import .*;\n/m, "").replace(/^export /gm, "");
    const util = { matches: (re: string, v: string) => new RegExp(re).test(v), unauthorized: () => { throw 0; }, dynamodb: { toMapValues: (v: unknown) => v } };
    const { onSubscribe, parsePath } = new Function("util", `${code}\nreturn { onSubscribe, parsePath };`)(util);
    const ctx = (rows: unknown[]) => ({ info: { channel: { path: `/ws/${WS}/tasks` } }, identity: { sub: "user_1" }, result: { data: { t: rows } } });
    expect(onSubscribe.request(ctx([])).tables.t.keys).toEqual([
      { pk: `WORKSPACE#${WS}`, sk: "META" },
      { pk: `WORKSPACE#${WS}`, sk: "MEMBER#user_1" },
    ]);
    expect(() => onSubscribe.response(ctx([{ workspaceId: WS }, { active: true }]))).not.toThrow();
    expect(() => onSubscribe.response(ctx([{ workspaceId: WS }, null]))).toThrow();
    expect(() => parsePath({ info: { channel: { path: `/ws/${WS}/tasks/x` } } })).toThrow();
  });

  test("prod synth carries the tasks channel and projector source", () => {
    expect(wsHandlerCode("t", "prod")).toMatch(/tasks/);
    expect(projectorSources(synthApiStack("prod"))).toHaveLength(2);
  });
});
