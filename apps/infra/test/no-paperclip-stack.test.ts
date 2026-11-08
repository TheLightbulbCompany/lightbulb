import { App, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { HOSTED_ZONE_CONTEXT } from "./helpers";
import cdkJson from "../cdk.json";

// Paperclip exit (task 8b.10): no Paperclip stack, no Aurora cluster, and only
// the Paperclip-named pieces the backend still reads.
// cdk.json's feature flags are loaded so this matches `cdk synth`: without
// them the snapshot cluster renders a second, generated credentials secret.
function synth(environment: "dev" | "prod"): Record<string, any> {
  const app = new App({ context: { ...cdkJson.context, ...HOSTED_ZONE_CONTEXT } });
  const stage = new LightbulbStage(app, environment, {
    environment,
    env: { account: "877352799272", region: "us-east-1" },
  });
  const stacks = stage.node.children.filter((c): c is Stack => c instanceof Stack);
  return Object.fromEntries(stacks.map((s) => [s.stackName, Template.fromStack(s).toJSON()]));
}

// Logical ids that may still say Paperclip.
const ALLOWED = [
  /^PaperclipCompaniesTable/, // DynamoDB, still read (identity rows)
  /^PaperclipServiceTokenKeyLightbulb/, // generic HS256 key: service tokens, share links, digest
  /^PaperclipEventsWebhookSecretLightbulb/, // still guards /api/v1/lifecycle/*
];

describe.each(["dev", "prod"] as const)("Paperclip exit (%s)", (env) => {
  const templates = synth(env);
  const resources = Object.values(templates).flatMap((t) => Object.entries<any>(t.Resources ?? {}));

  test("no Paperclip stack", () => {
    expect(Object.keys(templates).filter((n) => /paperclip/i.test(n))).toEqual([]);
  });

  test("only allowed Paperclip logical ids remain", () => {
    const left = resources.map(([id]) => id).filter((id) => /Paperclip/.test(id));
    expect(left.filter((id) => !ALLOWED.some((re) => re.test(id)))).toEqual([]);
  });

  test("no RDS resources", () => {
    expect(resources.filter(([, r]) => r.Type.startsWith("AWS::RDS::"))).toEqual([]);
  });

  test("no KMS key is Paperclip's own (the old cluster and its snapshots use the shared general key)", () => {
    expect(resources.filter(([id, r]) => r.Type === "AWS::KMS::Key" && /paperclip/i.test(id))).toEqual([]);
  });
});
