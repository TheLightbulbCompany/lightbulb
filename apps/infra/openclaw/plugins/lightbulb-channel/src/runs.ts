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
/**
 * `runId` is the HOST's run id, not ours. `sessionKey` is only populated for a surface the
 * control UI can see (and, for every surface, on `lifecycle`); `deliverySessionKey` is the
 * unblanked copy, but the host only attaches it when the run holds a context claim — so
 * neither is a reliable correlator on a streaming event. The host run id is.
 */
export type AgentEvent = {
  runId: string;
  stream: string;
  data: Record<string, unknown>;
  sessionKey?: string;
  deliverySessionKey?: string;
};
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
    // Never rejects: a lost callback is reported to the sweeper by silence, and a
    // throwing send would take the run's own terminal report down with it.
    async send(event) {
      for (let attempt = 0; attempt < CALLBACK_ATTEMPTS; attempt++) {
        try {
          // Inside the try: an unset ISOL8_API_URL/ISOL8_OWNER_ID/OPENCLAW_HOOK_TOKEN
          // throws LightbulbConfigurationError, which must not escape either.
          const resolved = resolveEnv(deps.env());
          const res = await deps.fetch(
            `${resolved.base}/api/v1/internal/messaging/runs/${encodeURIComponent(resolved.ownerId)}/events`,
            {
              method: "POST",
              headers: { "content-type": "application/json", Authorization: `Bearer ${resolved.token}` },
              body: JSON.stringify(event),
              signal: AbortSignal.timeout(CALLBACK_TIMEOUT_MS),
            },
          );
          if (res.status < 500) return; // 200 applied/replayed and 404 unknown are both final
        } catch {
          // configuration or network failure: retry below
        }
        if (attempt + 1 < CALLBACK_ATTEMPTS) {
          await new Promise((resolve) => setTimeout(resolve, 500 * (attempt + 1)));
        }
      }
      deps.log?.(`lightbulb: run callback ${event.type} undelivered for ${event.runId}`);
    },
  };
}

const asCount = (value: unknown) => (typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.round(value) : 0);
/** A run that never reached the host owns no tokens; the session row holds the last turn's. */
const ZERO_USAGE: RunUsage = { model: "unknown", inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, estimatedCostUsd: null };

