/**
 * Run-detail data layer (web RunDetailPanel/useRunTranscript parity).
 *
 * One hook owns the whole screen: run row (heartbeat-run with legacy /runs
 * fallback), transcript (bounded tail read of the NDJSON log), and the event
 * timeline — polled together every 15s while the run is active, frozen once
 * terminal. Actions: cancel (POST .../cancel), retry / resume (wakeup with the
 * run's contextSnapshot payload, exactly the web builders).
 *
 * Same conventions as teamsData.ts: bearer-only via the shared client,
 * readiness-gate mapping, loadSeq staleness guards, useFocusEffect refetch.
 */
import { useFocusEffect } from "expo-router";
import { useCallback, useRef, useState } from "react";
import {
  createApiClient,
  fetchJsonWithStatus,
  type PlatformAdapter,
} from "@lightbulb/shared";

import { purchaseSafeMessage } from "./purchaseCta";
import {
  gateFromError,
  gateFromSuccess,
  useTeamsConfig,
  type TeamsAgent,
  type TeamsGate,
} from "./teamsData";

/** Full heartbeat-run row (web shared/types.ts RunDetail subset — every field
 * optional-defensive because upstream row shapes drift). */
export interface RunDetail {
  id: string;
  agentId?: string | null;
  /** Free string; known vocab: queued | scheduled_retry | running |
   *  succeeded | failed | cancelled | timed_out. Render defensively. */
  status?: string | null;
  invocationSource?: string | null;
  triggerDetail?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  createdAt?: string | null;
  error?: string | null;
  errorCode?: string | null;
  exitCode?: number | null;
  usageJson?: Record<string, unknown> | null;
  logBytes?: number | null;
  stderrExcerpt?: string | null;
  retryOfRunId?: string | null;
  retryExhaustedReason?: string | null;
  livenessState?: string | null;
  livenessReason?: string | null;
  contextSnapshot?: Record<string, unknown> | null;
}

/** One heartbeat_run_events row (upstream events timeline). */
export interface RunEvent {
  seq: number;
  eventType?: string | null;
  level?: string | null;
  message?: string | null;
  createdAt?: string | null;
}

/** Matches the BFF's RUN_LOG_MAX_LIMIT_BYTES ceiling (web parity). */
export const RUN_TRANSCRIPT_LIMIT_BYTES = 262_144;
/** Events fetch ceiling. Events are seq-ordered ASCENDING with afterSeq=0,
 * so a capped response is the run's FIRST 1000 events — the screen's notices
 * must say so (full seq-paging is out of scope at v1, matching web). */
export const RUN_EVENTS_FETCH_LIMIT = 1000;
/** Poll cadence while the run is active (web parity). */
const REFRESH_MS = 15_000;

export function isActiveRunStatus(status: string | null | undefined): boolean {
  const s = (status ?? "").toLowerCase();
  return s === "running" || s === "queued" || s === "scheduled_retry";
}

/**
 * Parse the upstream log `content` blob into display lines — port of web
 * `parseLogLines` (dashboard/hooks/useRunLog.ts). Upstream appends
 * newline-delimited JSON records `{ts, stream, chunk}`; we pull the `chunk`
 * text out of each. Non-JSON lines are kept raw; blank lines filtered.
 */
export function parseRunLogLines(content: string): string[] {
  const out: string[] = [];
  for (const rawLine of content.split("\n")) {
    const trimmed = rawLine.trim();
    if (!trimmed) continue;
    let text = trimmed;
    if (trimmed.startsWith("{")) {
      try {
        const rec = JSON.parse(trimmed) as { chunk?: unknown };
        text = typeof rec.chunk === "string" ? rec.chunk : "";
      } catch {
        text = trimmed;
      }
    }
    for (const sub of text.split("\n")) {
      const subTrimmed = sub.replace(/\s+$/, "");
      if (subTrimmed.length > 0) out.push(subTrimmed);
    }
  }
  return out;
}

