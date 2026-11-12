import * as path from "path";
import * as fs from "fs";
import { execSync } from "child_process";
import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as ses from "aws-cdk-lib/aws-ses";
import * as sesActions from "aws-cdk-lib/aws-ses-actions";
import * as cr from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";
import { LEGACY_MAIL_DOMAIN, ROOT_DOMAIN } from "../domains";
import { logGroupPath } from "../table-names";

// =============================================================================
// SesInboundStack — SES inbound agent-mail receiving (agent-email SES chunk 2).
//
// PROD-STAGE-ONLY. This stack is only ever instantiated by lightbulb-stage.ts
// when `env === "prod"` (see lightbulb-stage.ts for the gate) — dev/local stages
// create NO live SES/DNS resources. Two independent reasons drive that
// gating:
//   1. SetActiveReceiptRuleSet is an ACCOUNT+REGION-scoped "one active rule
//      set" operation (like Bedrock's PutModelInvocationLoggingConfiguration
//      in bedrock-meter-stack.ts) — a second instantiation in the same
//      account/region would fight over which rule set is active, so this
//      stack must be a SINGLETON, never one-per-env.
//   2. The wildcard MX + SPF records and the domain identities are real DNS
//      + mail-routing changes against LIVE mail zones — not something a
//      routine dev-stage deploy should ever touch implicitly.
//
// Both env's `ses_ingest_secret` are created unconditionally by AuthStack
// (cheap, inert, matches the wsInternalSecret shape), but only
// the PROD secret is ever actually read here — the ingest Lambda posts to
// the prod backend (BACKEND_INGEST_URL is a fixed prod hostname) because
// AGENT_EMAIL_DOMAIN mail only ever resolves under the single production
// mail domain, regardless of which env's containers mint the addresses.
//
// TWO mail domains are published, not one. `mailDomain` is where new mail
// lives (`thelightbulb.company`); `legacyMailDomain` is the domain being
// retired (`isol8.co`). Every existing agent address in DynamoDB is still
// `{agent}@{slug}.isol8.co` and this PR does not migrate them, so the legacy
// records are published UNCHANGED alongside the new ones — repointing them
// would delete the `*.isol8.co` MX (killing inbound for every existing
// mailbox) and the apex TXT set (taking isol8.co's Google site-verification
// token with it). The legacy block, the legacy recipients on the catch-all
// rule, and the legacy identity ARN in service-stack.ts come out together in
// the retirement PR, after the address backfill.
// =============================================================================

/**
 * Google site-verification tokens (Workspace, Search Console), per mail domain.
 *
 * A Route53 TXT record is a SET keyed by (name, type) and the apex record
 * below is published with `deleteExisting`, so a token missing from that
 * values array is GONE from the zone — which un-verifies the domain in
 * Workspace. Keyed by domain so each apex carries its OWN token and can never
 * be handed the other domain's.
 */
const GOOGLE_SITE_VERIFICATION: Record<string, string[]> = {
  "thelightbulb.company": [
    // Google Workspace.
    "google-site-verification=v-MtfFMuBP2VYDP5sIB3wVXTQU4SjimPoryr5oX6mQg",
    // Search Console domain property (admin@thelightbulb.company).
    "google-site-verification=Clg4oJdtZ_0J7pJRFmfJfkKr0KE8C9CVze8I7AIbaWU",
    // Microsoft Entra custom-domain verification (publisher verification). Not
    // a Google token; it lives here because this array IS the apex TXT set.
    "MS=ms56087164",
  ],
  "isol8.co": [
    "google-site-verification=4m6AkWYnaePsQsIyDL4eJlPEddQg2RF1_Hxl7EFDHr8",
  ],
};

