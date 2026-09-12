/**
 * The async turn contract on the container side (spec §4/§5): one supervisor per
 * gateway process owns every in-flight room run, mirrors the run's agent events to
 * AppSync as the frame vocabulary clients already consume, and reports lifecycle and
 * usage to the backend over the hook-token route.
 */
import type { FramePublisher, LiveFrameEvent } from "./appsync.js";
import { IngressRejected, resolveEnv, type AmbientGateOutcome, type FetchLike, type RawEnv } from "./channel.js";

export const HEARTBEAT_MS = 30_000;
const CALLBACK_ATTEMPTS = 3;
const CALLBACK_TIMEOUT_MS = 10_000;

export type RunOutcome = "completed" | "error" | "aborted" | "ignored" | "gate_error";
export type RunUsage = { model: string; inputTokens: number; outputTokens: number; cacheReadTokens: number; cacheWriteTokens: number; estimatedCostUsd: number | null };
export type RunEvent = { runId: string; type: "run.started" | "run.heartbeat" | "run.ended"; at: string; sessionKey?: string; outcome?: RunOutcome; errorKind?: string; usage?: RunUsage };
export type RunCallbacks = { send(event: RunEvent): Promise<void> };
export type AgentEvent = { runId: string; stream: string; data: Record<string, unknown>; sessionKey?: string };
/** The session-store row the gateway patches at run end (sessions.list reads the same fields). */
export type SessionUsageEntry = { model?: string; inputTokens?: number; outputTokens?: number; cacheRead?: number; cacheWrite?: number; estimatedCostUsd?: number };
export type RunSdk = {
  onAgentEvent: (listener: (evt: AgentEvent) => void) => () => void;
  resolveActiveEmbeddedRunSessionId: (sessionKey: string) => string | undefined;
  abortEmbeddedAgentRun: (sessionId: string) => boolean;
  queueAgentHarnessMessage: (sessionId: string, text: string) => boolean;
  getSessionEntry: (params: { agentId: string; sessionKey: string }) => SessionUsageEntry | undefined;
};
export type RunStart = {
  runId: string;
  roomId: string;
  agentId: string;
  sessionKey: string;
  frameChannel: string;
  frameEndpoint: string;
  threadRootMessageId: string | null;
  eventKind: "user_request" | "room_event";
};
type Frame = Record<string, unknown> & { type: string };
type ActiveRun = RunStart & {
  started: boolean;
  heartbeat?: ReturnType<typeof setInterval>;
  text: { done: string; current: string };
  terminal?: { phase: "end" | "error"; aborted: boolean; error?: string; stopReason?: string };
};

/** Port of the backend's GatewayConnection._transform_agent_event (the frames clients render). */
export function transformAgentEvent(stream: string, data: Record<string, unknown>): Frame | null {
  const text = typeof data.text === "string" ? data.text : "";
  const delta = typeof data.delta === "string" ? data.delta : "";
  const textFrame = (type: string): Frame | null => {
    if (data.replace !== true && delta) return { type, content: delta, append: true };
    return text ? { type, content: text } : null;
  };
  if (stream === "assistant") return textFrame("chunk");
  if (stream === "reasoning" || stream === "thinking") return textFrame("thinking");
  if (stream === "tool") {
    const phase = data.phase;
    const name = typeof data.name === "string" ? data.name : "";
    const toolCallId = typeof data.toolCallId === "string" ? data.toolCallId : "";
    if (phase === "update") {
      if (!toolCallId) return null;
      return { type: "tool_progress", toolCallId, ...(name ? { tool: name } : {}), ...("partialResult" in data ? { partialResult: data.partialResult } : {}) };
    }
    if (!name) return null;
    if (phase === "start") return { type: "tool_start", tool: name, ...(toolCallId ? { toolCallId } : {}), ...("args" in data ? { args: data.args } : {}) };
    if (phase === "result") {
      return {
        type: data.isError ? "tool_error" : "tool_end",
        tool: name,
        ...(toolCallId ? { toolCallId } : {}),
        ...("result" in data ? { result: data.result } : {}),
        ...(data.meta ? { meta: data.meta } : {}),
      };
    }
    return null;
  }
  if (stream === "command_output") {
    const toolCallId = typeof data.toolCallId === "string" ? data.toolCallId : "";
    return toolCallId ? { type: "command_output", toolCallId, ...("output" in data ? { output: data.output } : {}) } : null;
  }
  if (stream === "run_status" || stream === "compaction") {
    const phase = typeof data.phase === "string" ? data.phase : "";
    return phase ? { type: "agent_activity", activity: stream, title: phase, detail: data } : null;
  }
  if (stream === "plan" || stream === "patch" || stream === "item" || stream === "acp") {
    return { type: "agent_activity", activity: stream, title: typeof data.title === "string" ? data.title : "", detail: data };
  }
  return null; // approval and lifecycle are consumed by the supervisor itself
}

