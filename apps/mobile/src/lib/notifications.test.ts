import { shouldSuppressBanner } from "./notifications";

describe("shouldSuppressBanner", () => {
  it("suppresses when viewing the same agent's chat", () => {
    expect(shouldSuppressBanner("agent_1", "agent_1")).toBe(true);
  });
  it("shows when on a different agent", () => {
    expect(shouldSuppressBanner("agent_2", "agent_1")).toBe(false);
  });
  it("shows when not on any agent chat", () => {
    expect(shouldSuppressBanner(null, "agent_1")).toBe(false);
  });
  it("shows when the notif carries no agent id", () => {
    expect(shouldSuppressBanner("agent_1", undefined)).toBe(false);
  });
});
