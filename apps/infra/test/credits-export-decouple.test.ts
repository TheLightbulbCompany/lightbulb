import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as kms from "aws-cdk-lib/aws-kms";
import { Template } from "aws-cdk-lib/assertions";
import { DatabaseStack } from "../lib/stacks/database-stack";

/**
 * Pass-2 of the credits-ledger export retirement (2026-07-14). The credits +
 * credit-transactions tables were unused by the backend (Metronome is the
 * only biller) and, once Pass-1 proved no stack still imported their
 * auto-generated Arn/Ref exports, both tables and all four pinned exports
 * were deleted outright.
 *
 * These tests lock in that the tables and their exports are GONE.
 */
describe("credits export decouple (pass 2 — retired)", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const env = { account: "877352799272", region: "us-east-1" };
    const support = new cdk.Stack(app, "Support", { env });
    const stack = new DatabaseStack(app, "isol8-dev-database", {
      env,
      stackName: "isol8-dev-database",
      environment: "dev",
      kmsKey: new kms.Key(support, "Key"),
    });
    template = Template.fromStack(stack);
  });

  test("both ledger tables are gone", () => {
    const creditsResources = template.findResources("AWS::DynamoDB::Table", {
      Properties: { TableName: "lightbulb-dev-credits" },
    });
    expect(Object.keys(creditsResources)).toEqual([]);

    const creditTransactionsResources = template.findResources(
      "AWS::DynamoDB::Table",
      { Properties: { TableName: "lightbulb-dev-credit-transactions" } },
    );
    expect(Object.keys(creditTransactionsResources)).toEqual([]);
  });

  test("none of the four former exports are published anymore", () => {
    const outputs = Object.keys(template.toJSON().Outputs ?? {});
    const staleOutputs = outputs.filter(
      (key) =>
        key.includes("CreditsTable") || key.includes("CreditTransactionsTable"),
    );
    expect(staleOutputs).toEqual([]);
  });
});
