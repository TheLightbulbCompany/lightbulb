import * as cdk from "aws-cdk-lib";
import { alertTopicArn } from "../table-names";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as ses from "aws-cdk-lib/aws-ses";
import * as sns from "aws-cdk-lib/aws-sns";
import * as sqs from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";

export interface AgentMailOutboundStackProps extends cdk.StackProps {
  /** Stage env name ("dev" | "prod"); names the configuration set. */
  envName: string;
  /** Public REST host, e.g. "api-dev.isol8.co" / "api.isol8.co". */
  apiDomain: string;
  /** By-name import of THIS env's ses_ingest_secret — the backend's /webhooks/ses-events checks it. */
  sesIngestSecretName: string;
}

/**
 * Outbound agent-mail plumbing, per env:
 *   configuration set (every agent send is stamped with it — core/services/ses_email.py)
 *     → COMPLAINT events → EventBridge default bus
 *     → rule filtered on this env's configuration-set tag
 *     → API destination → POST https://{apiDomain}/api/v1/webhooks/ses-events
 * Per-owner SES TENANTS are created by the backend at first send
 * (core/services/agent_mail_tenant.py) — dynamic per owner, so not declared here.
 */
export class AgentMailOutboundStack extends cdk.Stack {
  public readonly configurationSetName: string;

  constructor(scope: Construct, id: string, props: AgentMailOutboundStackProps) {
    super(scope, id, props);
    const { envName, apiDomain, sesIngestSecretName } = props;

    this.configurationSetName = `isol8-${envName}-agent-mail`;
    const configSet = new ses.CfnConfigurationSet(this, "AgentMailConfigSet", {
      name: this.configurationSetName,
    });

    const eventDestination = new ses.CfnConfigurationSetEventDestination(this, "ComplaintsToEventBridge", {
      configurationSetName: this.configurationSetName,
      eventDestination: {
        name: "complaints-eventbridge",
        enabled: true,
        matchingEventTypes: ["COMPLAINT"],
        eventBridgeDestination: {
          eventBusArn: `arn:aws:events:${this.region}:${this.account}:event-bus/default`,
        },
      },
    });
    eventDestination.addDependency(configSet);

    const connection = new events.Connection(this, "BackendConnection", {
      // Same shared secret the SES-ingest Lambda presents — the backend's
      // ses-events handler verifies it with the same compare.
      authorization: events.Authorization.apiKey(
        "X-Isol8-SES-Secret",
        cdk.SecretValue.secretsManager(sesIngestSecretName),
      ),
      description: `isol8 ${envName} agent-mail SES events -> backend`,
    });
    const destination = new events.ApiDestination(this, "BackendSesEvents", {
      connection,
      endpoint: `https://${apiDomain}/api/v1/webhooks/ses-events`,
      httpMethod: events.HttpMethod.POST,
    });
    // A complaint that never reaches the backend leaves the owner's outbound
    // ENABLED after a recipient marked their mail as spam — the exact state
    // that gets the sending domain blocked. EventBridge's default is to drop
    // the event once retries are exhausted, so it gets a DLQ.
    //
    // REPLAY: `aws sqs receive-message --queue-url <this queue>`, then re-POST
    // the message body (the EventBridge event) to
    // https://{apiDomain}/api/v1/webhooks/ses-events with the
    // X-Isol8-SES-Secret header. Replay is safe to repeat — the handler's
    // disable is an `update_settings` SET, not an increment.
    const dlq = new sqs.Queue(this, "ComplaintDlq", {
      queueName: `isol8-${envName}-agent-mail-complaint-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });

    new events.Rule(this, "SesComplaintRule", {
      description: `isol8 ${envName}: SES complaints on ${this.configurationSetName}`,
      eventPattern: {
        source: ["aws.ses"],
        detailType: ["Email Complaint Received"],
        // dev + prod share one account and one default bus — filter on the
        // configuration-set tag SES stamps on every event.
        detail: { mail: { tags: { "ses:configuration-set": [this.configurationSetName] } } },
      },
      targets: [
        new targets.ApiDestination(destination, {
          deadLetterQueue: dlq,
          // Well short of EventBridge's 185/24h default: a complaint is only
          // actionable while it is fresh, and holding it in the DLQ where an
          // alarm can see it beats retrying silently for a day.
          retryAttempts: 8,
          maxEventAge: cdk.Duration.hours(2),
        }),
      ],
    });

    // Topic imported by ARN convention (same as bedrock-meter-stack) —
    // ObservabilityStack creates it per env with this fixed name, and a real
    // prop would add a cross-stack dependency for one alarm action.
    const alarm = new cloudwatch.Alarm(this, "ComplaintDlqNotEmptyAlarm", {
      alarmName: `isol8-${envName}-agent-mail-complaint-dlq`,
      alarmDescription:
        "SES complaint events failed delivery to the backend ses-events webhook and are held for replay. Until they are replayed the affected owners' agent outbound is still ENABLED after a spam complaint. See the replay note in agent-mail-outbound-stack.ts.",
      metric: dlq.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(5),
        statistic: "Maximum",
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarm.addAlarmAction(
      new cloudwatchActions.SnsAction(
        sns.Topic.fromTopicArn(
          this,
          "AlertsWarn",
          alertTopicArn(this.region, this.account, envName, "warn"),
        ),
      ),
    );

    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", envName);
  }
}
