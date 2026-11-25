import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";
import { ROOT_DOMAIN } from "../domains";

// =============================================================================
// ApiStack — HTTP API Gateway: api[-env] -> VPC Link v2 -> internal ALB -> Fargate
// =============================================================================

export interface ApiStackProps extends cdk.StackProps {
  environment: string;
  /** Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN. */
  rootDomain?: string;
  vpc: ec2.IVpc;
  certificate?: acm.ICertificate;
  hostedZone?: route53.IHostedZone;
  albHttpListenerArn: string;
  albSecurityGroup: ec2.ISecurityGroup;
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
        allowOrigins: isProd ? [frontendUrl, `https://www.${rootDomain}`] : [frontendUrl],
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
        subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
      }).subnetIds,
    });

    // --- HTTP Integration → ALB via VPC Link ---
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

      this.httpApiUrl = `https://${httpApiDomain}`;
    } else {
      this.httpApiUrl = `https://${httpApi.ref}.execute-api.${this.region}.amazonaws.com/${env}`;
    }

    // DynamoDB Gateway Endpoint — free, keeps DynamoDB traffic on the AWS network.
    // It belongs in the network stack, but moving it replaces the endpoint.
    new ec2.GatewayVpcEndpoint(this, "DynamoDbEndpoint", {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
      vpc: props.vpc,
    });

    // =========================================================================
    // API Gateway account-level CloudWatch role (ACCOUNT singleton)
    // =========================================================================
    // Access/execution log delivery for every API Gateway in the account —
    // including this stage's accessLogSettings above — silently delivers
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
    // CloudFormation Outputs
    // =========================================================================

    new cdk.CfnOutput(this, "HttpApiUrlOutput", {
      value: this.httpApiUrl,
      description: "HTTP API Gateway URL",
      exportName: `lightbulb-${env}-http-api-url`,
    });
  }
}