export function createRunSupervisor(deps: {
  sdk: RunSdk;
  callbacks: RunCallbacks;
  frames: FramePublisher;
  uuid: () => string;
  now: () => Date;
  log?: (message: string) => void;
}) {
  const runs = new Map<string, ActiveRun>();
  /**
   * Session key -> the one run actually turning on it. The immediate ack means the FIFO
   * group no longer serializes two turns on one room session, so the supervisor serializes
   * them here instead: a second accept is parked in `queued` and only becomes this entry
   * when its predecessor finishes. So this always names the running turn — the one whose
   * durable final is being delivered, and hence the right run id to stamp on an outbound
   * reply, and the only correct fallback correlator in `onEvent`.
   */
  const bySession = new Map<string, string>();
  /** Session key -> turns accepted while that session was busy, oldest first. */
  const queued = new Map<string, Array<{ run: ActiveRun; turn: () => Promise<AmbientGateOutcome> }>>();
  /** Host run id -> our run id, learned from the first event that carries a session key. */
  const bySdkRun = new Map<string, string>();
  let unsubscribe: (() => void) | undefined;

  const iso = () => deps.now().toISOString();
  /** Error name and run id only — a callback body carries room text. */
  const logFailure = (runId: string, error: unknown) =>
    deps.log?.(`lightbulb: run ${runId} callback failed (${error instanceof Error ? error.name : "error"})`);
  /** Fire-and-forget: an unhandled rejection here would kill the gateway process. */
  const notify = (event: RunEvent): void => {
    void deps.callbacks.send(event).catch((error) => logFailure(event.runId, error));
  };
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
    notify({ runId: run.runId, type: "run.started", at: iso(), sessionKey: run.sessionKey });
    frame(run, { type: "run_active", active: true });
    run.heartbeat = setInterval(() => notify({ runId: run.runId, type: "run.heartbeat", at: iso() }), HEARTBEAT_MS);
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
    // `lifecycle/start` always carries a session key and always precedes the first
    // assistant/tool event, so it is what teaches us this host run id. Everything after
    // is matched on that id — a blanked session key would otherwise drop the whole
    // stream while lifecycle frames kept working, which fails silently.
    const key = evt.deliverySessionKey ?? evt.sessionKey;
    const runId = bySdkRun.get(evt.runId) ?? (key ? bySession.get(key) : undefined);
    const run = runId ? runs.get(runId) : undefined;
    if (!run) return;
    bySdkRun.set(evt.runId, run.runId);
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
    const transformed = transformAgentEvent(evt.stream, evt.data);
    if (!transformed) return;
    if (transformed.type === "chunk") trackText(run, evt.data);
    frame(run, transformed);
  }
  async function finish(run: ActiveRun, gate?: "ignored" | "gate_error", failure?: unknown, opts?: { neverLaunched: true }): Promise<void> {
    if (run.heartbeat) clearInterval(run.heartbeat);
    // Retire the run before the terminal frames: a late event must not be framed after
    // run_ended, and a throw below must not strand the entry in either map — or stall the
    // session's queue, which is why the hand-off happens here and not after the awaits.
    const owned = bySession.get(run.sessionKey) === run.runId;
    runs.delete(run.runId);
    for (const [sdkRunId, owner] of bySdkRun) if (owner === run.runId) bySdkRun.delete(sdkRunId);
    if (owned) {
      bySession.delete(run.sessionKey);
      const next = dequeue(run.sessionKey);
      if (next) {
        bySession.set(run.sessionKey, next.run.runId);
        launch(next.run, next.turn);
      }
    }
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
    await deps.callbacks.send({
      runId: run.runId,
      type: "run.ended",
      at: iso(),
      outcome,
      ...(errorKind ? { errorKind } : {}),
      ...(gate ? {} : { usage: opts?.neverLaunched ? ZERO_USAGE : usageFor(run) }),
    });
  }
  /** Only the session's current owner is turning; a queued run has nothing to reach yet. */
  function sessionIdFor(runId: string): string | undefined {
    const run = runs.get(runId);
    if (!run || bySession.get(run.sessionKey) !== run.runId) return undefined;
    return deps.sdk.resolveActiveEmbeddedRunSessionId(run.sessionKey);
  }
  /** Removes and returns a parked turn: the head of the session's line, or one named run. */
  function dequeue(sessionKey: string, runId?: string): { run: ActiveRun; turn: () => Promise<AmbientGateOutcome> } | undefined {
    const line = queued.get(sessionKey);
    if (!line) return undefined;
    const at = runId ? line.findIndex((entry) => entry.run.runId === runId) : 0;
    const [entry] = at < 0 ? [] : line.splice(at, 1);
    if (!line.length) queued.delete(sessionKey);
    return entry;
  }
  // finish() can still throw (a frame flush, or getSessionEntry reading a torn row);
  // nothing is left to report it to, so it is logged and never rethrown.
  function launch(run: ActiveRun, turn: () => Promise<AmbientGateOutcome>): void {
    void turn()
      .then(
        (gate) => finish(run, gate === "dispatched" ? undefined : gate),
        (failure) => finish(run, undefined, failure),
      )
      .catch((error) => logFailure(run.runId, error));
  }

  return {
    /** The ack has been written; run the turn to completion in the background. */
    start(spec: RunStart, turn: () => Promise<AmbientGateOutcome>): void {
      unsubscribe ??= deps.sdk.onAgentEvent(onEvent);
      const run: ActiveRun = { ...spec, started: false, text: { done: "", current: "" } };
      runs.set(spec.runId, run);
      // Accepted is started: the host serializes a second turn on this session behind the
      // first, and a run that reported nothing until its first agent event is closed by the
      // backend sweeper as agent_did_not_start long before it gets to run.
      markStarted(run);
      const busy = bySession.get(spec.sessionKey);
      if (busy !== undefined && runs.has(busy)) {
        queued.set(spec.sessionKey, [...(queued.get(spec.sessionKey) ?? []), { run, turn }]);
        return;
      }
      bySession.set(spec.sessionKey, spec.runId);
      launch(run, turn);
    },
    abort(runId: string): "aborting" | "not_found" {
      const run = runs.get(runId);
      // A queued turn has not been dispatched to the host, so there is nothing to abort
      // there: drop it from the line and report its own end — with explicit zero usage,
      // since the session row it would otherwise read belongs to the turn that did run.
      if (run && dequeue(run.sessionKey, runId)) {
        run.terminal = { phase: "end", aborted: true };
        void finish(run, undefined, undefined, { neverLaunched: true }).catch((error) => logFailure(runId, error));
        return "aborting";
      }
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
