import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as kms from "aws-cdk-lib/aws-kms";
import { Template } from "aws-cdk-lib/assertions";
import { DatabaseStack } from "../lib/stacks/database-stack";

/**
 * Pass-1 of the credits-ledger export retirement. The credits +
 * credit-transactions tables are no longer used by the backend (Metronome is
 * the only biller), but their auto-generated Arn/Ref exports are imported by
 * the LIVE service stack — and CloudFormation refuses to delete an in-use
 * export. Because the database stack deploys BEFORE the service stack drops
 * its imports, deleting table and import in one deploy rolls the database
 * stack back (observed live on the cutover deploy).
 *
 * These tests pin the Pass-1 invariants: the tables still exist and ALL FOUR
 * exports are published under their EXACT live names (verified against the
 * deployed isol8-{env}-database stacks). Pass-2 deletes tables + retentions
 * once no stack imports them.
 */
describe("credits export decouple (pass 1)", () => {
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
      vpc: new ec2.Vpc(support, "Vpc"),
    });
    template = Template.fromStack(stack);
  });

  test("both ledger tables still exist (deletion deferred to pass 2)", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "isol8-dev-credits",
    });
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "isol8-dev-credit-transactions",
    });
  });

  test("all four in-use exports are pinned under their exact live names", () => {
    const outputs = JSON.stringify(template.toJSON().Outputs ?? {});
    for (const name of [
      "isol8-dev-database:ExportsOutputFnGetAttCreditsTable13AC5EA8ArnB299C72C",
      "isol8-dev-database:ExportsOutputRefCreditsTable13AC5EA80B87C786",
      "isol8-dev-database:ExportsOutputFnGetAttCreditTransactionsTable72EF769AArn60F8FDA5",
      "isol8-dev-database:ExportsOutputRefCreditTransactionsTable72EF769A0289C51E",
    ]) {
      expect(outputs).toContain(name);
    }
  });
});
