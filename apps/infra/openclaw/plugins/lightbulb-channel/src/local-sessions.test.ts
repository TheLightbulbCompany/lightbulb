import { describe, expect, it, vi } from "vitest";
import { invocationBinding, localSessionTool } from "./local-sessions.js";

const agent = "11111111-1111-4111-8111-111111111111";
const room = "22222222-2222-4222-8222-222222222222";
const event = "33333333-3333-4333-8333-333333333333";
const sessionKey = `agent:${agent}:lightbulb:channel:${room}`;
function grant(changes = {}) {
  const now = Math.floor(Date.now() / 1000);
  const claims = { kind: "lightbulb_local_session_invocation", aud: "lightbulb:local-sessions",
    ownerId: "owner_test", roomId: room, agentId: agent, eventId: event, sessionKey,
    iat: now, exp: now + 300, jti: event, ...changes };
  return `eyJhbGciOiJIUzI1NiJ9.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.sentinel-signature`;
}
const env = { ISOL8_API_URL: "https://api.flickmeon.ai", ISOL8_OWNER_ID: "owner_test" };
function context(token = grant()) {
  return { agentId: agent, agentAccountId: agent, messageChannel: "lightbulb", nativeChannelId: room,
    sessionKey, toolBindings: { lightbulbLocalSessions: invocationBinding(token, agent, room, event) } };
}

describe("room native tool binding", () => {
  it("fails closed without exact native scope and never places the grant in its schema", () => {
    const deps = { env: () => env, fetch: vi.fn(), nodes: vi.fn() };
    expect(localSessionTool({}, deps)).toBeNull();
    for (const changes of [{ agentId: "other" }, { agentAccountId: "other" }, { sessionKey: "other" },
      { nativeChannelId: "other" }, { messageChannel: "other" }, { toolBindings: {} }]) {
      expect(localSessionTool({ ...context(), ...changes }, deps)).toBeNull();
    }
    expect(localSessionTool(context(grant({ exp: 1 })), deps)).toBeNull();
    const token = grant();
    const tool = localSessionTool(context(token), deps)!;
    expect(JSON.stringify(tool)).not.toContain(token);
    expect(JSON.stringify(tool.parameters)).not.toMatch(/ownerId|roomId|grant|actor/);
  });

  it("intersects exact current nodes and strips arbitrary metadata; failures make no recommendation", async () => {
    const pairs = ["a", "b", "c", "d"].map((deviceId) => ({ deviceId, clientId: "node-host", humanUserId: "user" }));
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ sessions: [], devices: [], pairedNodes: pairs })));
    const nodes = vi.fn(async () => ({ nodes: [
      { nodeId: "a", clientId: "node-host", connected: true, remoteIp: "private-sentinel" },
      { nodeId: "b", clientId: "openclaw-macos", connected: true },
      { nodeId: "c", clientId: "node-host", connected: false },
      { nodeId: "wider", clientId: "node-host", connected: true },
    ] }));
    const token = grant();
    const tool = localSessionTool(context(token), { env: () => env, fetch: fetcher, nodes })!;
    const result = await tool.execute("call", { action: "resources" });
    expect(result.details.nodePreference).toEqual({ advisory: true, availability: "known", nodes: [{ nodeId: "a", clientId: "node-host", connected: true }] });
    expect(JSON.stringify(result)).not.toMatch(/private-sentinel|wider/);
    expect(fetcher.mock.calls[0][1].headers.Authorization).toBe(`Bearer ${token}`);
    nodes.mockRejectedValueOnce(new Error("secret-sentinel"));
    const unknown = await tool.execute("call", { action: "resources" });
    expect(unknown.details.nodePreference).toEqual({ advisory: true, availability: "unknown", nodes: [] });
    expect(JSON.stringify(unknown)).not.toContain("secret-sentinel");
  });

  it("rejects model authority fields and rechecks expiry at execution", async () => {
    vi.useFakeTimers();
    try {
      const fetcher = vi.fn();
      const tool = localSessionTool(context(), { env: () => env, fetch: fetcher, nodes: vi.fn() })!;
      await expect(tool.execute("call", { action: "resources", roomId: room })).rejects.toThrow("unavailable");
      vi.advanceTimersByTime(301_000);
      await expect(tool.execute("call", { action: "resources" })).rejects.toThrow("unavailable");
      expect(fetcher).not.toHaveBeenCalled();
    } finally { vi.useRealTimers(); }
  });
});
