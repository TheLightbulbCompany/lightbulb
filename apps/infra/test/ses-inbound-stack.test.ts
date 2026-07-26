import { App, Stack } from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Isol8Stage } from "../lib/isol8-stage";
import { SesInboundStack, SesInboundStackProps } from "../lib/stacks/ses-inbound-stack";

const ACCOUNT = "877352799272";
const REGION = "us-east-1";

// Same static hosted-zone lookup cache as ascii-descriptions.test.ts, so
// DnsStack's HostedZone.fromLookup resolves without an AWS API call.
const HOSTED_ZONE_CONTEXT = {
  "availability-zones:account=877352799272:region=us-east-1": [
    "us-east-1a",
    "us-east-1b",
    "us-east-1c",
    "us-east-1d",
    "us-east-1e",
    "us-east-1f",
  ],
  "hosted-zone:account=877352799272:domainName=isol8.co:region=us-east-1": {
    Id: "/hostedzone/Z09248243AOUC775CDUI4",
    Name: "isol8.co.",
  },
};

/**
 * Builds a standalone SesInboundStack against a throwaway upstream stack that
 * owns the (imported) hosted zone — mirrors how helpers.ts's synthApiStack
 * builds real upstream dependencies rather than mocking them.
 */
function synthSesInboundStack(
  overrides: Partial<SesInboundStackProps> = {},
): Template {
  const app = new App({ context: HOSTED_ZONE_CONTEXT });
  const support = new Stack(app, "Support", {
    env: { account: ACCOUNT, region: REGION },
  });
  const hostedZone = route53.HostedZone.fromLookup(support, "Zone", {
    domainName: "isol8.co",
  });

  const stack = new SesInboundStack(app, "SesInboundTest", {
    env: { account: ACCOUNT, region: REGION },
    envName: "prod",
    hostedZone,
    sesIngestSecretName: "isol8/prod/ses_ingest_secret",
    mailDomain: "isol8.co",
    ...overrides,
  });
  return Template.fromStack(stack);
}

describe("SesInboundStack", () => {
  let template: Template;

  beforeAll(() => {
    template = synthSesInboundStack();
  });

  test("creates the receipt rule set + a catch-all rule matching the mail domain", () => {
    template.resourceCountIs("AWS::SES::ReceiptRuleSet", 1);
    template.hasResourceProperties("AWS::SES::ReceiptRule", {
      Rule: Match.objectLike({
        Recipients: ["isol8.co"],
        ScanEnabled: true,
        Enabled: true,
      }),
    });
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
    expect(JSON.stringify(activation.Properties.Delete)).toContain(
      "setActiveReceiptRuleSet",
    );
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
      Name: "*.isol8.co.",
      Type: "MX",
      ResourceRecords: ["10 inbound-smtp.us-east-1.amazonaws.com"],
    });
  });

  test("wildcard SPF TXT record authorizes SES sending", () => {
    template.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: "*.isol8.co.",
      Type: "TXT",
      ResourceRecords: ['"v=spf1 include:amazonses.com ~all"'],
    });
  });

  test("publishes an EXPLICIT _dmarc record so the wildcard SPF cannot answer for it", () => {
    // The wildcard TXT above matches `_dmarc.isol8.co`, which would serve an
    // SPF string where a DMARC record belongs and leave the domain with no
    // policy. Only an explicit record outranks the wildcard.
    template.hasResourceProperties("AWS::Route53::RecordSet", {
      Name: "_dmarc.isol8.co.",
      Type: "TXT",
      ResourceRecords: ['"v=DMARC1; p=none; rua=mailto:admin@isol8.co; fo=1"'],
    });
  });

  test("DMARC policy is monitor-only — it governs Google Workspace/Resend/Clerk mail too", () => {
    // p=quarantine or p=reject here would put every unverified sender on the
    // domain at risk, not just SES. Tightening is a deliberate later step.
    const dmarc = Object.values(
      template.findResources("AWS::Route53::RecordSet"),
    ).find((r: any) => r.Properties?.Name === "_dmarc.isol8.co.") as any;
    expect(dmarc).toBeDefined();
    const value = dmarc.Properties.ResourceRecords[0] as string;
    expect(value).toContain("v=DMARC1");
    expect(value).toContain("p=none");
    expect(value).not.toContain("p=reject");
    expect(value).not.toContain("p=quarantine");
  });

  test("verifies the domain identity (DKIM via the hosted zone)", () => {
    template.resourceCountIs("AWS::SES::EmailIdentity", 1);
    template.hasResourceProperties("AWS::SES::EmailIdentity", {
      EmailIdentity: "isol8.co",
    });
    // 3 DKIM CNAMEs + wildcard MX + wildcard SPF TXT + explicit _dmarc TXT.
    template.resourceCountIs("AWS::Route53::RecordSet", 6);
  });

  test("creates the raw-mail and attachments S3 buckets, both private", () => {
    template.resourceCountIs("AWS::S3::Bucket", 2);
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "isol8-prod-agent-mail-raw",
      PublicAccessBlockConfiguration: Match.objectLike({
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
      }),
    });
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "isol8-prod-agent-mail-attachments",
      PublicAccessBlockConfiguration: Match.objectLike({
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
      }),
    });
  });

  test("raw bucket expires objects after 7 days, attachments after 30", () => {
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "isol8-prod-agent-mail-raw",
      LifecycleConfiguration: {
        Rules: Match.arrayWith([Match.objectLike({ ExpirationInDays: 7 })]),
      },
    });
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "isol8-prod-agent-mail-attachments",
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
        BACKEND_INGEST_URL: "https://api.isol8.co/api/v1/webhooks/ses-inbound",
      }),
    );
  });

  test("no cross-stack export is created for the bucket names (deterministic literals only)", () => {
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain("Fn::ImportValue");
  });
});

