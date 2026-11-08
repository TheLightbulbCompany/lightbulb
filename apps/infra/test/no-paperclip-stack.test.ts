import { App, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { HOSTED_ZONE_CONTEXT } from "./helpers";
import cdkJson from "../cdk.json";

// Paperclip exit, step 1 of 2 (task 8b.10). The Paperclip Fargate stack is out
// of the app. The Aurora cluster stays one more deploy, set up so that step 2's
// removal leaves a final snapshot and orphans (never force-deletes) the
// Paperclip-only secrets.
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

// Logical ids that may still say Paperclip after step 1. Step 2 removes all
// but the DynamoDB table and the two secrets the backend still reads.
const ALLOWED = [
  /^PaperclipCompaniesTable/, // DynamoDB, still read (identity rows)
  /^PaperclipServiceTokenKeyLightbulb/, // generic HS256 key: service tokens, share links, digest
  /^PaperclipEventsWebhookSecretLightbulb/, // still guards /api/v1/lifecycle/*
  /^PaperclipBetterAuthSecretLightbulb/, // step 2 removes (RETAIN)
  /^PaperclipAdminCredentialsLightbulb/, // step 2 removes (RETAIN)
  /^PaperclipDb/, // cluster, writer, subnet group, SG, secret: step 2 removes
];

describe.each(["dev", "prod"] as const)("Paperclip exit step 1 (%s)", (env) => {
  const templates = synth(env);
  const resources = Object.values(templates).flatMap((t) => Object.entries<any>(t.Resources ?? {}));

  test("no Paperclip stack", () => {
    expect(Object.keys(templates).filter((n) => /paperclip/i.test(n))).toEqual([]);
  });

  test("only allowed Paperclip logical ids remain", () => {
    const left = resources.map(([id]) => id).filter((id) => /Paperclip/.test(id));
    expect(left.filter((id) => !ALLOWED.some((re) => re.test(id)))).toEqual([]);
  });

  test("the one Aurora cluster takes a final snapshot when removed and is not deletion-protected", () => {
    const clusters = resources.filter(([, r]) => r.Type === "AWS::RDS::DBCluster");
    expect(clusters).toHaveLength(1);
    const [, cluster] = clusters[0];
    expect(cluster.DeletionPolicy).toBe("Snapshot");
    expect(cluster.Properties.DeletionProtection).not.toBe(true);
  });

  test("Paperclip-only secrets are RETAIN, so removing them orphans instead of force-deleting", () => {
    const retain = resources
      .filter(([id, r]) => r.Type === "AWS::SecretsManager::Secret" && /^Paperclip(BetterAuth|AdminCredentials|DbV2Secret)/.test(id))
      .map(([id, r]) => [id.replace(/[0-9A-F]{8}$/, ""), r.DeletionPolicy]);
    expect(retain.sort()).toEqual([
      ["PaperclipAdminCredentialsLightbulb", "Retain"],
      ["PaperclipBetterAuthSecretLightbulb", "Retain"],
      ["PaperclipDbV2Secret", "Retain"],
    ]);
  });

  test("no KMS key is Paperclip's own (the cluster uses the shared general key)", () => {
    expect(resources.filter(([id, r]) => r.Type === "AWS::KMS::Key" && /paperclip/i.test(id))).toEqual([]);
  });
});