export interface SesInboundStackProps extends cdk.StackProps {
  /**
   * Stage env name — always "prod" in practice (this stack is only ever
   * instantiated in the prod stage). Named `envName` (not `env`) because
   * `cdk.StackProps` already reserves `env` for the account/region object.
   * Used only for resource naming/tagging + deriving the by-name secret
   * import, exactly like `BedrockMeterStackProps.envName`.
   */
  envName: string;
  /**
   * Name (NOT ISecret) of the shared secret the ingest Lambda presents to
   * POST /webhooks/ses-inbound, e.g. `isol8/prod/ses_ingest_secret`. Passed
   * by name — same cross-stack-cycle-avoidance posture as every other
   * by-name secret import in this codebase (see bedrock-meter-stack.ts's
   * Metronome key import).
   */
  sesIngestSecretName: string;
  /**
   * Base mail domain, e.g. "thelightbulb.company". Verified for BOTH receiving
   * and sending. Its Route53 zone is looked up BY NAME here rather than passed
   * in: mail no longer lives on the product domain, so DnsStack's zone (which
   * follows `rootDomain`) is the wrong one.
   */
  mailDomain: string;
  /**
   * Mail domain being retired, e.g. "isol8.co". Its records, identity and
   * receipt-rule recipients are published UNCHANGED so every pre-existing
   * `{agent}@{slug}.isol8.co` address keeps receiving and sending while the
   * DynamoDB address book still points at it. Set to "" (or to `mailDomain`)
   * once the backfill has run — that is the retirement step. Defaults to
   * LEGACY_MAIL_DOMAIN.
   */
  legacyMailDomain?: string;
  /**
   * Product root domain (see lib/domains.ts) — the ingest Lambda POSTs to
   * `api.{rootDomain}` / `api-dev.{rootDomain}`. Separate from `mailDomain`
   * because the backend does not have to live under the mail domain.
   * Defaults to ROOT_DOMAIN.
   */
  rootDomain?: string;

  /**
   * The DEV stage's product domain. Separate from `rootDomain` because this
   * stack is PROD-ONLY: `rootDomain` is prod's, and deriving the dev ingest URL
   * from it silently pointed dev inbound mail at prod's apex. Once dev and prod
   * are on different domains mid-migration that stops being the same string and
   * dev mail POSTs to a host that no longer serves it.
   */
  devRootDomain?: string;
  /**
   * Dev mail domain, e.g. "dev.isol8.co". When set, this stack also publishes
   * `*.{devMailDomain}` MX + SPF and the ingest Lambda routes recipients under
   * that suffix to the DEV backend instead of prod (see `_route_for`).
   *
   * It is configured HERE, in the prod stack, because dev cannot own an SES
   * receipt rule set — `SetActiveReceiptRuleSet` is account+region scoped.
   * Omit to keep every dev path inert.
   */
  devMailDomain?: string;
  /**
   * Name of the DEV `ses_ingest_secret`. Required whenever `devMailDomain` is
   * set — each backend verifies the shared secret against its OWN copy, so
   * presenting prod's secret to dev would 401.
   */
  devSesIngestSecretName?: string;
}

export class SesInboundStack extends cdk.Stack {
  public readonly rawBucket: s3.Bucket;
  public readonly attachmentsBucket: s3.Bucket;
  public readonly ingestFunction: lambda.Function;
  public readonly ruleSet: ses.ReceiptRuleSet;
  /** Attachments bucket name — threaded into ServiceStack's backend env. */
  public readonly attachmentsBucketName: string;
  /** Raw-mail bucket name — exposed for completeness/ops visibility. */
  public readonly rawBucketName: string;

