import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const template = readFileSync(resolve(__dirname, "../aws/aws-connector.yaml"), "utf8");

describe("AWS connector EventBridge API Destination permissions", () => {
  it("pins the invoke role and connector permissions to EventBridge", () => {
    expect(template).toContain("RoleName: isol8-eventbridge-api-destination");
    expect(template).toContain("Service: events.amazonaws.com");
    expect(template).toContain("Action: events:InvokeApiDestination");
    expect(template).toContain(
      "Resource: !Sub arn:${AWS::Partition}:events:*:${AWS::AccountId}:api-destination/isol8-*/*",
    );
    expect(template).toContain("Action: iam:PassRole");
    expect(template).toContain("iam:PassedToService: events.amazonaws.com");
    expect(template).toContain("iam:AWSServiceName: apidestinations.events.amazonaws.com");
  });
});
