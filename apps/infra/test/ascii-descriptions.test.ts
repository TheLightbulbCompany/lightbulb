import { App, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { Isol8Stage } from "../lib/isol8-stage";
import { HOSTED_ZONE_CONTEXT } from "./helpers";

/**
 * EC2 rejects non-ASCII in SG GroupDescription + SG Ingress/Egress
 * description. This test synthesizes the dev stage and asserts no
 * description field contains non-ASCII bytes — fails fast at CI time so
 * the next deploy doesn't.
 *
 * Resources covered:
 *  - AWS::EC2::SecurityGroup → GroupDescription
 *  - AWS::EC2::SecurityGroupIngress → Description
 *  - AWS::EC2::SecurityGroupEgress → Description
 *
 * NOT covered (intentionally — AWS allows non-ASCII):
 *  - CloudFormation Outputs (Description field)
 *  - CloudWatch Alarms (AlarmDescription)
 *  - IAM policies (Description)
 */

const ASCII_RE = /^[\x00-\x7F]*$/;

// EC2's SG-description constraint is NARROWER than ASCII: the allowed set is
// exactly `a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*`. An ASCII-only check waved through
// a "->" description that CloudFormation then rejected at deploy time
// (isol8-dev-network UPDATE_FAILED, 2026-08-16) -- the exact class this file
// exists to catch at synth time instead.
const EC2_SG_DESC_RE = /^[a-zA-Z0-9. _\-:\/()#,@\[\]+=&;{}!$*]*$/;

function assertAscii(template: Template, resourceType: string, descKey: string) {
  // SG rule descriptions get the narrower EC2 charset; everything else the
  // original ASCII rule. Inline SG-resource ingress/egress rules also carry
  // Description fields nested in arrays, checked below.
  const re = resourceType.startsWith("AWS::EC2::SecurityGroup") ? EC2_SG_DESC_RE : ASCII_RE;
  const check = (desc: unknown, logicalId: string, where: string) => {
    if (typeof desc === "string" && !re.test(desc)) {
      const offending = [...desc].find((c) => !re.test(c));
      throw new Error(
        `${resourceType} ${logicalId} has a char EC2/ASCII rejects (${JSON.stringify(offending)}) in ${where}: ${JSON.stringify(desc)}`,
      );
    }
  };
  const resources = template.findResources(resourceType);
  for (const [logicalId, body] of Object.entries(resources)) {
    const props = (body as any).Properties ?? {};
    check(props[descKey], logicalId, descKey);
    // AWS::EC2::SecurityGroup inlines its rules — the "->" that broke the
    // 2026-08-16 dev deploy lived in SecurityGroupIngress[].Description,
    // which the top-level descKey check never saw.
    for (const ruleKey of ["SecurityGroupIngress", "SecurityGroupEgress"]) {
      for (const rule of props[ruleKey] ?? []) {
        check(rule?.Description, logicalId, `${ruleKey}[].Description`);
      }
    }
  }
}

/**
 * Synthesizes an Isol8 stage with the static cdk.context.json lookups
 * inlined so DnsStack's HostedZone.fromLookup resolves without an AWS API
 * call. BOTH dev and prod are synthesized: some stacks are prod-only
 * (e.g. BedrockMeterStack), and an em-dash in its SG GroupDescription was
 * the prod CREATE_FAILED root cause — a dev-only guard never covered it.
 */
function synthStageTemplates(environment: "dev" | "prod"): Template[] {
  const app = new App({ context: HOSTED_ZONE_CONTEXT });
  const stage = new Isol8Stage(app, environment, {
    environment,
    env: { account: "877352799272", region: "us-east-1" },
  });
  // Stage children are Stacks (and possibly nested constructs). Filter
  // strictly with `instanceof Stack` so we never call Template.fromStack
  // on a non-Stack child.
  const stacks = stage.node.children.filter(
    (c): c is Stack => c instanceof Stack,
  );
  return stacks.map((s) => Template.fromStack(s));
}

describe("EC2 description fields are ASCII-only", () => {
  let templates: Template[];

  beforeAll(() => {
    // Cover both stages so prod-only stacks (BedrockMeterStack) are guarded.
    templates = [
      ...synthStageTemplates("dev"),
      ...synthStageTemplates("prod"),
    ];
    expect(templates.length).toBeGreaterThan(0);
  });

  it("AWS::EC2::SecurityGroup GroupDescription is ASCII", () => {
    for (const t of templates) {
      assertAscii(t, "AWS::EC2::SecurityGroup", "GroupDescription");
    }
  });

  it("AWS::EC2::SecurityGroupIngress Description is ASCII", () => {
    for (const t of templates) {
      assertAscii(t, "AWS::EC2::SecurityGroupIngress", "Description");
    }
  });

  it("AWS::EC2::SecurityGroupEgress Description is ASCII", () => {
    for (const t of templates) {
      assertAscii(t, "AWS::EC2::SecurityGroupEgress", "Description");
    }
  });
});
