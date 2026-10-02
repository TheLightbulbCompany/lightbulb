import { App, Stack } from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import {
  DEV_ROOT_DOMAIN,
  LEGACY_MAIL_DOMAIN,
  PLATFORM_MAIL_DOMAIN,
  ROOT_DOMAIN,
} from "../lib/domains";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { HOSTED_ZONE_CONTEXT } from "./helpers";
import { SesInboundStack, SesInboundStackProps } from "../lib/stacks/ses-inbound-stack";

const ACCOUNT = "877352799272";
const REGION = "us-east-1";

/**
 * Builds a standalone SesInboundStack. The stack looks BOTH mail zones up by
 * name itself (mail no longer lives on the product domain), so the only
 * upstream it needs is the cached hosted-zone context.
 */
function synthSesInboundStack(
  overrides: Partial<SesInboundStackProps> = {},
): Template {
  const app = new App({ context: HOSTED_ZONE_CONTEXT });
  const stack = new SesInboundStack(app, "SesInboundTest", {
    env: { account: ACCOUNT, region: REGION },
    envName: "prod",
    sesIngestSecretName: "lightbulb/prod/ses_ingest_secret",
    mailDomain: PLATFORM_MAIL_DOMAIN,
    ...overrides,
  });
  return Template.fromStack(stack);
}

// LEGACY_MAIL_DOMAIN is now "" — the retirement shipped. The legacy leg is
// still a supported mechanism (it is how ANY mail domain gets retired), so it
// keeps its coverage here via an explicit prop rather than the constant.
const RETIRED_MAIL_DOMAIN = "isol8.co";

