import * as cdk from "aws-cdk-lib";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as ses from "aws-cdk-lib/aws-ses";
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
      description: `isol8 ${envName} agent-mail SES events → backend`,
    });
    const destination = new events.ApiDestination(this, "BackendSesEvents", {
      connection,
      endpoint: `https://${apiDomain}/api/v1/webhooks/ses-events`,
      httpMethod: events.HttpMethod.POST,
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
      targets: [new targets.ApiDestination(destination)],
    });

    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", envName);
  }
}