export function createRunCallbacks(deps: { fetch: FetchLike; env: () => RawEnv; log?: (message: string) => void }): RunCallbacks {
  return {
    async send(event) {
      const resolved = resolveEnv(deps.env());
      const url = `${resolved.base}/api/v1/internal/messaging/runs/${encodeURIComponent(resolved.ownerId)}/events`;
      for (let attempt = 0; attempt < CALLBACK_ATTEMPTS; attempt++) {
        try {
          const res = await deps.fetch(url, {
            method: "POST",
            headers: { "content-type": "application/json", Authorization: `Bearer ${resolved.token}` },
            body: JSON.stringify(event),
            signal: AbortSignal.timeout(CALLBACK_TIMEOUT_MS),
          });
          if (res.status < 500) return; // 200 applied/replayed and 404 unknown are both final
        } catch {
          // network failure: retry below
        }
        await new Promise((resolve) => setTimeout(resolve, 500 * (attempt + 1)));
      }
      deps.log?.(`lightbulb: run callback ${event.type} undelivered for ${event.runId}`);
    },
  };
}

const asCount = (value: unknown) => (typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.round(value) : 0);

export function createRunSupervisor(deps: {
  sdk: RunSdk;
  callbacks: RunCallbacks;
  frames: FramePublisher;
  uuid: () => string;
  now: () => Date;
  log?: (message: string) => void;
}) {
  const runs = new Map<string, ActiveRun>();
  const bySession = new Map<string, string>();
  let unsubscribe: (() => void) | undefined;

  const iso = () => deps.now().toISOString();
  function frame(run: ActiveRun, body: Frame): void {
    const payload: { frame: Frame } & Record<string, unknown> = {
      roomId: run.roomId,
      agentId: run.agentId,
      runId: run.runId,
      frame: { ...body, runId: run.runId, agentId: run.agentId },
    };
    if (run.threadRootMessageId) payload.threadRootMessageId = run.threadRootMessageId;
    const event = { schemaVersion: 1, eventId: deps.uuid(), type: "room.run.frame", occurredAt: iso(), roomId: run.roomId, payload } as LiveFrameEvent;
    deps.frames.push(run.frameEndpoint, run.frameChannel, event);
  }
  function markStarted(run: ActiveRun): void {
    if (run.started) return;
    run.started = true;
    void deps.callbacks.send({ runId: run.runId, type: "run.started", at: iso(), sessionKey: run.sessionKey });
    frame(run, { type: "run_active", active: true });
    run.heartbeat = setInterval(() => void deps.callbacks.send({ runId: run.runId, type: "run.heartbeat", at: iso() }), HEARTBEAT_MS);
  }
  function trackText(run: ActiveRun, data: Record<string, unknown>): void {
    // Upstream clears `text` at every message start, so a text that no longer extends
    // the current message is a new message: bank the old one for the final resync.
    const text = typeof data.text === "string" ? data.text : "";
    const delta = typeof data.delta === "string" ? data.delta : "";
    if (run.text.current && text && !text.startsWith(run.text.current)) {
      run.text.done += run.text.current;
      run.text.current = "";
    }
    run.text.current = data.replace !== true && delta ? (text || run.text.current + delta) : text || run.text.current;
  }
  function usageFor(run: ActiveRun): RunUsage {
    const entry = deps.sdk.getSessionEntry({ agentId: run.agentId, sessionKey: run.sessionKey }) ?? {};
    const cost = entry.estimatedCostUsd;
    return {
      model: typeof entry.model === "string" && entry.model ? entry.model : "unknown",
      inputTokens: asCount(entry.inputTokens),
      outputTokens: asCount(entry.outputTokens),
      cacheReadTokens: asCount(entry.cacheRead),
      cacheWriteTokens: asCount(entry.cacheWrite),
      estimatedCostUsd: typeof cost === "number" && Number.isFinite(cost) && cost > 0 ? cost : null,
    };
  }
  function onEvent(evt: AgentEvent): void {
    const runId = evt.sessionKey ? bySession.get(evt.sessionKey) : undefined;
    const run = runId ? runs.get(runId) : undefined;
    if (!run) return;
    if (evt.stream === "lifecycle") {
      const phase = evt.data.phase;
      if (phase === "start") markStarted(run);
      else if (phase === "end" || phase === "error") {
        run.terminal = {
          phase,
          aborted: evt.data.aborted === true,
          error: typeof evt.data.error === "string" ? evt.data.error : undefined,
          stopReason: typeof evt.data.stopReason === "string" ? evt.data.stopReason : undefined,
        };
      }
      return;
    }
    markStarted(run); // any event for the session proves the run is live
    const transformed = transformAgentEvent(evt.stream, evt.data);
    if (!transformed) return;
    if (transformed.type === "chunk") trackText(run, evt.data);
    frame(run, transformed);
  }
  async function finish(run: ActiveRun, gate?: "ignored" | "gate_error", failure?: unknown): Promise<void> {
    if (run.heartbeat) clearInterval(run.heartbeat);
    let outcome: RunOutcome;
    let errorKind: string | undefined;
    let message = "Agent run failed";
    if (gate) outcome = gate;
    else if (run.terminal?.aborted) {
      outcome = "aborted";
      message = "Agent run was cancelled";
    } else if (failure !== undefined || run.terminal?.phase === "error") {
      outcome = "error";
      errorKind = run.terminal?.stopReason ?? (failure instanceof IngressRejected ? "dispatch_failed" : "unknown");
      message = run.terminal?.error ?? (failure instanceof Error ? failure.message : message);
    } else outcome = "completed";
    if (run.started) {
      if (outcome === "completed") {
        const text = run.text.done + run.text.current;
        if (text) frame(run, { type: "chunk", content: text });
        frame(run, { type: "done" });
      } else if (outcome === "error" || outcome === "aborted") {
        frame(run, { type: "error", message, ...(errorKind ? { code: errorKind } : {}) });
      }
      frame(run, { type: "run_ended", outcome: outcome === "completed" ? "done" : outcome });
      frame(run, { type: "run_active", active: false });
      await deps.frames.flush(run.frameChannel);
    }
    runs.delete(run.runId);
    if (bySession.get(run.sessionKey) === run.runId) bySession.delete(run.sessionKey);
    await deps.callbacks.send({
      runId: run.runId,
      type: "run.ended",
      at: iso(),
      outcome,
      ...(errorKind ? { errorKind } : {}),
      ...(gate ? {} : { usage: usageFor(run) }),
    });
  }
  function sessionIdFor(runId: string): string | undefined {
    const run = runs.get(runId);
    return run ? deps.sdk.resolveActiveEmbeddedRunSessionId(run.sessionKey) : undefined;
  }

  return {
    /** The ack has been written; run the turn to completion in the background. */
    start(spec: RunStart, turn: () => Promise<AmbientGateOutcome>): void {
      unsubscribe ??= deps.sdk.onAgentEvent(onEvent);
      const run: ActiveRun = { ...spec, started: false, text: { done: "", current: "" } };
      runs.set(spec.runId, run);
      bySession.set(spec.sessionKey, spec.runId);
      void turn().then(
        (gate) => finish(run, gate === "dispatched" ? undefined : gate),
        (failure) => finish(run, undefined, failure),
      );
    },
    abort(runId: string): "aborting" | "not_found" {
      const sessionId = sessionIdFor(runId);
      return sessionId && deps.sdk.abortEmbeddedAgentRun(sessionId) ? "aborting" : "not_found";
    },
    // ponytail: queueAgentHarnessMessage is deprecated upstream; if it stops queuing,
    // dispatch the steer text as a second inbound turn on the same session instead.
    steer(runId: string, text: string): "accepted" | "not_found" {
      const sessionId = sessionIdFor(runId);
      return sessionId && deps.sdk.queueAgentHarnessMessage(sessionId, text) ? "accepted" : "not_found";
    },
    runIdForSession(sessionKey: string): string | undefined {
      return bySession.get(sessionKey);
    },
  };
}
export type RunSupervisor = ReturnType<typeof createRunSupervisor>;