describe("SesInboundStack", () => {
  let template: Template;
  let legacyTemplate: Template;

  beforeAll(() => {
    template = synthSesInboundStack();
    legacyTemplate = synthSesInboundStack({
      legacyMailDomain: RETIRED_MAIL_DOMAIN,
    });
  });

  test("catch-all rule matches the apex AND every subdomain mailbox, on BOTH mail domains", () => {
    // The leading-dot entry is the load-bearing one: agent mailboxes are
    // ALWAYS `friday@{slug}.{domain}`, and in SES a bare domain matches only
    // the exact domain. With the bare domain alone, every agent reply hit SES
    // and was dropped — no S3 object, no Lambda invocation — so inbound had
    // never been delivered once in prod. This test previously asserted that
    // broken value, which is why nothing caught it.
    //
    // The legacy pair is equally load-bearing right now: every address already
    // in the DynamoDB address book is `{agent}@{slug}.isol8.co`, and dropping
    // it here would stop delivering to all of them.
    legacyTemplate.resourceCountIs("AWS::SES::ReceiptRuleSet", 1);
    legacyTemplate.hasResourceProperties("AWS::SES::ReceiptRule", {
      Rule: Match.objectLike({
        Recipients: [
          PLATFORM_MAIL_DOMAIN,
          `.${PLATFORM_MAIL_DOMAIN}`,
          RETIRED_MAIL_DOMAIN,
          `.${RETIRED_MAIL_DOMAIN}`,
        ],
        ScanEnabled: true,
        Enabled: true,
      }),
    });
  });

  test("apex SPF authorizes Google AND SES, and preserves the site-verification token", () => {
    // An apex with no SPF authenticates as spf=none; with Google's default
    // gappssmtp DKIM being unaligned to the apex, DMARC then fails outright
    // and the inbound anti-spoof gate drops the owner's own replies to their
    // agent. That happened on isol8.co and is why this record exists.
    //
    // Both values are asserted because a Route53 TXT record is a SET, and
    // the `deleteExisting` below removes the pre-existing set before creating
    // this one — so a value dropped from this array is GONE from the zone.
    // The thelightbulb.company apex already held a Google verification token
    // (added by hand); losing it un-verifies the Workspace.
    template.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: `${PLATFORM_MAIL_DOMAIN}.`,
      Type: "TXT",
      ResourceRecords: [
        '"v=spf1 include:_spf.google.com include:amazonses.com ~all"',
        '"google-site-verification=v-MtfFMuBP2VYDP5sIB3wVXTQU4SjimPoryr5oX6mQg"',
        '"google-site-verification=Clg4oJdtZ_0J7pJRFmfJfkKr0KE8C9CVze8I7AIbaWU"',
      ],
    });
  });

  test("the LEGACY apex keeps its own SPF + its own site-verification token", () => {
    // Each apex carries its OWN token. Handing one domain the other's would
    // un-verify both — the new domain because its token vanished from the set,
    // the old one because the token it was handed does not belong to it.
    legacyTemplate.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: `${RETIRED_MAIL_DOMAIN}.`,
      Type: "TXT",
      ResourceRecords: [
        '"v=spf1 include:_spf.google.com include:amazonses.com ~all"',
        '"google-site-verification=4m6AkWYnaePsQsIyDL4eJlPEddQg2RF1_Hxl7EFDHr8"',
      ],
    });
  });

  test("both apex SPF records delete the pre-existing unmanaged TXT set first", () => {
    // Without this, the deploy FAILS and rolls the stack back:
    //   Tried to create resource record set [name='isol8.co.', type='TXT']
    //   but it already exists
    // CloudFormation's Route53 CREATE does not upsert, and a TXT set already
    // existed at BOTH apexes outside CFN. This cost one failed prod deploy.
    legacyTemplate.resourceCountIs("Custom::DeleteExistingRecordSet", 2);
    // Retired: one apex left, so one delete-existing.
    template.resourceCountIs("Custom::DeleteExistingRecordSet", 1);
  });

  test("the legacy mail domain is published UNCHANGED alongside the new one", () => {
    // Every agent address already in DynamoDB is `{agent}@{slug}.isol8.co`
    // and this stack does not migrate them. Repointing these records instead
    // of adding new ones would replace them — deleting the `*.isol8.co` MX
    // (inbound dies for every existing mailbox) and the isol8.co apex TXT set
    // (taking its Workspace verification token with it).
    legacyTemplate.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: `*.${RETIRED_MAIL_DOMAIN}.`,
      Type: "MX",
      ResourceRecords: ["10 inbound-smtp.us-east-1.amazonaws.com"],
    });
    legacyTemplate.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: `_dmarc.${RETIRED_MAIL_DOMAIN}.`,
      Type: "TXT",
      ResourceRecords: [
        `"v=DMARC1; p=none; rua=mailto:admin@${RETIRED_MAIL_DOMAIN}; fo=1"`,
      ],
    });
    legacyTemplate.hasResourceProperties("AWS::SES::EmailIdentity", {
      EmailIdentity: RETIRED_MAIL_DOMAIN,
    });
  });

  test("turning the legacy leg off leaves exactly one mail domain", () => {
    // This is the retirement step, and the only supported way to take the old
    // domain out: one flag, not a hand-edit of every record.
    const t = synthSesInboundStack({ legacyMailDomain: "" });
    t.resourceCountIs("AWS::SES::EmailIdentity", 1);
    t.hasResourceProperties("AWS::SES::ReceiptRule", {
      Rule: Match.objectLike({
        Recipients: [PLATFORM_MAIL_DOMAIN, `.${PLATFORM_MAIL_DOMAIN}`],
      }),
    });
    // 3 DKIM CNAMEs + wildcard MX + wildcard SPF + _dmarc + apex SPF.
    t.resourceCountIs("AWS::Route53::RecordSet", 7);
  });

  test("catch-all rule wires both the S3 action and the Lambda action", () => {
    template.hasResourceProperties("AWS::SES::ReceiptRule", {
      Rule: Match.objectLike({
        Actions: Match.arrayWith([
          Match.objectLike({
            S3Action: Match.objectLike({ ObjectKeyPrefix: "inbound/" }),
          }),
          Match.objectLike({
            LambdaAction: Match.objectLike({}),
          }),
        ]),
      }),
    });
  });

  test("activates the rule set via a custom resource that depends on the rule", () => {
    const customResources = template.findResources("Custom::AWS");
    const activation = Object.values(customResources).find((r: any) =>
      JSON.stringify(r.Properties?.Create ?? "").includes("setActiveReceiptRuleSet"),
    ) as any;
    expect(activation).toBeDefined();
    expect(JSON.stringify(activation.Properties.Create)).toContain(
      "setActiveReceiptRuleSet",
    );
    // REGRESSION, same class as the Bedrock logging incident (2026-08-30):
    // there must be NO Delete. setActiveReceiptRuleSet with empty params does
    // not deactivate "our" rule set — omitting RuleSetName deactivates WHATEVER
    // is active, account-wide, stopping inbound agent mail for every tenant.
    // Its Bedrock twin proved the mechanism live: a logical-id rename made the
    // OLD id's Delete fire 9s after the new one's Create (CloudTrail); CFN
    // deletes by LOGICAL id however stable the physical id is. Assert on the
    // Delete PROPERTY — the template's own DeletionPolicy is the literal
    // string "Delete" and would make a serialised check meaningless.
    expect(activation.Properties.Delete).toBeUndefined();
    // DependsOn must include the catch-all rule's logical id so activation
    // never races ahead of the rule (and its S3/Lambda action permissions)
    // existing.
    const ruleLogicalIds = Object.keys(template.findResources("AWS::SES::ReceiptRule"));
    expect(ruleLogicalIds.length).toBe(1);
    expect(activation.DependsOn).toEqual(
      expect.arrayContaining([ruleLogicalIds[0]]),
    );
  });

  test("wildcard MX record routes every company subdomain to SES", () => {
    template.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: `*.${PLATFORM_MAIL_DOMAIN}.`,
      Type: "MX",
      ResourceRecords: ["10 inbound-smtp.us-east-1.amazonaws.com"],
    });
  });

  test("wildcard SPF TXT record authorizes SES sending", () => {
    template.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: `*.${PLATFORM_MAIL_DOMAIN}.`,
      Type: "TXT",
      ResourceRecords: ['"v=spf1 include:amazonses.com ~all"'],
    });
  });

  test("publishes an EXPLICIT _dmarc record so the wildcard SPF cannot answer for it", () => {
    // The wildcard TXT above matches `_dmarc.isol8.co`, which would serve an
    // SPF string where a DMARC record belongs and leave the domain with no
    // policy. Only an explicit record outranks the wildcard.
    template.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: `_dmarc.${PLATFORM_MAIL_DOMAIN}.`,
      Type: "TXT",
      ResourceRecords: [
        `"v=DMARC1; p=none; rua=mailto:admin@${PLATFORM_MAIL_DOMAIN}; fo=1"`,
      ],
    });
  });

  test("DMARC policy is monitor-only — it governs Google Workspace/Resend/Clerk mail too", () => {
    // p=quarantine or p=reject here would put every unverified sender on the
    // domain at risk, not just SES. Tightening is a deliberate later step.
    const dmarc = Object.values(
      template.findResources("AWS::Route53::RecordSet"),
    ).find(
      (r: any) => r.Properties?.Name === `_dmarc.${PLATFORM_MAIL_DOMAIN}.`,
    ) as any;
    expect(dmarc).toBeDefined();
    const value = dmarc.Properties.ResourceRecords[0] as string;
    expect(value).toContain("v=DMARC1");
    expect(value).toContain("p=none");
    expect(value).not.toContain("p=reject");
    expect(value).not.toContain("p=quarantine");
  });

  test("verifies the domain identity (DKIM via the hosted zone)", () => {
    // ONE identity now. It was two while the move was in flight; the address
    // backfill that gated the teardown has run in both envs, so the legacy
    // identity is gone. The legacy leg still synths two — asserted below.
    template.resourceCountIs("AWS::SES::EmailIdentity", 1);
    legacyTemplate.resourceCountIs("AWS::SES::EmailIdentity", 2);
    template.hasResourceProperties("AWS::SES::EmailIdentity", {
      EmailIdentity: PLATFORM_MAIL_DOMAIN,
    });
    // Per domain: 3 DKIM CNAMEs + wildcard MX + wildcard SPF TXT + explicit
    // _dmarc TXT + apex SPF TXT = 7. One domain now; the legacy leg doubles it.
    template.resourceCountIs("AWS::Route53::RecordSet", 7);
    legacyTemplate.resourceCountIs("AWS::Route53::RecordSet", 14);
  });

  test("creates the raw-mail and attachments S3 buckets, both private", () => {
    template.resourceCountIs("AWS::S3::Bucket", 2);
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "lightbulb-prod-agent-mail-raw",
      PublicAccessBlockConfiguration: Match.objectLike({
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
      }),
    });
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "lightbulb-prod-agent-mail-attachments",
      PublicAccessBlockConfiguration: Match.objectLike({
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
      }),
    });
  });

  test("raw bucket expires objects after 7 days, attachments after 30", () => {
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "lightbulb-prod-agent-mail-raw",
      LifecycleConfiguration: {
        Rules: Match.arrayWith([Match.objectLike({ ExpirationInDays: 7 })]),
      },
    });
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "lightbulb-prod-agent-mail-attachments",
      LifecycleConfiguration: {
        Rules: Match.arrayWith([Match.objectLike({ ExpirationInDays: 30 })]),
      },
    });
  });

  test("SES is auto-granted PutObject on the raw bucket via the S3 action's own bind() — no hand-rolled bucket policy needed", () => {
    template.hasResourceProperties("AWS::S3::BucketPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Allow",
            Principal: Match.objectLike({ Service: "ses.amazonaws.com" }),
            Action: "s3:PutObject",
          }),
        ]),
      }),
    });
  });

  test("SES is auto-granted InvokeFunction on the ingest Lambda via the Lambda action's own bind()", () => {
    template.hasResourceProperties("AWS::Lambda::Permission", {
      Action: "lambda:InvokeFunction",
      Principal: "ses.amazonaws.com",
    });
  });

  test("the ingest Lambda is Python 3.12, non-VPC, and wired with the expected env vars", () => {
    const fns = Object.values(template.findResources("AWS::Lambda::Function")).filter(
      (fn: any) => fn.Properties?.Environment?.Variables?.RAW_MAIL_PREFIX != null,
    );
    expect(fns.length).toBe(1);
    const fn = fns[0] as any;
    expect(fn.Properties.Runtime).toBe("python3.12");
    expect(fn.Properties.VpcConfig).toBeUndefined();
    expect(fn.Properties.Environment.Variables).toEqual(
      expect.objectContaining({
        RAW_MAIL_PREFIX: "inbound/",
        BACKEND_INGEST_URL: `https://api.${ROOT_DOMAIN}/api/v1/webhooks/ses-inbound`,
      }),
    );
  });

  test("no cross-stack export is created for the bucket names (deterministic literals only)", () => {
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain("Fn::ImportValue");
  });
});

