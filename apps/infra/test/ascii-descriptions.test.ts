import { App, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { Isol8Stage } from "../lib/isol8-stage";

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

function assertAscii(template: Template, resourceType: string, descKey: string) {
  const resources = template.findResources(resourceType);
  for (const [logicalId, body] of Object.entries(resources)) {
    const desc = (body as any).Properties?.[descKey];
    if (typeof desc === "string" && !ASCII_RE.test(desc)) {
      const offending = [...desc].find((c) => c.charCodeAt(0) > 127);
      const codePoint = offending
        ? offending.charCodeAt(0).toString(16)
        : "?";
      throw new Error(
        `${resourceType} ${logicalId} has non-ASCII char (U+${codePoint}) in ${descKey}: ${JSON.stringify(desc)}`,
      );
    }
  }
}

/**
 * Synthesizes the dev Isol8 stage with the static cdk.context.json
 * lookups inlined so DnsStack's HostedZone.fromLookup resolves without
 * an AWS API call.
 */
function synthDevStageTemplates(): Template[] {
  const app = new App({
    context: {
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
    },
  });
  const stage = new Isol8Stage(app, "dev", {
    environment: "dev",
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
    templates = synthDevStageTemplates();
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
