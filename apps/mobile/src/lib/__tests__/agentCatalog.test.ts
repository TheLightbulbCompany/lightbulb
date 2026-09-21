import { buildModelNameMap, buildSelectorModels, listCatalogModels } from "../agentCatalog";

const ARN = "arn:aws:bedrock:us-east-1:1:application-inference-profile/son";

/**
 * M6 task 10-C (scan row 31). On AgentCore the picker's source is the `models.list` payload —
 * a Hermes config has no `models.providers` catalog to walk, which is why the phone's picker
 * rendered "No models are configured in the gateway" on every migrated workspace.
 */
describe("agentCatalog over the models.list payload", () => {
  const list = {
    models: [
      { id: ARN, name: "Claude Sonnet", provider: "bedrock", tier: "standard" },
      { id: "claude-opus-5", provider: "anthropic" },
    ],
    defaultId: ARN,
  };

  it("names an AIP ARN and offers each id verbatim as the ref to send back", () => {
    expect(buildModelNameMap(list)).toEqual({ [ARN]: "Claude Sonnet" });
    expect(listCatalogModels(list)).toEqual([
      { ref: ARN, name: "Claude Sonnet" },
      { ref: "claude-opus-5", name: "claude-opus-5" },
    ]);
  });

  it("uses only the server-approved managed model list", () => {
    const managed = {
      models: [{ id: "deepseek/deepseek-v4", name: "DeepSeek V4", provider: "custom" }],
      defaultId: "deepseek/deepseek-v4",
    };
    expect(listCatalogModels(managed)).toEqual([
      { ref: "deepseek/deepseek-v4", name: "DeepSeek V4" },
    ]);
  });

  it("still pins the agent's current model to the front when the list omits it", () => {
    const [first] = buildSelectorModels(list, "retired-model");
    expect(first.ref).toBe("retired-model");
  });

  it("leaves the ECS providers-catalog shape working", () => {
    const config = { models: { providers: { "amazon-bedrock": { models: [{ id: ARN, name: "Claude Opus 4.6" }] } } } };
    expect(listCatalogModels(config)).toEqual([{ ref: `amazon-bedrock/${ARN}`, name: "Claude Opus 4.6" }]);
  });
});