describe("SesInboundStack gating (LightbulbStage)", () => {
  function stageStacks(environment: "dev" | "prod"): Stack[] {
    const app = new App({ context: HOSTED_ZONE_CONTEXT });
    const stage = new LightbulbStage(app, environment, {
      environment,
      env: { account: ACCOUNT, region: REGION },
    });
    return stage.node.children.filter((c): c is Stack => c instanceof Stack);
  }

  test("prod stage: the ses-inbound stack is created with SES resources", () => {
    const stacks = stageStacks("prod");
    const sesStack = stacks.find((s) => s.node.id === "lightbulb-prod-ses-inbound");
    expect(sesStack).toBeDefined();

    const t = Template.fromStack(sesStack!);
    t.resourceCountIs("AWS::SES::ReceiptRuleSet", 1);
    // ONE identity: the legacy leg is retired (LEGACY_MAIL_DOMAIN === "").
    t.resourceCountIs("AWS::SES::EmailIdentity", 1);
    t.resourceCountIs("AWS::S3::Bucket", 2);
  });

  test("dev stage: never gets a ses-inbound stack (prod-only, account-scoped rule set)", () => {
    const stacks = stageStacks("dev");
    const sesStack = stacks.find((s) => s.node.id === "lightbulb-dev-ses-inbound");
    expect(sesStack).toBeUndefined();

    // The account+region-scoped SINGLETON resources (SetActiveReceiptRuleSet
    // is a one-active-rule-set-per-account operation, and the domain
    // identity is likewise account-scoped) must never exist twice. This does
    // NOT forbid every "AWS::SES::" resource type fleet-wide: the per-env
    // AgentMailOutboundStack (task 10) creates a per-env-NAMED
    // AWS::SES::ConfigurationSet + ConfigurationSetEventDestination, and
    // those are deliberately live in dev AND prod — see agent-mail-outbound-stack.test.ts.
    for (const s of stacks) {
      const json = JSON.stringify(Template.fromStack(s).toJSON());
      expect(json).not.toContain("AWS::SES::ReceiptRuleSet");
      expect(json).not.toContain("AWS::SES::ReceiptRule");
      expect(json).not.toContain("AWS::SES::EmailIdentity");
    }
  });

  test("prod: the service stack's AGENT_EMAIL_DOMAIN is set live", () => {
    const stacks = stageStacks("prod");
    const serviceStack = stacks.find((s) => s.node.id === "lightbulb-prod-service");
    expect(serviceStack).toBeDefined();
    const t = Template.fromStack(serviceStack!);
    t.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            { Name: "AGENT_EMAIL_DOMAIN", Value: PLATFORM_MAIL_DOMAIN },
          ]),
        }),
      ]),
    });
  });

  test("the backend is NOT handed an attachments-bucket env var", () => {
    // AGENT_EMAIL_ATTACHMENTS_BUCKET was injected here but never read: nothing
    // in apps/backend/ references it, and Pydantic BaseSettings silently drops
    // unknown vars, so it was inert noise. The SES ingest Lambda uses its own
    // ATTACHMENTS_BUCKET (ses-inbound-stack), and the backend receives
    // attachment metadata in the webhook body instead.
    const serviceStack = stageStacks("prod").find((s) => s.node.id === "lightbulb-prod-service");
    const rendered = JSON.stringify(Template.fromStack(serviceStack!).toJSON());
    expect(rendered).not.toContain("AGENT_EMAIL_ATTACHMENTS_BUCKET");
  });

  test("dev: agent email is LIVE on its own subdomain, without a stack of its own", () => {
    // Dev cannot own an SES receipt rule set (SetActiveReceiptRuleSet is
    // account+region scoped and prod owns it), but it does not need one: SES
    // matches a recipient condition against the domain AND its subdomains, so
    // the single prod-owned catch-all rule already receives
    // `{slug}.dev.isol8.co`. Only the domain and the Lambda's routing differ.
    const stacks = stageStacks("dev");
    const serviceStack = stacks.find((s) => s.node.id === "lightbulb-dev-service");
    expect(serviceStack).toBeDefined();
    const t = Template.fromStack(serviceStack!);
    t.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            {
              Name: "AGENT_EMAIL_DOMAIN",
              Value: `dev.${PLATFORM_MAIL_DOMAIN}`,
            },
          ]),
        }),
      ]),
    });
  });

  test("prod publishes the DEV wildcard MX + SPF (a single-label wildcard does not nest)", () => {
    // `*.isol8.co` matches `acme.isol8.co` but NOT `acme.dev.isol8.co`, so the
    // dev pair is required and lives in the prod stack, which owns mail DNS.
    const stacks = stageStacks("prod");
    const ses = stacks.find((s) => s.node.id === "lightbulb-prod-ses-inbound");
    expect(ses).toBeDefined();
    const t = Template.fromStack(ses!);
    t.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: `*.dev.${PLATFORM_MAIL_DOMAIN}.`,
      Type: "MX",
      ResourceRecords: ["10 inbound-smtp.us-east-1.amazonaws.com"],
    });
    t.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: `*.dev.${PLATFORM_MAIL_DOMAIN}.`,
      Type: "TXT",
      ResourceRecords: ['"v=spf1 include:amazonses.com ~all"'],
    });
  });

  test("the ingest Lambda carries the dev routing env so both backends are reachable", () => {
    const stacks = stageStacks("prod");
    const ses = stacks.find((s) => s.node.id === "lightbulb-prod-ses-inbound");
    const t = Template.fromStack(ses!);
    const fns = Object.values(t.findResources("AWS::Lambda::Function")).filter(
      (fn: any) => fn.Properties?.Environment?.Variables?.RAW_MAIL_PREFIX != null,
    );
    expect(fns.length).toBe(1);
    const env = (fns[0] as any).Properties.Environment.Variables;
    // ONE dev suffix now. This carried `dev.isol8.co` too while dev addresses
    // still lived there; the re-key moved all 6 and the leg was retired, so a
    // second entry here would be `dev.` — a suffix matching everything.
    expect(env.DEV_MAIL_SUFFIX).toBe(`dev.${PLATFORM_MAIL_DOMAIN}`);
    // DEV_ROOT_DOMAIN, not ROOT_DOMAIN: this stack is PROD-ONLY, so `rootDomain`
    // is prod's. Deriving the dev ingest URL from it pointed dev inbound mail at
    // prod's apex - the same string until the two envs are on different domains,
    // then a host that no longer serves it.
    expect(env.DEV_BACKEND_INGEST_URL).toBe(
      `https://api-dev.${DEV_ROOT_DOMAIN}/api/v1/webhooks/ses-inbound`,
    );
    // Each backend verifies the shared secret against its OWN copy, so the
    // Lambda must be able to present dev's rather than prod's.
    expect(env.DEV_SES_INBOUND_SECRET_ARN).toBeDefined();
  });

  test("synth FAILS if a dev mail domain is set without the dev secret name", () => {
    // Routing dev mail while presenting prod's secret would 401 every message,
    // and the only symptom would be silently undelivered mail. Fail at synth.
    expect(() =>
      synthSesInboundStack({
        devMailDomain: `dev.${PLATFORM_MAIL_DOMAIN}`,
        devSesIngestSecretName: undefined,
      }),
    ).toThrow(/devSesIngestSecretName is required/);
  });


});