  constructor(scope: Construct, id: string, props: SesInboundStackProps) {
    super(scope, id, props);

    const { envName, mailDomain, devMailDomain } = props;
    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    const devRootDomain = props.devRootDomain ?? rootDomain;
    const legacyMailDomain = props.legacyMailDomain ?? LEGACY_MAIL_DOMAIN;
    // Publishing the same records twice against one domain would collide on
    // every record name. Empty / equal-to-`mailDomain` is how the retirement
    // PR (and a unit-test synth) turns the legacy leg off.
    const publishLegacy = Boolean(legacyMailDomain) && legacyMailDomain !== mailDomain;

    if (devMailDomain && !props.devSesIngestSecretName) {
      // Fail at synth, not at the first dev email. Routing dev mail to the dev
      // backend while presenting prod's secret would 401 on every message, and
      // the only symptom would be silently undelivered mail.
      throw new Error(
        "SesInboundStack: devSesIngestSecretName is required when devMailDomain is set",
      );
    }

    // -------------------------------------------------------------------------
    // Everything DNS + identity for ONE mail domain. Called once per domain
    // (the live one, and the one being retired) — the two are identical in
    // shape and differ only in zone, apex token and dev subdomain, so a second
    // hand-written copy would just be a place for the two to drift apart.
    //
    // The `idPrefix` is load-bearing: the legacy leg passes "AgentMail", which
    // reproduces the construct ids this stack has always used, so its records
    // synthesize byte-identically and CloudFormation sees NO change to them.
    // -------------------------------------------------------------------------
    const publishMailDomain = (
      idPrefix: string,
      domain: string,
      devDomain: string | undefined,
    ) => {
      // Looked up by NAME, not passed in from DnsStack: `thelightbulb.company`
      // is not the product zone, and after the product-domain flip DnsStack's
      // zone is not even `isol8.co`. Each mail domain owns its own lookup, so
      // the DKIM CNAMEs below always land in the zone that actually serves the
      // domain being verified.
      const zone = route53.HostedZone.fromLookup(this, `${idPrefix}HostedZone`, {
        domainName: domain,
      });

      // -----------------------------------------------------------------------
      // Domain identity — verifies `domain` for BOTH receiving and sending.
      // `Identity.publicHostedZone` auto-creates the 3 DKIM CNAME records in
      // the zone (see DkimIdentity.easyDkim().bind() — no manual DNS work
      // needed). ONE identity covers every company subdomain: SES authorizes a
      // send from any address under a verified domain identity, including
      // `{agent}@{slug}.{domain}` (see
      // apps/backend/core/services/agent_mail_tenant.py:_identity_domain()).
      // No `mailFromDomain` override: outbound agent replies default to SES's
      // own amazonses.com bounce domain, which is fine for our use case and
      // avoids provisioning a second MX/SPF pair purely for bounces.
      // -----------------------------------------------------------------------
      new ses.EmailIdentity(this, `${idPrefix}DomainIdentity`, {
        identity: ses.Identity.publicHostedZone(zone),
      });

      // -----------------------------------------------------------------------
      // Wildcard MX — routes `*.{domain}` (every company subdomain,
      // `{slug}.{domain}`) to SES receiving. The apex MX is deliberately
      // untouched: it points at Google Workspace for the human mailboxes and
      // `support@`, and a leftmost wildcard never matches the apex itself, so
      // the two coexist without either being aware of the other.
      // -----------------------------------------------------------------------
      new route53.MxRecord(this, `${idPrefix}WildcardMx`, {
        zone,
        recordName: `*.${domain}`,
        values: [{ priority: 10, hostName: "inbound-smtp.us-east-1.amazonaws.com" }],
      });

      // Wildcard SPF — authorizes SES to send mail "from" any subdomain
      // address (friday@acme.{domain}, etc).
      new route53.TxtRecord(this, `${idPrefix}WildcardSpf`, {
        zone,
        recordName: `*.${domain}`,
        values: ["v=spf1 include:amazonses.com ~all"],
      });

      // -----------------------------------------------------------------------
      // APEX SPF. Without one, mail from the apex authenticates as `spf=none`
      // — and because Google Workspace signs outbound DKIM as
      // `<domain>.<date>.gappssmtp.com` until a domain key is published, DKIM
      // is not ALIGNED with `header.from=<domain>` either. DMARC needs one
      // aligned pass; with neither, every message from `admin@<domain>`
      // evaluates `dmarc=fail` — including an owner's own reply to their
      // agent, which the inbound anti-spoof gate then correctly drops. That
      // exact outage happened on isol8.co, and only `p=none` kept it from
      // bouncing everywhere.
      //
      // Note the wildcard above does NOT cover the apex (`*.example.com`
      // matches `a.example.com`, never `example.com`), which is why this looks
      // configured while being absent — the same trap documented for `_dmarc`
      // below.
      //
      // Every apex sender must be listed here, because publishing a record
      // moves unlisted senders from neutral to softfail:
      //   * `_spf.google.com` — Workspace user mail (admin@, support@, humans)
      //   * `amazonses.com`   — SES directly (EMAIL_FROM_WELCOME hello@,
      //                         EMAIL_FROM_TICKETS tickets@ — both wired from
      //                         service-stack.ts), and anything else sending
      //                         over SES infrastructure.
      // ~all (softfail) not -all: a hard fail would bounce any apex sender this
      // list has missed. Tighten only after the DMARC rua reports come back clean.
      //
      // SPF permits 10 DNS lookups; this costs ~5 (_spf.google.com ~4, SES 1).
      //
      // `deleteExisting` is REQUIRED, not optional tidiness. A TXT record set
      // already exists at the apex of BOTH domains (created outside CFN,
      // holding the google-site-verification token), and CloudFormation's
      // Route53 CREATE does NOT upsert — it fails outright:
      //
      //   Tried to create resource record set [name='isol8.co.', type='TXT']
      //   but it already exists
      //
      // which rolled the whole stack back once already. `deleteExisting`
      // removes the unmanaged set first so CFN can adopt the name.
      //
      // The google-site-verification string is therefore carried in this array
      // DELIBERATELY: a TXT record is a SET keyed by (name, type), and the
      // delete-then-create replaces it wholesale, so a value omitted here is
      // GONE from the zone — losing that token can unverify the domain in
      // Google Workspace. Any future apex TXT value must be appended to this
      // array, never declared as a second TxtRecord on the same name.
      // -----------------------------------------------------------------------
      new route53.TxtRecord(this, `${idPrefix}ApexSpf`, {
        zone,
        recordName: domain,
        deleteExisting: true,
        values: [
          "v=spf1 include:_spf.google.com include:amazonses.com ~all",
          ...(GOOGLE_SITE_VERIFICATION[domain] ?? []),
        ],
      });

      // -----------------------------------------------------------------------
      // DEV mail domain — `{slug}.dev.{domain}`.
      //
      // These records live in THIS (prod) stack on purpose. A second receipt
      // rule set is impossible — SetActiveReceiptRuleSet is account+region
      // scoped and this stack owns the active one — so dev cannot run its own
      // SES-inbound stack. It does not need to: SES matches a recipient
      // condition against the domain AND its subdomains, so the catch-all rule
      // for `{domain}` already catches `friday@acme.dev.{domain}`. Only DNS and
      // per-environment backend routing are missing, and both are here.
      //
      // A single-label wildcard does not nest: `*.{domain}` matches
      // `acme.{domain}` but NOT `acme.dev.{domain}`, hence the second pair.
      // -----------------------------------------------------------------------
      if (devDomain) {
        new route53.MxRecord(this, `${idPrefix}DevWildcardMx`, {
          zone,
          recordName: `*.${devDomain}`,
          values: [{ priority: 10, hostName: "inbound-smtp.us-east-1.amazonaws.com" }],
        });

        new route53.TxtRecord(this, `${idPrefix}DevWildcardSpf`, {
          zone,
          recordName: `*.${devDomain}`,
          values: ["v=spf1 include:amazonses.com ~all"],
        });
      }

      // -----------------------------------------------------------------------
      // DMARC policy at the ORGANIZATIONAL domain.
      //
      // This record is NOT optional decoration — without it the zone actively
      // LOOKS configured while having no policy at all. The wildcard SPF TXT
      // above matches `_dmarc.{domain}` (`_dmarc` is just another label under
      // the zone), so a receiver querying for DMARC gets back
      // `"v=spf1 include:amazonses.com ~all"`. That is not a DMARC record — it
      // does not start with `v=DMARC1` — so every receiver reads the domain as
      // having NO published policy. An explicit record is the only fix, since
      // an explicit name always beats a wildcard match.
      //
      // One record at the org domain covers every company subdomain too:
      // `_dmarc.acme.{domain}` also resolves to the wildcard SPF junk, and
      // RFC 7489 §6.6.3 says a query returning no VALID DMARC record falls
      // through to the organizational domain — landing here. (A wildcard can't
      // help anyway: DNS wildcards must be leftmost, and `_dmarc.*.{domain}`
      // is not a legal wildcard name.)
      //
      // `p=none` is deliberate and load-bearing. This policy governs ALL mail
      // on the domain — Google Workspace on the apex, Clerk, and SES — and
      // only SES alignment has been verified. Monitor-only publishes the
      // policy and turns on aggregate reporting without putting a single
      // existing mail flow at risk of quarantine/reject. Tighten to
      // quarantine/reject only after the rua reports show every sender aligned.
      //
      // Aggregate reports go to an address that ALREADY EXISTS on the apex
      // (Google Workspace). A same-domain `rua` needs no authorization record
      // at the receiving domain, and reusing a live mailbox means publishing
      // DMARC costs no new Workspace seat, alias, or admin-console change.
      // -----------------------------------------------------------------------
      new route53.TxtRecord(this, `${idPrefix}Dmarc`, {
        zone,
        recordName: `_dmarc.${domain}`,
        values: [`v=DMARC1; p=none; rua=mailto:admin@${domain}; fo=1`],
      });
    };

    // The live mail domain. `idPrefix` "PlatformMail" — everything it creates
    // is a pure CREATE against the new zone.
    publishMailDomain("PlatformMail", mailDomain, devMailDomain);

    // The domain being retired. "AgentMail" is the prefix this stack has
    // always used, so these synthesize EXACTLY as already deployed and
    // CloudFormation changes nothing about them — verified against the live
    // `isol8-prod-ses-inbound` template: zero removed resources, zero changed
    // record sets.
    //
    // The dev pair is gated on `devMailDomain` too, because it has always
    // existed only when dev mail is configured at all.
    const legacyDevMailDomain =
      publishLegacy && devMailDomain ? `dev.${legacyMailDomain}` : undefined;
    if (publishLegacy) {
      publishMailDomain("AgentMail", legacyMailDomain, legacyDevMailDomain);
    }

    // Deterministic literal names (not read off the live construct — see
    // note below) so cross-stage consumers never receive a CDK Token.
    const rawBucketName = `lightbulb-${envName}-agent-mail-raw`;
    const attachmentsBucketName = `lightbulb-${envName}-agent-mail-attachments`;

    // -------------------------------------------------------------------------
    // Raw-mail S3 bucket — the SES receipt rule's S3 action lands the full
    // MIME here; the ingest Lambda reads it back by `{prefix}{messageId}`.
    // Short-lived: the Lambda consumes each object within seconds of
    // delivery, so 7 days is purely a debugging window, not a system of
    // record (mirrors the bedrock-invocation log group's retention
    // reasoning in bedrock-meter-stack.ts).
    // -------------------------------------------------------------------------
    this.rawBucket = new s3.Bucket(this, "RawMailBucket", {
      bucketName: rawBucketName,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy:
        envName === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: envName !== "prod",
      lifecycleRules: [{ expiration: cdk.Duration.days(7) }],
    });

    // -------------------------------------------------------------------------
    // Attachments S3 bucket — the ingest Lambda copies each MIME attachment
    // here and mints a 24h presigned GET URL for the agent to fetch. Longer
    // retention than raw mail (30 days) since an agent may act on a message
    // well after the triggering delivery.
    // -------------------------------------------------------------------------
    this.attachmentsBucket = new s3.Bucket(this, "AttachmentsBucket", {
      bucketName: attachmentsBucketName,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy:
        envName === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: envName !== "prod",
      lifecycleRules: [{ expiration: cdk.Duration.days(30) }],
    });
    // NOTE: exposed as the LITERAL strings above, not `this.attachmentsBucket
    // .bucketName` / `this.rawBucket.bucketName`. Even with an explicit
    // `bucketName` prop, the L2 Bucket's `.bucketName` getter returns a CDK
    // Token (`Ref` to the underlying CfnBucket), not the plain string. Since
    // lightbulb-stage.ts threads `attachmentsBucketName` into ServiceStack (a
    // DIFFERENT stack), a Token there would force CDK to emit a real
    // CloudFormation Export/Fn::ImportValue pair across the stack boundary —
    // exactly the "export that can never be safely renamed/deleted" trap
    // this codebase deliberately avoids everywhere else (see the
    // DynamoDbOwnedTables by-name/wildcard-grant comment in
    // service-stack.ts, and CFN_TEMPLATE_URL for the same
    // literal-string-over-token posture). Because the name is
    // fully deterministic, the literal is exactly as correct as the token
    // and costs nothing.
    this.attachmentsBucketName = attachmentsBucketName;
    this.rawBucketName = rawBucketName;

    // The shared secret the ingest Lambda presents to the backend, imported
    // by NAME (not ISecret) — same no-cross-stack-KMS-cycle posture as every
    // other secret import in this codebase.
    const sesIngestSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "ImportSesIngestSecret",
      props.sesIngestSecretName,
    );

