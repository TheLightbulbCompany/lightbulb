import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { ROOT_DOMAIN } from "../lib/domains";
import { AgentMailOutboundStack } from "../lib/stacks/agent-mail-outbound-stack";

function buildStack(environment: "dev" | "prod" = "dev"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const stack = new AgentMailOutboundStack(app, `AgentMailOutbound-${environment}`, {
    env,
    envName: environment,
    apiDomain:
      environment === "prod"
        ? `api.${ROOT_DOMAIN}`
        : `api-${environment}.${ROOT_DOMAIN}`,
    sesIngestSecretName: `isol8/${environment}/ses_ingest_secret`,
  });
  return Template.fromStack(stack);
}

describe("AgentMailOutboundStack", () => {
  const template = buildStack("dev");

  test("creates the SES configuration set every agent-mail send is stamped with", () => {
    template.resourceCountIs("AWS::SES::ConfigurationSet", 1);
    template.hasResourceProperties("AWS::SES::ConfigurationSet", {
      Name: "lightbulb-dev-agent-mail",
    });
  });

  test("wires COMPLAINT events to EventBridge on that configuration set", () => {
    template.resourceCountIs("AWS::SES::ConfigurationSetEventDestination", 1);
    template.hasResourceProperties("AWS::SES::ConfigurationSetEventDestination", {
      ConfigurationSetName: "lightbulb-dev-agent-mail",
      EventDestination: Match.objectLike({
        MatchingEventTypes: ["COMPLAINT"],
        EventBridgeDestination: Match.objectLike({
          EventBusArn: Match.anyValue(),
        }),
      }),
    });
  });

  test("creates the API destination pointed at this env's ses-events webhook", () => {
    template.resourceCountIs("AWS::Events::ApiDestination", 1);
    template.hasResourceProperties("AWS::Events::ApiDestination", {
      InvocationEndpoint: `https://api-dev.${ROOT_DOMAIN}/api/v1/webhooks/ses-events`,
      HttpMethod: "POST",
    });
  });

  test("creates an API-key connection presenting the shared ses_ingest_secret", () => {
    template.resourceCountIs("AWS::Events::Connection", 1);
    template.hasResourceProperties("AWS::Events::Connection", {
      AuthorizationType: "API_KEY",
      AuthParameters: Match.objectLike({
        ApiKeyAuthParameters: Match.objectLike({
          ApiKeyName: "X-Lightbulb-SES-Secret",
        }),
      }),
    });
  });

  test("rule filters on THIS env's configuration-set tag, not a bare aws.ses source match", () => {
    // dev + prod share one account and one default event bus, so an
    // unfiltered rule would fire the dev API destination for prod complaints
    // (and vice versa). The configuration-set tag is the only per-env signal
    // on the event.
    template.resourceCountIs("AWS::Events::Rule", 1);
    template.hasResourceProperties("AWS::Events::Rule", {
      EventPattern: {
        source: ["aws.ses"],
        "detail-type": ["Email Complaint Received"],
        detail: {
          mail: {
            tags: {
              "ses:configuration-set": ["lightbulb-dev-agent-mail"],
            },
          },
        },
      },
    });
  });

  test("the API-destination target has a DLQ and a bounded retry policy", () => {
    // Without these, a complaint EventBridge cannot deliver is dropped and
    // the owner's outbound stays ENABLED after a spam complaint.
    template.resourceCountIs("AWS::SQS::Queue", 1);
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "lightbulb-dev-agent-mail-complaint-dlq",
    });
    template.hasResourceProperties("AWS::Events::Rule", {
      Targets: Match.arrayWith([
        Match.objectLike({
          DeadLetterConfig: { Arn: Match.anyValue() },
          RetryPolicy: {
            MaximumRetryAttempts: 8,
            MaximumEventAgeInSeconds: 7200,
          },
        }),
      ]),
    });
  });

  test("alarms on the complaint DLQ being non-empty", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "lightbulb-dev-agent-mail-complaint-dlq",
      MetricName: "ApproximateNumberOfMessagesVisible",
      ComparisonOperator: "GreaterThanThreshold",
      Threshold: 0,
    });
  });

  test("prod env names its configuration set and endpoint distinctly from dev", () => {
    const prodTemplate = buildStack("prod");
    prodTemplate.hasResourceProperties("AWS::SES::ConfigurationSet", {
      Name: "lightbulb-prod-agent-mail",
    });
    prodTemplate.hasResourceProperties("AWS::Events::ApiDestination", {
      InvocationEndpoint: `https://api.${ROOT_DOMAIN}/api/v1/webhooks/ses-events`,
    });
  });
});
