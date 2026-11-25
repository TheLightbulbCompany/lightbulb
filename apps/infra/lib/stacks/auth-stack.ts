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
  /** Optional initial secret values (for local dev — production sets these manually). */
  secretValues?: Record<string, string>;
}

export class AuthStack extends cdk.Stack {
  public readonly secrets: AuthSecrets;
  public readonly kmsKey: kms.Key;
  public readonly paperclipServiceTokenKey: secretsmanager.Secret;
  public readonly wsInternalSecret: secretsmanager.Secret;
  public readonly networkProofKey: secretsmanager.Secret;
  public readonly gogKeyringPassword: secretsmanager.Secret;
  public readonly metronomeApiKey: secretsmanager.Secret;
  public readonly metronomeWebhookSecret: secretsmanager.Secret;
  public readonly sesIngestSecret: secretsmanager.Secret;
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

    // Helper to create a CDK-managed secret.
    // If secretValues is provided (local dev), use that value instead of random generation.
    // If generateConfig is provided, CDK will generate a random string per the config
    // on first create (ignored when an initialValue is seeded for local dev).
    // Optional description overrides the default `Isol8 ${env} ${secretName}` shape.
    const secretVals = props.secretValues ?? {};
    const createSecret = (
      logicalId: string,
      secretName: string,
      generateConfig?: secretsmanager.SecretStringGenerator,
      description?: string,
    ): secretsmanager.Secret => {
      const initialValue = secretVals[secretName];
      const make = (id: string, prefix: string) =>
        new secretsmanager.Secret(this, id, {
          secretName: `${prefix}/${env}/${secretName}`,
          description: description ?? `Lightbulb ${env} ${secretName}`,
          encryptionKey: this.kmsKey,
          ...(initialValue
            ? { secretStringValue: cdk.SecretValue.unsafePlainText(initialValue) }
            : generateConfig
              ? { generateSecretString: generateConfig }
              : {}),
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

    // Shared secret (header x-lightbulb-ws-internal) that authenticates internal
    // callers of backend routes the public HTTP API Gateway $default route — which
    // proxies to the same ALB with no authorizer — can also reach: the EventBridge
    // connection that posts provision status events (routers/container.py) and one
    // backend task forwarding a network op to another (routers/network_route.py).
    // Same CMK + createSecret shape as the service-token key; read by service-stack
    // (the backend and that connection, which needs a redeploy to pick up a rotation).
    this.wsInternalSecret = createSecret(
      "WsInternalSecret",
      "ws_internal_secret",
      { passwordLength: 64, excludePunctuation: true },
      "Shared secret the WebSocket Lambdas attach so the backend /ws/* endpoints can distinguish them from forged public-gateway callers.",
    );

    // Seeds the Ed25519 key that signs `lightbulb login` proofs (backend network_proof: the
    // SHA-256 of this string is the private key). Its PUBLIC half is baked into the engine and
    // handed to each runtime; rotating it signs every computer out (they run lightbulb login).
    this.networkProofKey = createSecret(
      "NetworkProofKey",
      "network_proof_key",
      { passwordLength: 64, excludePunctuation: true },
      "Seed of the Ed25519 key that signs lightbulb login proofs. Rotating it signs every computer out.",
    );

    // Metronome API key, shared by the backend's lifecycle + gating modules
    // (customer/contract provisioning, top-up funding, the low-balance
    // webhook's owner resolution, balance display) AND the meter Lambda's
    // direct usage ingest (BedrockMeterStack imports it by name —
    // fromSecretNameV2 — so renaming it breaks the meter). The api key is a
    // SANDBOX token in dev (Metronome is token-scoped: same base URL, the
    // token decides sandbox vs prod); prod gets a prod token after the sales
    // engagement.
    // CDK generates a random placeholder on first create; the operator
    // overwrites it out-of-band via `aws secretsmanager put-secret-value`
    // with the real Metronome token. Same CMK as the other auth secrets, so
    // the backend task role's KmsDecryptForSecrets + wildcard SecretsAccess
    // already cover read.
    this.metronomeApiKey = createSecret(
      "MetronomeApiKey",
      "metronome_api_key",
      { passwordLength: 48, excludePunctuation: true },
      "Metronome API bearer token (sandbox in dev, prod after sales). Overwritten out-of-band with the real token.",
    );
    this.metronomeWebhookSecret = createSecret(
      "MetronomeWebhookSecret",
      "metronome_webhook_secret",
      { passwordLength: 48, excludePunctuation: true },
      "HMAC key for verifying Metronome-Webhook-Signature. Overwritten out-of-band with the value from the Metronome dashboard webhook config.",
    );

    // Shared secret the SES-ingest Lambda (ses-inbound-stack.ts, prod-stage
    // only) presents as the X-Lightbulb-SES-Secret header on its POST
    // to /webhooks/ses-inbound; the backend compares it in constant time (see
    // routers/ses_inbound.py). Created unconditionally in every env — same
    // posture as wsInternalSecret — but only ever actually READ
    // by the prod SesInboundStack (the ingest Lambda posts to the fixed prod
    // backend hostname regardless of which env's containers minted the
    // address). Harmless placeholder in envs where the SES stack doesn't
    // exist. Same CMK as the other auth secrets.
    this.sesIngestSecret = createSecret(
      "SesIngestSecret",
      "ses_ingest_secret",
      { passwordLength: 64, excludePunctuation: true },
      "Shared secret the SES-ingest Lambda attaches so /webhooks/ses-inbound can distinguish it from a forged caller.",
    );

    // Password the gog CLI's file-keyring backend uses to encrypt/decrypt the
    // owner's Google OAuth keyring on EFS (GOG_KEYRING_BACKEND=file +
    // GOG_KEYRING_PASSWORD). The SAME value is injected into both the backend
    // service (which materializes the keyring by running gog) and every
    // per-user OpenClaw container (which reads it at runtime).
    //
    // CRITICAL: this value must be STABLE forever. If it ever changes, every
    // keyring already written to EFS becomes undecryptable and owners would
    // have to re-authorize Google. CDK generates the random string only on
    // first create (CFN emits just GenerateSecretString and never re-runs it
    // on stack updates), and we deliberately do NOT enable rotation. Punctuation
    // is excluded because the value is passed through the container environment,
    // where shell-special characters would be hazardous.
    this.gogKeyringPassword = createSecret(
      "GogKeyringPassword",
      "gog-keyring-password",
      { passwordLength: 64, excludePunctuation: true },
      "Stable password for the gog CLI file-keyring backend (Google OAuth keyring on EFS). Never rotate - rotation orphans every existing keyring.",
    );

    // ponytail: one-deploy pins. Spark uses none of these six secrets, but the service stack's
    // deployed template still imports them and deploys after this stack, so CloudFormation refuses
    // to drop the exports this time. ses_ingest_secret is also imported by lightbulb-prod-ses-inbound,
    // which is no longer deployed but still exists. Delete them (and these lines) in a follow-up
    // after one green deploy and after the hand cleanup of the retired stacks.
    for (const pinned of [
      this.wsInternalSecret,
      this.networkProofKey,
      this.metronomeApiKey,
      this.metronomeWebhookSecret,
      this.sesIngestSecret,
      this.gogKeyringPassword,
    ]) {
      this.exportValue(pinned.secretArn);
    }
  }
}
