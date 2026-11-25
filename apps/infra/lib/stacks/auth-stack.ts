import * as cdk from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

export interface AuthSecrets {
  clerkIssuer: secretsmanager.ISecret;
  clerkSecretKey: secretsmanager.ISecret;
  clerkWebhookSecret: secretsmanager.ISecret;
  stripeSecretKey: secretsmanager.ISecret;
  stripeWebhookSecret: secretsmanager.ISecret;
  posthogProjectApiKey: secretsmanager.ISecret;
  openaiApiKey: secretsmanager.ISecret;
}

export interface AuthStackProps extends cdk.StackProps {
  environment: string;
}

export class AuthStack extends cdk.Stack {
  public readonly secrets: AuthSecrets;
  public readonly kmsKey: kms.Key;
  public readonly paperclipServiceTokenKey: secretsmanager.Secret;
  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);

    const env = props.environment;

    // KMS key for general encryption (EBS, EFS, DynamoDB)
    this.kmsKey = new kms.Key(this, "GeneralEncryptionKey", {
      enableKeyRotation: true,
      description: `Lightbulb ${env} general encryption key (EBS, EFS, DynamoDB)`,
      // Renaming an alias replaces the AWS::KMS::Alias resource ONLY — CFN
      // creates the new alias, then deletes the old, and the KEY (with every
      // grant and every ciphertext ever written under it) is untouched. Nothing
      // references this alias by name; every consumer takes the key ARN.
      alias: `lightbulb-${env}-general`,
    });

    // Helper to create a CDK-managed secret. If generateConfig is provided, CDK
    // generates a random string on first create; otherwise the operator sets the value.
    const createSecret = (
      logicalId: string,
      secretName: string,
      generateConfig?: secretsmanager.SecretStringGenerator,
      description?: string,
    ): secretsmanager.Secret => {
      const make = (id: string, prefix: string) =>
        new secretsmanager.Secret(this, id, {
          secretName: `${prefix}/${env}/${secretName}`,
          description: description ?? `Lightbulb ${env} ${secretName}`,
          encryptionKey: this.kmsKey,
          ...(generateConfig ? { generateSecretString: generateConfig } : {}),
        });

      // The `Lightbulb` suffix on the logical id is load-bearing, not leftover
      // naming: it is what CloudFormation already knows these resources by. The
      // id was introduced when the secrets moved off the `isol8/` prefix (that
      // move had to be add-before-remove — `secretName` is a REPLACEMENT
      // property, so an in-place rename destroys the secret and mints a new one
      // with a generated value). Renaming the id back would replace every secret
      // and lose every live value, which is the exact outcome the three-step
      // migration existed to avoid.
      return make(`${logicalId}Lightbulb`, "lightbulb");
    };

    this.secrets = {
      clerkIssuer: createSecret("ClerkIssuer", "clerk_issuer"),
      clerkSecretKey: createSecret("ClerkSecretKey", "clerk_secret_key"),
      // Svix signing secret for the Clerk webhook endpoint. CDK generates a
      // random placeholder; the operator overwrites it out-of-band with the
      // `whsec_…` shown on the endpoint's page in the Clerk dashboard (same
      // posture as stripeWebhookSecret). Until that happens the backend
      // REJECTS Clerk deliveries with 503 — _verify_svix_signature fails
      // closed in any deployed env — and svix retries them, so nothing is
      // lost as long as the real value lands within the retry window.
      clerkWebhookSecret: createSecret("ClerkWebhookSecret", "clerk_webhook_secret"),
      stripeSecretKey: createSecret("StripeSecretKey", "stripe_secret_key"),
      stripeWebhookSecret: createSecret("StripeWebhookSecret", "stripe_webhook_secret"),
      // Same createSecret pattern as Clerk/Stripe — CDK generates a random
      // placeholder on first create (CFN emits only GenerateSecretString).
      // Operator immediately overrides via `aws secretsmanager update-secret`
      // with the real phx_ key from PostHog. Harmless until then because
      // posthog_admin.py also gates on POSTHOG_PROJECT_ID being set, and
      // that defaults to empty in service-stack's environment block.
      posthogProjectApiKey: createSecret("PosthogProjectApiKey", "posthog_project_api_key"),
      // The CDK-generated placeholder is intentionally not a valid key: voice stays off until the owner sets a real sk- key.
      openaiApiKey: createSecret("OpenaiApiKey", "openai_api_key"),
    };

    // Paperclip-named but generic: the backend signs service tokens, share links,
    // Needs-you and digest links with this key (core/services/service_token.py).
    this.paperclipServiceTokenKey = createSecret(
      "PaperclipServiceTokenKey",
      "paperclip_service_token_key",
      { passwordLength: 64, excludePunctuation: true },
      "Symmetric secret for signing/verifying OpenClaw service-token JWTs (used by paperclip_provisioning + Lambda Authorizer)",
    );

    // ponytail: one-deploy pin. Spark does not use this secret, but the retired
    // lightbulb-prod-ses-inbound stack (not in app.ts, still deployed) imports its
    // export. Delete it and these lines once that stack is handled.
    const sesIngestSecret = createSecret(
      "SesIngestSecret",
      "ses_ingest_secret",
      { passwordLength: 64, excludePunctuation: true },
      "Shared secret the SES-ingest Lambda attaches so /webhooks/ses-inbound can distinguish it from a forged caller.",
    );
    this.exportValue(sesIngestSecret.secretArn);
  }
}