/** Retry/resume wake payload from the run's contextSnapshot — mirrors web
 * contextPayload: retry sends issueId/taskId/taskKey; resume additionally
 * carries a commentId (wakeCommentId preferred) + resumeFromRunId. */
export function runContextPayload(
  run: RunDetail,
  opts?: { withComment?: boolean },
): Record<string, unknown> {
  const snap = run.contextSnapshot ?? {};
  const out: Record<string, unknown> = {};
  for (const k of ["issueId", "taskId", "taskKey"]) {
    const v = snap[k];
    if (typeof v === "string" && v) out[k] = v;
  }
  if (opts?.withComment) {
    const wake = snap["wakeCommentId"];
    const plain = snap["commentId"];
    const commentId =
      (typeof wake === "string" && wake) || (typeof plain === "string" && plain) || null;
    if (commentId) out.commentId = commentId;
  }
  return out;
}

export type RunDetailState =
  | { kind: "loading" }
  | { kind: "gate"; gate: TeamsGate; message: string | null }
  | { kind: "error"; message: string }
  | {
      kind: "ready";
      run: RunDetail;
      transcriptLines: string[];
      transcriptTruncated: boolean;
      events: RunEvent[];
      agents: TeamsAgent[];
    };

async function fetchRunRow(
  config: PlatformAdapter,
  runId: string,
): Promise<{ gate: TeamsGate | null; run: RunDetail | null }> {
  // A run link can target a heartbeat-run (the normal case) or a legacy
  // generic run; try heartbeat first, fall back to /runs (web parity).
  try {
    const { status, body } = await fetchJsonWithStatus(
      config,
      `/teams/heartbeat-runs/${encodeURIComponent(runId)}`,
    );
    const gate = gateFromSuccess(status, body);
    if (gate) return { gate, run: null };
    if (body && typeof body === "object" && (body as { id?: unknown }).id)
      return { gate: null, run: body as RunDetail };
  } catch (err) {
    const gate = gateFromError(err);
    if (gate === "provisioning" || gate === "needs_subscription") return { gate, run: null };
    // 404/unavailable → try the legacy read below.
  }
  const { status, body } = await fetchJsonWithStatus(
    config,
    `/teams/runs/${encodeURIComponent(runId)}`,
  );
  const gate = gateFromSuccess(status, body);
  if (gate) return { gate, run: null };
  return { gate: null, run: body as RunDetail };
}

