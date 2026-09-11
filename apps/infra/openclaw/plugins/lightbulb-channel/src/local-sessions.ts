import type { OpenClawPluginToolContext, AnyAgentTool } from "openclaw/plugin-sdk/core";

type Binding = Readonly<{ grant: string; ownerId: string; roomId: string; agentId: string; sessionKey: string; expiresAt: number }>;
type NativeNode = { nodeId: string; clientId?: string; connected?: boolean };
type Deps = {
  env: () => Record<string, string | undefined>;
  fetch: typeof fetch;
  nodes: () => Promise<{ nodes: NativeNode[] }>;
};
const unavailable = () => new Error("Lightbulb local sessions unavailable");
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** JWT projection only; authenticity is checked independently by the backend on every call. */
export function invocationBinding(token: unknown, agent: string, room: string, event?: string): Binding | null {
  try {
    if (typeof token !== "string" || token.length > 4096 || token.split(".").length !== 3) return null;
    const c = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString("utf8"));
    const now = Math.floor(Date.now() / 1000);
    if (Object.keys(c).sort().join() !== ["kind", "aud", "ownerId", "roomId", "agentId", "eventId", "sessionKey", "iat", "exp", "jti"].sort().join()
      || c.kind !== "lightbulb_local_session_invocation" || c.aud !== "lightbulb:local-sessions"
      || !Number.isSafeInteger(c.iat) || !Number.isSafeInteger(c.exp) || c.iat > now || c.exp <= now || c.exp > c.iat + 300
      || typeof c.ownerId !== "string" || !/^[A-Za-z0-9_-]{1,256}$/.test(c.ownerId)
      || c.agentId !== agent || c.roomId !== room || (event !== undefined && c.eventId !== event)
      || typeof c.eventId !== "string" || !UUID.test(c.eventId) || typeof c.jti !== "string" || !UUID.test(c.jti)
      || !UUID.test(room) || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(agent)
      || c.sessionKey !== `agent:${agent}:lightbulb:channel:${room}`) return null;
    return Object.freeze({ grant: token, ownerId: c.ownerId, roomId: room, agentId: agent, sessionKey: c.sessionKey, expiresAt: c.exp });
  } catch { return null; }
}

