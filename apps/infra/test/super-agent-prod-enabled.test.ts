import { App, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { superAgentEnabled } from "../lib/super-agent";
import { HOSTED_ZONE_CONTEXT } from "./helpers";

function synthProd(): Template[] {
  const app = new App({ context: HOSTED_ZONE_CONTEXT });
  const stage = new LightbulbStage(app, "prod", {
    environment: "prod",
    env: { account: "877352799272", region: "us-east-1" },
  });
  return stage.node.children.filter((c): c is Stack => c instanceof Stack).map((s) => Template.fromStack(s));
}

describe("super-agent gate (prod enabled)", () => {
  it("is on for dev and prod", () => {
    expect(superAgentEnabled("dev")).toBe(true);
    expect(superAgentEnabled("prod")).toBe(true);
  });

  it("prod synth has the env master, Temporal namespace and an on-demand worker", () => {
    const templates = synthProd();
    const json = templates.map((t) => JSON.stringify(t.toJSON())).join("\n");
    expect(json).toMatch(/"Name":"SUPER_AGENT_ENV_ENABLED","Value":"true"/);
    expect(json).toMatch(/"Name":"TEMPORAL_NAMESPACE","Value":"lightbulb-prod\.nv5xx"/);

    const services = templates.flatMap((t) =>
      Object.values(t.findResources("AWS::ECS::Service")),
    ) as { Properties: { ServiceName?: string; CapacityProviderStrategy?: { CapacityProvider: string }[] } }[];
    for (const name of ["lightbulb-prod-temporal-worker", "lightbulb-prod-temporal-watchdog"]) {
      const svc = services.find((s) => s.Properties.ServiceName === name);
      expect(svc).toBeDefined();
      expect(JSON.stringify(svc!.Properties.CapacityProviderStrategy ?? [])).not.toMatch(/FARGATE_SPOT/);
    }
  });
});
