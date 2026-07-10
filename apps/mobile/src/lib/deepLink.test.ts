import { parseAgentDeepLink } from "./deepLink";

describe("parseAgentDeepLink", () => {
  it("parses an agent deep link", () => {
    expect(parseAgentDeepLink("isol8://agent/agent_123")).toEqual({ agentId: "agent_123" });
  });
  it("parses with a query string", () => {
    expect(parseAgentDeepLink("isol8://agent/agent_123?session=s1")).toEqual({ agentId: "agent_123" });
  });
  it("returns null for the oauth callback", () => {
    expect(parseAgentDeepLink("isol8://oauth-native-callback")).toBeNull();
  });
  it("returns null for junk", () => {
    expect(parseAgentDeepLink("https://example.com")).toBeNull();
    expect(parseAgentDeepLink("isol8://agent/")).toBeNull();
  });
});