export function useRunDetail(runId: string | undefined) {
  const config = useTeamsConfig();
  const [state, setState] = useState<RunDetailState>({ kind: "loading" });
  const loadSeq = useRef(0);
  const stateRef = useRef(state);
  stateRef.current = state;
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState<"cancel" | "retry" | "resume" | null>(null);
  const actingRef = useRef(false);

  const load = useCallback(async () => {
    if (!runId) {
      setState({ kind: "error", message: "Missing run id" });
      return;
    }
    const seq = loadSeq.current + 1;
    loadSeq.current = seq;
    setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
    try {
      const { gate, run } = await fetchRunRow(config, runId);
      if (loadSeq.current !== seq) return;
      if (gate) {
        setState({ kind: "gate", gate, message: null });
        return;
      }
      if (!run) {
        setState({ kind: "error", message: "Failed to load run" });
        return;
      }
      // Transcript: ONE bounded read aimed at the tail — when the log exceeds
      // the cap, start at logBytes - cap and drop the partial first NDJSON
      // record (web useRunTranscript parity). Events + agent names ride
      // alongside; all three are best-effort against a run that loaded.
      const truncated =
        typeof run.logBytes === "number" && run.logBytes > RUN_TRANSCRIPT_LIMIT_BYTES;
      const logPath = truncated
        ? `/teams/heartbeat-runs/${encodeURIComponent(runId)}/log?offset=${(run.logBytes as number) - RUN_TRANSCRIPT_LIMIT_BYTES}&limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`
        : `/teams/heartbeat-runs/${encodeURIComponent(runId)}/log?limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`;
      const [transcriptLines, events, agents] = await Promise.all([
        fetchJsonWithStatus(config, logPath)
          .then((r) => {
            let content = (r.body as { content?: string | null } | null)?.content ?? "";
            if (!content) return [] as string[];
            if (truncated) {
              // A tail read's byte offset can land mid-record; parsing starts
              // at the first complete line past the first newline.
              const firstNewline = content.indexOf("\n");
              content = firstNewline === -1 ? "" : content.slice(firstNewline + 1);
            }
            return parseRunLogLines(content);
          })
          .catch(() => [] as string[]),
        fetchJsonWithStatus(
          config,
          `/teams/heartbeat-runs/${encodeURIComponent(runId)}/events?limit=${RUN_EVENTS_FETCH_LIMIT}`,
        )
          .then((r) => {
            const list = (r.body as { events?: unknown } | null)?.events;
            return Array.isArray(list) ? (list as RunEvent[]) : [];
          })
          .catch(() => [] as RunEvent[]),
        fetchJsonWithStatus(config, "/teams/agents")
          .then((r) => {
            const list = (r.body as { agents?: unknown } | null)?.agents;
            return Array.isArray(list) ? (list as TeamsAgent[]) : [];
          })
          .catch(() => [] as TeamsAgent[]),
      ]);
      if (loadSeq.current !== seq) return;
      setState({
        kind: "ready",
        run,
        transcriptLines,
        transcriptTruncated: truncated,
        events,
        agents,
      });
    } catch (err) {
      if (loadSeq.current !== seq) return;
      const gate = gateFromError(err);
      if (gate) {
        setState({ kind: "gate", gate, message: null });
      } else {
        setState({
          kind: "error",
          message: purchaseSafeMessage(err, "Request failed"),
        });
      }
    }
  }, [config, runId]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  // Poll while active AND focused: status/logBytes/transcript freeze on first
  // render otherwise and a running run never visually completes (web parity).
  // useFocusEffect scopes the interval to the focused screen so a run screen
  // parked under a pushed sibling doesn't keep polling in the background.
  const active =
    state.kind === "ready" && isActiveRunStatus(state.run.status);
  useFocusEffect(
    useCallback(() => {
      if (!active) return;
      const timer = setInterval(() => void load(), REFRESH_MS);
      return () => clearInterval(timer);
    }, [active, load]),
  );

  const act = useCallback(
    async (kind: "cancel" | "retry" | "resume") => {
      const current = stateRef.current;
      if (current.kind !== "ready" || actingRef.current || !runId) return;
      const run = current.run;
      actingRef.current = true;
      setActing(kind);
      setActionError(null);
      try {
        if (kind === "cancel") {
          await createApiClient(config).post(
            `/teams/heartbeat-runs/${encodeURIComponent(runId)}/cancel`,
            {},
          );
        } else if (run.agentId) {
          // Upstream's wakeup responds with the new run, or a "skipped"
          // envelope carrying no id — surface the skip instead of a silent
          // no-op success (web parity).
          const res = (await createApiClient(config).post(
            `/teams/agents/${encodeURIComponent(run.agentId)}/wakeup`,
            {
              source: "on_demand",
              reason: kind === "retry" ? "retry_failed_run" : "resume_process_lost_run",
              payload:
                kind === "retry"
                  ? runContextPayload(run)
                  : { resumeFromRunId: run.id, ...runContextPayload(run, { withComment: true }) },
            },
          )) as { id?: unknown; message?: unknown } | null;
          if (!res?.id) {
            setActionError(
              String(res?.message ?? "Request was skipped — the agent may already be running."),
            );
            return;
          }
        }
        await load();
      } catch (err) {
        setActionError(purchaseSafeMessage(err, "Action failed."));
      } finally {
        actingRef.current = false;
        setActing(null);
      }
    },
    [config, runId, load],
  );

  return { state, active, acting, actionError, act, retry: load };
}