    // The DEV backend verifies against its OWN copy of the shared secret, so
    // the Lambda must be able to read both. AuthStack already creates both
    // unconditionally; before dev routing existed only prod's was ever read.
    const devSesIngestSecret = props.devSesIngestSecretName
      ? secretsmanager.Secret.fromSecretNameV2(
          this,
          "ImportDevSesIngestSecret",
          props.devSesIngestSecretName,
        )
      : undefined;

    // -------------------------------------------------------------------------
    // The ses-inbound ingest Lambda. Its one dependency is dkimpy (pure
    // Python, + dnspython) so each DKIM signature is verified on its own.
    // Non-VPC — it talks to S3, Secrets Manager, public DNS (DKIM keys), and
    // the backend's public HTTPS endpoint.
    // -------------------------------------------------------------------------
    const sesInboundSrc = path.join(__dirname, "..", "..", "lambda", "ses-inbound");
    this.ingestFunction = new lambda.Function(this, "SesIngestFunction", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(sesInboundSrc, {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          command: ["bash", "-c", "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output"],
          local: {
            // Pure-Python wheels, so the host's pip resolves the same files
            // the 3.12 image would — no platform pins needed.
            tryBundle(outputDir: string): boolean {
              try {
                execSync(`pip3 install -r ${sesInboundSrc}/requirements.txt -t ${outputDir} -q`, { stdio: "inherit" });
                for (const f of fs.readdirSync(sesInboundSrc)) {
                  // Files only: pytest leaves a __pycache__/ dir here and
                  // copyFileSync throws EISDIR on it (see api-stack.ts).
                  const src = path.join(sesInboundSrc, f);
                  if (fs.statSync(src).isFile()) {
                    fs.copyFileSync(src, path.join(outputDir, f));
                  }
                }
                return true;
              } catch {
                return false;
              }
            },
          },
        },
      }),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        RAW_MAIL_BUCKET: this.rawBucket.bucketName,
        RAW_MAIL_PREFIX: "inbound/",
        ATTACHMENTS_BUCKET: this.attachmentsBucket.bucketName,
        BACKEND_INGEST_URL: `https://api.${rootDomain}/api/v1/webhooks/ses-inbound`,
        SES_INBOUND_SECRET_ARN: sesIngestSecret.secretArn,
        // Per-environment routing. Absent => every message goes to prod,
        // which is the behaviour before dev is enabled (see `_route_for`).
        // Comma-separated: while the legacy domain is still published there
        // are TWO dev suffixes (`dev.thelightbulb.company` and
        // `dev.isol8.co`), and a message for either must reach the DEV
        // backend. A single suffix would silently hand every legacy dev
        // address to the PROD backend, which has never heard of it.
        ...(devMailDomain && devSesIngestSecret
          ? {
              DEV_MAIL_SUFFIX: [devMailDomain, legacyDevMailDomain]
                .filter(Boolean)
                .join(","),
              // devRootDomain, NOT rootDomain: dev's API host follows the DEV
              // product domain, which has already flipped. Using rootDomain here
              // routed every dev address at the PROD backend.
              DEV_BACKEND_INGEST_URL: `https://api-dev.${devRootDomain}/api/v1/webhooks/ses-inbound`,
              DEV_SES_INBOUND_SECRET_ARN: devSesIngestSecret.secretArn,
            }
          : {}),
      },
      logGroup: new logs.LogGroup(this, "SesIngestFunctionLogs", {
        logGroupName: logGroupPath(envName, "ses-ingest"),
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });

    this.rawBucket.grantRead(this.ingestFunction);
    this.attachmentsBucket.grantReadWrite(this.ingestFunction);
    // grantRead only adds GetSecretValue — the secret is CMK-encrypted with
    // AuthStack's key, and that key's ARN is unknowable here (cross-stage:
    // this is the prod-stage SesInboundStack, and threading a KMS ARN prop
    // just for this one secret would reintroduce the cross-stack-ref cycle
    // this codebase deliberately avoids elsewhere). Same trap + same fix as
    // bedrock-meter-stack.ts's Metronome secret import: scope kms:Decrypt by
    // kms:ViaService=secretsmanager instead of a specific key ARN. Read-only
    // (no GenerateDataKey) — this Lambda never writes the secret.
    sesIngestSecret.grantRead(this.ingestFunction);
    devSesIngestSecret?.grantRead(this.ingestFunction);
    this.ingestFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "KmsDecryptViaSecretsManager",
        effect: iam.Effect.ALLOW,
        actions: ["kms:Decrypt"],
        resources: ["*"],
        conditions: {
          StringEquals: {
            "kms:ViaService": [`secretsmanager.${this.region}.amazonaws.com`],
          },
        },
      }),
    );

    // -------------------------------------------------------------------------
    // Receipt rule set + catch-all rule.
    //
    // BOTH entries per domain are required. In SES, a bare domain matches ONLY
    // that exact domain; matching subdomains needs a LEADING DOT. Every agent
    // mailbox lives on a subdomain (`friday@{slug}.{domain}`), so `[domain]`
    // alone matched nothing an agent could ever be reached at — mail hit SES
    // (the `*.{domain}` MX resolves) and was dropped with no S3 object and no
    // Lambda invocation. Inbound agent mail had never once been delivered.
    //
    // An earlier comment here asserted that a bare domain also matches
    // subdomains. It does not, and the whole inbound design rested on that
    // untested claim. The bare domain is kept so each apex keeps working.
    //
    // The legacy pair is what keeps every pre-existing
    // `{agent}@{slug}.isol8.co` address deliverable; it comes out with the
    // rest of the legacy leg in the retirement PR.
    //
    // No fixed `receiptRuleSetName` — CDK auto-generates one, avoiding the
    // CREATE-before-DELETE collision risk a fixed name would have on replace
    // (same reasoning as the meter Lambdas in bedrock-meter-stack.ts).
    //
    // NOTE ON BUCKET/LAMBDA PERMISSIONS: unlike the task's initial framing,
    // no hand-rolled bucket resource policy or Lambda permission is needed
    // here — `sesActions.S3` and `sesActions.Lambda`'s own `bind()` methods
    // already add the `ses.amazonaws.com` principal grants (S3: scoped
    // `s3:PutObject` on `{prefix}*`, conditioned on `aws:Referer` = this
    // account; Lambda: `addPermission` scoped by `sourceAccount`) the moment
    // `addRule` is called below. Adding a second, hand-rolled statement would
    // just be a redundant/overlapping policy.
    // -------------------------------------------------------------------------
    this.ruleSet = new ses.ReceiptRuleSet(this, "AgentMailRuleSet");

    const catchAllRule = this.ruleSet.addRule("AgentMailCatchAll", {
      recipients: [
        mailDomain,
        `.${mailDomain}`,
        ...(publishLegacy ? [legacyMailDomain, `.${legacyMailDomain}`] : []),
      ],
      scanEnabled: true,
      actions: [
        new sesActions.S3({ bucket: this.rawBucket, objectKeyPrefix: "inbound/" }),
        new sesActions.Lambda({ function: this.ingestFunction }),
      ],
    });

    // -------------------------------------------------------------------------
    // Activate the rule set. No CDK L1/L2 for SetActiveReceiptRuleSet — same
    // AwsCustomResource pattern as bedrock-meter-stack.ts's
    // PutModelInvocationLoggingConfiguration custom resource. It has NO
    // onDelete — see below.
    // -------------------------------------------------------------------------
    // "V2" for the same ServiceToken-replacement reason as BedrockLoggingConfigV2
    // (bedrock-meter-stack.ts). NOTE the original note here claimed the stable
    // physical id means the old resource's Delete is skipped on replacement.
    // That is FALSE across a logical-id rename, and it was proven in production
    // by the Bedrock twin of this resource: on 2026-08-30 the renamed resource's
    // Create wrote the config at 22:13:14Z and the OLD logical id's Delete wiped
    // it at 22:13:23Z (CloudTrail). CFN deletes by LOGICAL id; a stable physical
    // id changes nothing.
    const activateRuleSet = new cr.AwsCustomResource(this, "ActivateAgentMailRuleSetV2", {
      // Explicit name + role so the helper Lambda carries the lightbulb
      // prefix instead of the auto-name derived from the old stack name.
      functionName: `lightbulb-${envName}-ses-rule-set-activate`,
      role: new iam.Role(this, "ActivateAgentMailRuleSetRole", {
        roleName: `lightbulb-${envName}-ses-rule-set-activate`,
        assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
        ],
      }),
      onCreate: {
        service: "SES",
        action: "setActiveReceiptRuleSet",
        parameters: { RuleSetName: this.ruleSet.receiptRuleSetName },
        physicalResourceId: cr.PhysicalResourceId.of(`ses-active-rule-set-${this.region}`),
      },
      onUpdate: {
        service: "SES",
        action: "setActiveReceiptRuleSet",
        parameters: { RuleSetName: this.ruleSet.receiptRuleSetName },
        physicalResourceId: cr.PhysicalResourceId.of(`ses-active-rule-set-${this.region}`),
      },
      // NO onDelete. setActiveReceiptRuleSet with empty params does not
      // deactivate "our" rule set — omitting RuleSetName deactivates WHATEVER
      // is currently active, account-wide, which stops inbound agent mail for
      // every tenant. Renaming this logical id again would fire that Delete
      // seconds after the replacement's Create, exactly as the Bedrock twin
      // did. Teardown now leaves the active rule set in place: a stale active
      // rule set is strictly safer than silently losing inbound mail.
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ["ses:SetActiveReceiptRuleSet"],
          resources: ["*"], // account-level config — no resource ARN exists
        }),
      ]),
    });
    // Activation must run AFTER the rule (and its S3/Lambda actions' own
    // resource policies) exist — otherwise a race could activate the rule
    // set before SES is permitted to write to the bucket / invoke the fn.
    activateRuleSet.node.addDependency(catchAllRule);

    cdk.Tags.of(this).add("Project", "lightbulb");
    cdk.Tags.of(this).add("Environment", envName);
  }
}
