import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as kms from "aws-cdk-lib/aws-kms";
import { Template } from "aws-cdk-lib/assertions";
import { DatabaseStack } from "../lib/stacks/database-stack";

test("managed OpenRouter owner claims have a durable, encrypted table", () => {
  const app = new cdk.App();
  const env = { account: "877352799272", region: "us-east-1" };
  const support = new cdk.Stack(app, "Support", { env });
  const stack = new DatabaseStack(app, "Database", {
    env,
    environment: "prod",
    kmsKey: new kms.Key(support, "Key"),
  });
  const template = Template.fromStack(stack);
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    TableName: "lightbulb-prod-openrouter-keys",
    KeySchema: [{ AttributeName: "owner_id", KeyType: "HASH" }],
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
  });
  const tables = template.findResources("AWS::DynamoDB::Table", {
    Properties: { TableName: "lightbulb-prod-openrouter-keys" },
  });
  expect(Object.values(tables)[0].DeletionPolicy).toBe("Retain");

  template.hasResourceProperties("AWS::DynamoDB::Table", {
    TableName: "lightbulb-prod-openrouter-requests",
    KeySchema: [{ AttributeName: "request_id", KeyType: "HASH" }],
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    GlobalSecondaryIndexes: [{
      IndexName: "state-created-index",
      KeySchema: [
        { AttributeName: "state", KeyType: "HASH" },
        { AttributeName: "created_at", KeyType: "RANGE" },
      ],
      Projection: { ProjectionType: "ALL" },
    }],
  });
});
