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
  public readonly paperclipAdminCredentials: secretsmanager.Secret;

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
