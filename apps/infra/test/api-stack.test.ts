import { Match, Template } from "aws-cdk-lib/assertions";
import { synthApiStack } from "./helpers";

// Hoist a single ApiStack synthesis and reuse across tests.
const API_TEMPLATE = synthApiStack();

describe("ApiStack — ws-connections owner-keyed fan-out", () => {
  const template: Template = API_TEMPLATE;

  test("ws-connections has a by-owner-id GSI projecting userId + connectionType", () => {
    // Owner-keyed fan-out reads recipients from this GSI: given an ownerId
    // (org_id for orgs, user_id for personal), return every active connection
    // for that owner. INCLUDE projection carries the member userId (per-member
    // demux) + connectionType (chat filter excludes node/operator pipes).
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-owner-id",
          KeySchema: [{ AttributeName: "ownerId", KeyType: "HASH" }],
          Projection: {
            ProjectionType: "INCLUDE",
            NonKeyAttributes: Match.arrayWith(["userId", "connectionType"]),
          },
        }),
      ]),
    });
  });
});
