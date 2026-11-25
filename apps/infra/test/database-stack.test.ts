import * as cdk from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import { Template } from "aws-cdk-lib/assertions";
import { DatabaseStack } from "../lib/stacks/database-stack";

function buildStack(environment: "dev" | "prod"): Template {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const supportStack = new cdk.Stack(app, `Support-${environment}`, { env });
  const kmsKey = new kms.Key(supportStack, "KmsKey");
  const databaseStack = new DatabaseStack(app, `Database-${environment}`, {
    env,
    environment,
    kmsKey,
  });
  return Template.fromStack(databaseStack);
}

describe.each(["dev", "prod"] as const)("DatabaseStack — retired orphan tables (%s)", (environment) => {
  test("no construct remains for tables nothing on main reads", () => {
    const names = Object.values(buildStack(environment).findResources("AWS::DynamoDB::Table")).map(
      (r: any) => r.Properties.TableName as string,
    );
    for (const suffix of [
      "apps",
      "bedrock-aip-owners",
      "room-read-cursors",
      "marketplace-listings",
      "marketplace-purchases",
      "marketplace-payout-accounts",
      "marketplace-takedowns",
      "marketplace-sellers",
      "admin-actions",
    ]) {
      expect(names).not.toContain(`lightbulb-${environment}-${suffix}`);
    }
  });
});

describe("DatabaseStack — exports", () => {
  test("publishes no CloudFormation exports (nothing imports them)", () => {
    expect(buildStack("prod").toJSON().Outputs ?? {}).toEqual({});
  });
});

describe("DatabaseStack — no join-links table", () => {
  test("a person is the tenant: there is no workspace-join-links table", () => {
    const tables = buildStack("dev").findResources("AWS::DynamoDB::Table");
    const names = Object.values(tables).map((t) => JSON.stringify((t as { Properties: { TableName?: unknown } }).Properties.TableName));
    expect(names.filter((n) => n.includes("join-links"))).toEqual([]);
  });
});

describe("DatabaseStack — containers table", () => {
  test("Spark voice-session rows expire by TTL (session_ttl) in dev and prod", () => {
    buildStack("dev").hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-containers",
      TimeToLiveSpecification: { AttributeName: "session_ttl", Enabled: true },
    });
    const prod = Object.values(buildStack("prod").findResources("AWS::DynamoDB::Table")).find(
      (t: any) => t.Properties.TableName === "lightbulb-prod-containers",
    ) as any;
    expect(prod.Properties.TimeToLiveSpecification).toEqual({ AttributeName: "session_ttl", Enabled: true });
  });
});