describe("SesInboundStack gating (Isol8Stage)", () => {
  function stageStacks(environment: "dev" | "prod"): Stack[] {
    const app = new App({ context: HOSTED_ZONE_CONTEXT });
    const stage = new Isol8Stage(app, environment, {
      environment,
      env: { account: ACCOUNT, region: REGION },
    });
    return stage.node.children.filter((c): c is Stack => c instanceof Stack);
  }

  test("prod stage: the ses-inbound stack is created with SES resources", () => {
    const stacks = stageStacks("prod");
    const sesStack = stacks.find((s) => s.node.id === "isol8-prod-ses-inbound");
    expect(sesStack).toBeDefined();

    const t = Template.fromStack(sesStack!);
    t.resourceCountIs("AWS::SES::ReceiptRuleSet", 1);
    t.resourceCountIs("AWS::SES::EmailIdentity", 1);
    t.resourceCountIs("AWS::S3::Bucket", 2);
  });

  test("dev stage: never gets a ses-inbound stack (prod-only, account-scoped rule set)", () => {
    const stacks = stageStacks("dev");
    const sesStack = stacks.find((s) => s.node.id === "isol8-dev-ses-inbound");
    expect(sesStack).toBeUndefined();

    for (const s of stacks) {
      const json = JSON.stringify(Template.fromStack(s).toJSON());
      expect(json).not.toContain("AWS::SES::");
    }
  });

  test("prod: the service stack's AGENT_EMAIL_DOMAIN + attachments-bucket env vars are set live", () => {
    const stacks = stageStacks("prod");
    const serviceStack = stacks.find((s) => s.node.id === "isol8-prod-service");
    expect(serviceStack).toBeDefined();
    const t = Template.fromStack(serviceStack!);
    t.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            { Name: "AGENT_EMAIL_DOMAIN", Value: "isol8.co" },
            {
              Name: "AGENT_EMAIL_ATTACHMENTS_BUCKET",
              Value: "isol8-prod-agent-mail-attachments",
            },
          ]),
        }),
      ]),
    });
  });

  test("dev: the service stack's AGENT_EMAIL_DOMAIN + attachments-bucket env vars are empty (inert)", () => {
    const stacks = stageStacks("dev");
    const serviceStack = stacks.find((s) => s.node.id === "isol8-dev-service");
    expect(serviceStack).toBeDefined();
    const t = Template.fromStack(serviceStack!);
    t.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            { Name: "AGENT_EMAIL_DOMAIN", Value: "" },
            { Name: "AGENT_EMAIL_ATTACHMENTS_BUCKET", Value: "" },
          ]),
        }),
      ]),
    });
  });
});
