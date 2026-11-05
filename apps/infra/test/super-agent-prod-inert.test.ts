import { App, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { superAgentEnabled } from "../lib/super-agent";
import { HOSTED_ZONE_CONTEXT } from "./helpers";

/**
 * Spec 09 G1: prod synthesizes nothing of the super-agent program until a founder-approved rollout flips the
 * gate. There are no super-agent resources yet; this pins the gate so the first one lands behind it.
 */
const SUPER_AGENT = /super[-_]?agent/i;

function synthStage(environment: "dev" | "prod"): string[] {
  const app = new App({ context: HOSTED_ZONE_CONTEXT });
  const stage = new LightbulbStage(app, environment, {
    environment,
    env: { account: "877352799272", region: "us-east-1" },
  });
  return stage.node.children
    .filter((c): c is Stack => c instanceof Stack)
    .map((s) => JSON.stringify(Template.fromStack(s).toJSON()));
}

describe("super-agent gate", () => {
  it("is on for dev only", () => {
    expect(superAgentEnabled("dev")).toBe(true);
    expect(superAgentEnabled("prod")).toBe(false);
  });

  it("prod synth carries no super-agent resource or env var", () => {
    for (const template of synthStage("prod")) {
      expect(template).not.toMatch(SUPER_AGENT);
    }
  });

  it("prod synth grants nothing on per-task overlays (<owner>/hermes-tasks/); dev does", () => {
    // Matches any resource, statement or ARN naming the prefix, whatever its Sid.
    const OVERLAYS = /hermes-tasks/;
    for (const template of synthStage("prod")) {
      expect(template).not.toMatch(OVERLAYS);
    }
    expect(synthStage("dev").join("\n")).toMatch(OVERLAYS);
  });

  it("dev's backend task gets the env master", () => {
    expect(synthStage("dev").join("\n")).toMatch(/"Name":"SUPER_AGENT_ENV_ENABLED","Value":"true"/);
  });
});
