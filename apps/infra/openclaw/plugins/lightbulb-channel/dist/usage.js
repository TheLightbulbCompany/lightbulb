import { usageFromEntry } from "./runs.js";
const ROOM_SESSION_RE = /^agent:[^:]+:lightbulb:channel:[0-9a-f-]{36}$/;
/** `agent_end` for every run the room contract does not cover (cron, heartbeat, Paperclip). */
export function createUsageHook(deps) {
    return async (event, ctx) => {
        const { agentId, sessionKey } = ctx;
        if (!event.runId || !agentId || !sessionKey || ROOM_SESSION_RE.test(sessionKey))
            return;
        try {
            const usage = usageFromEntry(deps.getSessionEntry({ agentId, sessionKey }));
            // `event.error` is deliberately not forwarded: it can carry model output.
            await deps.post("/api/v1/internal/messaging/usage", {
                sessionKey, agentId, runId: event.runId, success: event.success,
                ...(typeof event.durationMs === "number" ? { durationMs: event.durationMs } : {}),
                usage,
            });
        }
        catch (error) {
            deps.log?.(`lightbulb: usage hook failed (${error instanceof Error ? error.name : "error"})`);
        }
    };
}