export function localSessionTool(ctx: Partial<OpenClawPluginToolContext>, deps: Deps): AnyAgentTool | null {
  const candidate = ctx.toolBindings?.lightbulbLocalSessions as Binding | undefined;
  const binding = invocationBinding(candidate?.grant, ctx.agentId ?? "", ctx.nativeChannelId ?? "");
  if (!binding || ctx.messageChannel !== "lightbulb" || ctx.agentAccountId !== binding.agentId
    || ctx.sessionKey !== binding.sessionKey || deps.env().ISOL8_OWNER_ID !== binding.ownerId) return null;
  // Closure captures a fresh immutable projection, never mutable plugin-global authority.
  return {
    name: "lightbulb_local_sessions", label: "Room local sessions",
    description: "Discover/read/send to this room's human-shared local sessions. Resources also reports advisory eligible connected nodes; use ordinary native node tools separately. Native provider approvals remain unchanged. Tools expire until a new room ingress. No room-history injection.",
    parameters: {
      type: "object", additionalProperties: false, required: ["action"],
      properties: {
        action: { type: "string", enum: ["resources", "read", "send", "receipt"] },
        sessionId: { type: "string" }, requestId: { type: "string", maxLength: 256 },
        expectedGeneration: { type: "string", maxLength: 256 }, text: { type: "string", maxLength: 8192 },
        afterSeq: { type: "integer", minimum: 0 }, limit: { type: "integer", minimum: 1, maximum: 100 },
      },
    },
    execute: async (_id, args) => {
      try {
        if (binding.expiresAt <= Math.floor(Date.now() / 1000) || deps.env().ISOL8_OWNER_ID !== binding.ownerId
          || !args || typeof args !== "object" || Array.isArray(args)) throw unavailable();
        const a = args as Record<string, unknown>;
        const allowed: Record<string, string[]> = { resources: ["action"], read: ["action", "sessionId", "afterSeq", "limit"],
          send: ["action", "sessionId", "requestId", "expectedGeneration", "text"], receipt: ["action", "sessionId", "requestId"] };
        if (typeof a.action !== "string" || !allowed[a.action] || Object.keys(a).some((k) => !allowed[a.action as string].includes(k))) throw unavailable();
        if (a.action !== "resources" && (typeof a.sessionId !== "string" || !/^[a-f0-9-]{36}$/.test(a.sessionId))) throw unavailable();
        const opaque = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_.:-]{1,256}$/.test(v);
        if (["send", "receipt"].includes(a.action) && !opaque(a.requestId)) throw unavailable();
        let suffix = "/resources";
        let body: string | undefined;
        if (a.action === "read") {
          const after = a.afterSeq ?? 0, limit = a.limit ?? 50;
          if (!Number.isSafeInteger(after) || Number(after) < 0 || !Number.isSafeInteger(limit) || Number(limit) < 1 || Number(limit) > 100) throw unavailable();
          suffix = `/sessions/${a.sessionId}/events?afterSeq=${after}&limit=${limit}`;
        } else if (a.action === "receipt") suffix = `/sessions/${a.sessionId}/commands/${encodeURIComponent(a.requestId as string)}`;
        else if (a.action === "send") {
          if (!opaque(a.expectedGeneration) || typeof a.text !== "string" || !a.text.trim()) throw unavailable();
          body = JSON.stringify({ requestId: a.requestId, expectedGeneration: a.expectedGeneration, kind: "send", text: a.text });
          if (Buffer.byteLength(body) > 8192) throw unavailable();
          suffix = `/sessions/${a.sessionId}/commands`;
        }
        const api = new URL(deps.env().ISOL8_API_URL ?? "");
        if (api.protocol !== "https:" || api.username || api.password || api.search || api.hash || api.pathname.replace(/\/$/, "") !== "") throw unavailable();
        const response = await deps.fetch(`${api.origin}/api/v1/messaging/local-session-agent${suffix}`, {
          method: body ? "POST" : "GET", headers: { Authorization: `Bearer ${binding.grant}`, "Content-Type": "application/json" },
          body, signal: AbortSignal.timeout(10_000), redirect: "error",
        });
        if (!response.ok || !response.body) throw unavailable();
        const reader = response.body.getReader();
        let bytes = 0; const chunks: Uint8Array[] = [];
        try {
          while (true) { const part = await reader.read(); if (part.done) break;
            bytes += part.value.byteLength; if (bytes > 2 * 1024 * 1024) throw unavailable(); chunks.push(part.value); }
        } finally { await reader.cancel(); }
        const details = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (a.action === "resources") {
          // DB presence is not native availability and preference is not invocation authorization.
          let preference: { advisory: true; availability: string; nodes: NativeNode[] } = { advisory: true, availability: "unknown", nodes: [] };
          try {
            let timer: ReturnType<typeof setTimeout> | undefined;
            const current = await Promise.race([deps.nodes(), new Promise<never>((_, reject) => {
              timer = setTimeout(() => reject(unavailable()), 5_000);
            })]).finally(() => clearTimeout(timer));
            const eligible = new Map((details.pairedNodes ?? []).map((p: { deviceId: string; clientId: string }) => [p.deviceId, p.clientId]));
            preference = { advisory: true, availability: "known", nodes: current.nodes
              .filter((n) => n.connected === true && ["openclaw-macos", "node-host"].includes(n.clientId ?? "") && eligible.get(n.nodeId) === n.clientId)
              .map((n) => ({ nodeId: n.nodeId, clientId: n.clientId, connected: true })) };
          } catch { /* Unknown native availability cannot produce a positive recommendation. */ }
          details.nodePreference = preference;
        }
        return { content: [{ type: "text", text: JSON.stringify(details) }], details };
      } catch { throw unavailable(); }
    },
  };
}
