import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as cr from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";

export interface AuthSecrets {
  clerkIssuer: secretsmanager.ISecret;
  clerkSecretKey: secretsmanager.ISecret;
  clerkWebhookSecret: secretsmanager.ISecret;
  stripeSecretKey: secretsmanager.ISecret;
  stripeWebhookSecret: secretsmanager.ISecret;
  encryptionKey: secretsmanager.ISecret;
  posthogProjectApiKey: secretsmanager.ISecret;
}

export interface AuthStackProps extends cdk.StackProps {
  environment: string;
  /** Optional initial secret values (for local dev — production sets these manually). */
  secretValues?: Record<string, string>;
}

export class AuthStack extends cdk.Stack {
  public readonly secrets: AuthSecrets;
  public readonly kmsKey: kms.Key;
  public readonly paperclipBetterAuthSecret: secretsmanager.Secret;
  public readonly paperclipServiceTokenKey: secretsmanager.Secret;
  public readonly platformOpsTokenKey: secretsmanager.Secret;
  public readonly paperclipEventsWebhookSecret: secretsmanager.Secret;
  public readonly wsInternalSecret: secretsmanager.Secret;
  public readonly paperclipAdminCredentials: secretsmanager.Secret;
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
      description: `Isol8 ${env} general encryption key (EBS, EFS, DynamoDB)`,
      alias: `isol8-${env}-general`,
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
      return new secretsmanager.Secret(this, logicalId, {
        secretName: `isol8/${env}/${secretName}`,
        description: description ?? `Isol8 ${env} ${secretName}`,
        encryptionKey: this.kmsKey,
        ...(initialValue
          ? { secretStringValue: cdk.SecretValue.unsafePlainText(initialValue) }
          : generateConfig
            ? { generateSecretString: generateConfig }
            : {}),
      });
    };

    const encryptionKey = createSecret("EncryptionKey", "encryption_key");

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
      encryptionKey,
      // Same createSecret pattern as Clerk/Stripe — CDK generates a random
      // placeholder on first create (CFN emits only GenerateSecretString).
      // Operator immediately overrides via `aws secretsmanager update-secret`
      // with the real phx_ key from PostHog. Harmless until then because
      // posthog_admin.py also gates on POSTHOG_PROJECT_ID being set, and
      // that defaults to empty in service-stack's environment block.
      posthogProjectApiKey: createSecret("PosthogProjectApiKey", "posthog_project_api_key"),
    };

    // Paperclip secrets (Task 3 — Paperclip rebuild). These do not flow through
    // the AuthSecrets struct because they are consumed only by the
    // service-stack's Paperclip-specific wiring + Lambda authorizer (Task 4+).
    this.paperclipBetterAuthSecret = createSecret(
      "PaperclipBetterAuthSecret",
      "paperclip_better_auth_secret",
      { passwordLength: 64, excludePunctuation: true },
      "Required by Paperclip server for cookie/session signing; not consumed by Isol8 backend.",
    );

    this.paperclipServiceTokenKey = createSecret(
      "PaperclipServiceTokenKey",
      "paperclip_service_token_key",
      { passwordLength: 64, excludePunctuation: true },
      "Symmetric secret for signing/verifying OpenClaw service-token JWTs (used by paperclip_provisioning + Lambda Authorizer)",
    );

    this.platformOpsTokenKey = createSecret(
      "PlatformOpsTokenKey",
      "platform_ops_token_key",
      { passwordLength: 64, excludePunctuation: true },
      "Dedicated symmetric secret for signing/verifying platform_ops tokens (owner-scoped ops API).",
    );

    // Shared bearer secret between the isol8.notifications Paperclip plugin
    // and the backend's /webhooks/paperclip-events receiver. Generated here;
    // the backend reads it as an env secret (service-stack), and the
    // Paperclip task receives the same value as PAPERCLIP_ISOL8_NOTIFY_TOKEN
    // (paperclip-stack) for the plugin worker's env passthrough.
    this.paperclipEventsWebhookSecret = createSecret(
      "PaperclipEventsWebhookSecret",
      "paperclip_events_webhook_secret",
      { passwordLength: 64, excludePunctuation: true },
      "Bearer secret the isol8.notifications Paperclip plugin sends to the backend paperclip-events webhook.",
    );

    // Shared secret that authenticates the CALLER of the backend /api/v1/ws/*
    // endpoints. The WebSocket Lambdas (ws-connect/disconnect/message) attach it
    // as a header; the public HTTP API Gateway $default route — which proxies to
    // the same ALB with no authorizer — cannot. Without it, anyone could POST to
    // /api/v1/ws/connect with a forged x-user-id and pollute/evict the
    // connection registry. Same CMK + createSecret shape as the service-token
    // key; read by api-stack (the Lambdas) + service-stack (the backend). Safe to
    // rotate — both sides read it live, never cache across the value change.
    this.wsInternalSecret = createSecret(
      "WsInternalSecret",
      "ws_internal_secret",
      { passwordLength: 64, excludePunctuation: true },
      "Shared secret the WebSocket Lambdas attach so the backend /ws/* endpoints can distinguish them from forged public-gateway callers.",
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
    // only) presents as the X-Isol8-SES-Secret header on its POST
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

    // Bootstrap admin Better Auth credentials. CDK creates the secret
    // with a placeholder; the operator populates it via
    // apps/backend/scripts/bootstrap_paperclip_admin.py (run via
    // `aws ecs execute-command` against the backend task — see the
    // script's docstring for the runbook). Backend reads it at first
    // provisioning call via core/services/paperclip_admin_session.py.
    // Encrypted with the same CMK as other auth secrets so the existing
    // KmsDecryptForSecrets policy on the backend task role covers it.
    //
    // The email written into this secret is whatever ADMIN_EMAIL the
    // bootstrap script holds at run time — currently
    // ``admin+paperclip@isol8.co`` (a plus-addressed alias of
    // admin@isol8.co, kept distinct so the bootstrap admin's
    // instance_admin role doesn't leak into admin@isol8.co's regular
    // Isol8 user identity). Legacy envs (dev) carry the bare
    // ``admin@isol8.co`` from before the rename — safe there only
    // because no dev user has signed up with that email.
    this.paperclipAdminCredentials = createSecret(
      "PaperclipAdminCredentials",
      "paperclip_admin_credentials",
      // Placeholder string — bootstrap script overwrites with
      // {"email": "<ADMIN_EMAIL>", "password": "<32-byte-random>"}.
      { passwordLength: 16, excludePunctuation: true },
      "Paperclip bootstrap admin Better Auth credentials (JSON: {email,password}). Populated by bootstrap_paperclip_admin.py — placeholder until then.",
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
      "Stable password for the gog CLI file-keyring backend (Google OAuth keyring on EFS). Never rotate — rotation orphans every existing keyring.",
    );

    // CDK's default GenerateSecretString produces a hex-ish placeholder that
    // is NOT a valid Fernet key (Fernet wants 32 random bytes encoded as
    // 44-char URL-safe base64). Bootstrap a real Fernet key on first deploy
    // via a Lambda-backed Custom Resource. Idempotent: if the current value
    // already decodes to 32 bytes via urlsafe_b64decode, the Lambda no-ops.
    this.bootstrapFernetKey(encryptionKey);
  }

  private bootstrapFernetKey(secret: secretsmanager.Secret): void {
    const handler = new lambda.Function(this, "FernetKeyBootstrapFn", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(30),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: lambda.Code.fromInline(`
import base64
import secrets as py_secrets

import boto3

sm = boto3.client("secretsmanager")

def _is_valid_fernet(value):
    if not isinstance(value, str) or len(value) != 44:
        return False
    try:
        return len(base64.urlsafe_b64decode(value)) == 32
    except Exception:
        return False

def handler(event, context):
    request_type = event["RequestType"]
    secret_id = event["ResourceProperties"]["SecretId"]

    if request_type == "Delete":
        return {"PhysicalResourceId": secret_id}

    try:
        current = sm.get_secret_value(SecretId=secret_id).get("SecretString", "")
    except sm.exceptions.ResourceNotFoundException:
        current = ""

    if not _is_valid_fernet(current):
        new_key = base64.urlsafe_b64encode(py_secrets.token_bytes(32)).decode()
        sm.put_secret_value(SecretId=secret_id, SecretString=new_key)
        rotated = True
    else:
        rotated = False

    return {"PhysicalResourceId": secret_id, "Data": {"Rotated": str(rotated)}}
`),
    });

    handler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
        resources: [secret.secretArn],
      }),
    );
    secret.encryptionKey?.grantEncryptDecrypt(handler);

    const provider = new cr.Provider(this, "FernetKeyBootstrapProvider", {
      onEventHandler: handler,
    });

    const resource = new cdk.CustomResource(this, "FernetKeyBootstrap", {
      serviceToken: provider.serviceToken,
      resourceType: "Custom::FernetKey",
      properties: {
        SecretId: secret.secretName,
        // Bump this on intentional rotation; the handler is also called on
        // every stack update and is a no-op when the value is already valid.
        Version: "1",
      },
    });
    resource.node.addDependency(secret);
  }
}
