import { App, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { LightbulbStage } from "../lib/lightbulb-stage";
import { HOSTED_ZONE_CONTEXT } from "./helpers";

function backendEnv(environment: string): Record<string, string> {
  const app = new App({ context: HOSTED_ZONE_CONTEXT });
  const stage = new LightbulbStage(app, environment, {
    environment,
    env: { account: "877352799272", region: "us-east-1" },
  });
  const service = stage.node.children.find(
    (c): c is Stack => c instanceof Stack && c.stackName === `lightbulb-${environment}-service`,
  )!;
  const backend = Object.values(Template.fromStack(service).findResources("AWS::ECS::TaskDefinition"))
    .flatMap((td: any) => td.Properties.ContainerDefinitions)
    .find((c: any) => c.Name === "backend");
  return Object.fromEntries(backend.Environment.map((e: any) => [e.Name, e.Value]));
}

// One LiveKit project serves both envs; dev must dispatch to its Mac worker, never prod's lightbulb-huddle.
test.each([
  ["dev", "lightbulb-meet-dev", ""],
  ["prod", undefined, "+14842079294"],
])("%s backend LiveKit env", (environment, agentName, voiceLine) => {
  const env = backendEnv(environment);
  expect(env.LIVEKIT_URL).toBe("wss://lightbulb-svx3a1rx.livekit.cloud");
  expect(env.LIVEKIT_AGENT_NAME).toBe(agentName);
  expect(env.VOICE_LINE_NUMBER).toBe(voiceLine); // the one phone line: prod only
});
