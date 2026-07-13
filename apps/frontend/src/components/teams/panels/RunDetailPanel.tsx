// apps/frontend/src/components/teams/panels/RunDetailPanel.tsx
"use client";

import { useMemo, useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useRunTranscript } from "@/components/teams/runs/useRunTranscript";
import { EditorialButton } from "@/components/teams/shell";
import { formatSilenceAgeMs } from "@/components/teams/shared/lib/liveRuns";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyAgent, RunDetail, RunEvent } from "@/components/teams/shared/types";

// The BFF defaults the events read to 200 and clamps at 1000; ask for the
// ceiling so long runs don't hide later failure/cancel/watchdog events
// (events are seq-ordered ascending, so the default page is the OLDEST 200).
// Full seq-paging is deliberately out of scope at v1 scale.
const RUN_EVENTS_LIMIT = 1000;

// Free upstream string — color the known vocabulary, default to mute.
function statusColor(status: string | null | undefined): string {
  const s = (status ?? "").toLowerCase();
  if (s === "succeeded" || s === "done" || s === "completed") return "var(--forest)";
  if (s === "failed" || s === "error" || s === "cancelled" || s === "timed_out") return "var(--danger)";
  if (s === "running" || s === "queued" || s === "scheduled_retry") return "var(--forest)";
  return "var(--mute)";
}

function isActiveStatus(status: string | null | undefined): boolean {
  const s = (status ?? "").toLowerCase();
  return s === "running" || s === "queued" || s === "scheduled_retry";
}

function nonEmptyString(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

// Upstream's retry/resume wakes re-target the source issue via a payload
// subset of the run's contextSnapshot. Mirrors AgentDetail's own builders:
// retry sends issueId/taskId/taskKey only; resume additionally carries a
// commentId, preferring the snapshot's wakeCommentId over commentId. Each
// candidate is normalized BEFORE coalescing so an empty or non-string
// wakeCommentId still falls through to a valid commentId.
function contextPayload(
  run: RunDetail,
  opts?: { withComment?: boolean },
): Record<string, unknown> {
  const snap = run.contextSnapshot ?? {};
  const out: Record<string, unknown> = {};
  for (const k of ["issueId", "taskId", "taskKey"]) {
    const v = nonEmptyString(snap[k]);
    if (v) out[k] = v;
  }
  if (opts?.withComment) {
    const commentId = nonEmptyString(snap["wakeCommentId"]) ?? nonEmptyString(snap["commentId"]);
    if (commentId) out.commentId = commentId;
  }
  return out;
}

// Mirrors upstream's runMetrics: usageJson carries camelCase or snake_case
// token keys depending on provider adapter.
function usageNumber(u: Record<string, unknown> | null | undefined, ...keys: string[]): number | null {
  if (!u) return null;
  for (const k of keys) {
    const v = u[k];
    if (typeof v === "number" && Number.isFinite(v)) return v;
  }
  return null;
}

function fmtWhen(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? null : `${t.toLocaleString()} (${timeAgo(iso)})`;
}

function fmtDuration(start?: string | null, end?: string | null): string | null {
  if (!start || !end) return null;
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function Prop({ label, children }: { label: string; children: React.ReactNode }) {
  if (children === null || children === undefined || children === "") return null;
  return (
    <div className="flex flex-col gap-0.5 px-3 py-2" style={{ borderBottom: "1px solid var(--edge)" }}>
      <dt className="text-[10px] uppercase tracking-wide" style={{ color: "var(--mute)" }}>
        {label}
      </dt>
      <dd className="text-sm break-words text-[var(--ink)]">{children}</dd>
    </div>
  );
}

export function RunDetailPanel({ runId }: { runId: string }) {
  const { read, post } = useTeamsApi();
  const [acting, setActing] = useState<"cancel" | "retry" | "resume" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [wdActing, setWdActing] = useState(false);

  // A run link can target a heartbeat-run (the normal case) or a legacy
  // generic run; read both, prefer the heartbeat-run.
  //
  // The heartbeat-run read keeps polling while the run is active — otherwise
  // status and logBytes freeze on first render and a running run never
  // visually completes. Activity derives from this read's own data, so the
  // interval uses SWR's function form: it receives the latest data each
  // cycle and returns 0 as soon as a terminal status lands.
  const hb = read<RunDetail>(`/heartbeat-runs/${runId}`, {
    refreshInterval: (latest) => (isActiveStatus(latest?.status) ? 15_000 : 0),
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });
  const legacy = read<RunDetail>(`/runs/${runId}`, { shouldRetryOnError: false });
  const run = hb.data ?? legacy.data ?? null;
  const isLoading = (hb.isLoading || legacy.isLoading) && !run;

  const active = isActiveStatus(run?.status);
  const { data: eventsData } = read<{ events: RunEvent[] }>(
    `/heartbeat-runs/${runId}/events?limit=${RUN_EVENTS_LIMIT}`,
    {
      refreshInterval: active ? 15_000 : 0,
      revalidateOnFocus: false,
      shouldRetryOnError: false,
    },
  );
  const events = eventsData?.events ?? [];

  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const agentName = useMemo(() => {
    const agents = agentsData?.agents ?? [];
    return agents.find((a) => a.id === run?.agentId)?.name ?? run?.agentId ?? "Agent";
  }, [agentsData?.agents, run?.agentId]);

  const transcript = useRunTranscript(runId, {
    active,
    logBytes: run?.logBytes ?? null,
  });

  if (isLoading) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{ fontFamily: "var(--font-lora-serif), serif", color: "var(--mute)" }}
      >
        Loading…
      </div>
    );
  }

  if (!run) {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center p-8 text-sm"
        style={{ color: "var(--danger)" }}
      >
        <p>Failed to load run.</p>
      </div>
    );
  }

  // Cross-guard: a run action and a watchdog decision share actionError, so
  // neither may start while the other is in flight.
  const busy = acting !== null || wdActing;

  async function act(kind: "cancel" | "retry" | "resume") {
    if (!run || busy) return;
    setActing(kind);
    setActionError(null);
    try {
      if (kind === "cancel") {
        await post(`/heartbeat-runs/${run.id}/cancel`, {});
      } else if (run.agentId) {
        // Upstream's wakeup responds with the new run, or a "skipped"
        // envelope carrying no id — surface the skip instead of a silent
        // no-op success.
        const res = await post<{ id?: unknown; message?: unknown } | null>(
          `/agents/${run.agentId}/wakeup`,
          {
            source: "on_demand",
            reason: kind === "retry" ? "retry_failed_run" : "resume_process_lost_run",
            payload:
              kind === "retry"
                ? contextPayload(run)
                : { resumeFromRunId: run.id, ...contextPayload(run, { withComment: true }) },
          },
        );
        if (!res?.id) {
          setActionError(String(res?.message ?? "Request was skipped — the agent may already be running."));
          return;
        }
      }
      await hb.mutate();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Action failed.");
    } finally {
      setActing(null);
    }
  }

  const silence = run?.outputSilence ?? null;
  const silenceLevel = silence?.level ?? null;
  const showWatchdog = silenceLevel === "suspicious" || silenceLevel === "critical";

  async function decideWatchdog(decision: "continue" | "snooze" | "dismissed_false_positive") {
    if (!run || busy) return;
    setWdActing(true);
    setActionError(null);
    try {
      const body: Record<string, unknown> = { decision };
      if (decision === "snooze") {
        body.snoozedUntil = new Date(Date.now() + 60 * 60 * 1000).toISOString();
        body.reason = "Snoozed from run detail";
      }
      await post(`/heartbeat-runs/${run.id}/watchdog-decisions`, body);
      await hb.mutate();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Watchdog decision failed.");
    } finally {
      setWdActing(false);
    }
  }

  const canCancel = isActiveStatus(run?.status);
  const s = (run?.status ?? "").toLowerCase();
  const canRetry = s === "failed" || s === "timed_out";
  const canResume = s === "failed" && run?.errorCode === "process_lost" && !!run?.agentId;

  const usage = run.usageJson ?? null;
  const input = usageNumber(usage, "inputTokens", "input_tokens");
  const output = usageNumber(usage, "outputTokens", "output_tokens");
  const cached = usageNumber(usage, "cachedInputTokens", "cached_input_tokens", "cache_read_input_tokens");
  const provider = typeof usage?.provider === "string" ? usage.provider : null;
  const model = typeof usage?.model === "string" ? usage.model : null;
  const statusLabel = (run.status ?? "unknown").replace(/_/g, " ");

  return (
    <div
      className="editorial-page grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_320px] gap-6 p-4 sm:p-6"
      style={{ background: "var(--cream)", color: "var(--ink)" }}
    >
      <main className="flex min-w-0 flex-col gap-6">
        <div className="flex flex-col gap-2">
          <h1 className="font-editorial-serif text-2xl font-medium leading-tight text-[var(--ink)]">
            Run — {agentName}
          </h1>
          <div className="flex items-center gap-2 text-sm">
            <span className="capitalize" style={{ color: statusColor(run.status) }}>
              {statusLabel}
            </span>
            {run.startedAt && (
              <span className="text-xs" style={{ color: "var(--mute)" }}>
                started {timeAgo(run.startedAt)}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {canCancel && (
              <EditorialButton variant="secondary" size="sm" onClick={() => act("cancel")} disabled={busy}>
                {acting === "cancel" ? "Cancelling…" : "Cancel run"}
              </EditorialButton>
            )}
            {canResume && (
              <EditorialButton variant="secondary" size="sm" onClick={() => act("resume")} disabled={busy}>
                {acting === "resume" ? "Resuming…" : "Resume run"}
              </EditorialButton>
            )}
            {canRetry && run?.agentId && (
              <EditorialButton variant="secondary" size="sm" onClick={() => act("retry")} disabled={busy}>
                {acting === "retry" ? "Retrying…" : "Retry"}
              </EditorialButton>
            )}
          </div>
          {actionError && (
            <p className="text-xs" style={{ color: "var(--danger)" }}>
              {actionError}
            </p>
          )}
        </div>

        {showWatchdog && (
          <section
            className="flex flex-col gap-2 rounded px-3 py-2 text-sm"
            style={{
              border: `1px solid color-mix(in srgb, ${silenceLevel === "critical" ? "var(--danger)" : "var(--warn)"} 35%, var(--edge))`,
              background: `color-mix(in srgb, ${silenceLevel === "critical" ? "var(--danger)" : "var(--warn)"} 8%, transparent)`,
            }}
          >
            <p className="font-medium">
              {silenceLevel === "critical" ? "Stale-run watchdog alert" : "Output silence warning"}
            </p>
            <p className="text-xs" style={{ color: "var(--mute)" }}>
              This run has been silent for {formatSilenceAgeMs(silence?.silenceAgeMs ?? null) ?? "an extended period"}.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <EditorialButton variant="secondary" size="sm" disabled={busy} onClick={() => decideWatchdog("continue")}>
                Continue monitoring
              </EditorialButton>
              <EditorialButton variant="secondary" size="sm" disabled={busy} onClick={() => decideWatchdog("snooze")}>
                Snooze 1h
              </EditorialButton>
              <EditorialButton variant="secondary" size="sm" disabled={busy} onClick={() => decideWatchdog("dismissed_false_positive")}>
                Mark false positive
              </EditorialButton>
            </div>
          </section>
        )}

        {(run.error || run.retryExhaustedReason || run.stderrExcerpt) && (
          <section
            className="flex flex-col gap-1 rounded px-3 py-2 text-sm"
            style={{
              border: "1px solid color-mix(in srgb, var(--danger) 35%, var(--edge))",
              background: "color-mix(in srgb, var(--danger) 8%, transparent)",
            }}
          >
            {run.error && <p style={{ color: "var(--danger)" }}>{run.error}</p>}
            {run.retryExhaustedReason && (
              <p style={{ color: "var(--danger)" }}>{run.retryExhaustedReason}</p>
            )}
            {run.stderrExcerpt && (
              <pre className="whitespace-pre-wrap text-xs" style={{ color: "var(--mute)" }}>
                {run.stderrExcerpt}
              </pre>
            )}
          </section>
        )}

        <section className="flex flex-col gap-3">
          <h2 className="section-label"><b>Transcript</b></h2>
          {transcript.truncated && (
            <p className="text-xs italic" style={{ color: "var(--mute)" }}>
              Log is larger than one read — showing the last 256 KiB.
            </p>
          )}
          {transcript.lines.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--mute)" }}>
              {transcript.isLoading ? "Loading transcript…" : "No transcript captured."}
            </p>
          ) : (
            <pre
              className="max-h-[50vh] overflow-auto whitespace-pre-wrap rounded px-3 py-2 font-mono text-xs"
              style={{ border: "1px solid var(--edge)", background: "var(--cream)" }}
            >
              {transcript.lines.join("\n")}
            </pre>
          )}
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="section-label"><b>Events</b></h2>
          {events.length >= RUN_EVENTS_LIMIT && (
            <p className="text-xs italic" style={{ color: "var(--mute)" }}>
              Showing the first 1,000 events.
            </p>
          )}
          {events.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--mute)" }}>
              No events recorded.
            </p>
          ) : (
            <ul className="flex flex-col">
              {events.map((e) => (
                <li
                  key={e.seq}
                  className="flex items-baseline gap-2 px-3 py-1.5 text-xs"
                  style={{ borderBottom: "1px solid var(--edge)" }}
                >
                  <span
                    className="flex-shrink-0 uppercase tracking-wide"
                    style={{ color: e.level === "error" ? "var(--danger)" : e.level === "warn" ? "var(--warn)" : "var(--mute)" }}
                  >
                    {e.eventType ?? "event"}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[var(--ink)]">{e.message ?? ""}</span>
                  {e.createdAt && (
                    <span className="flex-shrink-0" style={{ color: "var(--mute)" }}>
                      {timeAgo(e.createdAt)}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>

      <aside className="lg:sticky lg:top-4 lg:self-start">
        <dl className="flex flex-col">
          <Prop label="Agent">{agentName}</Prop>
          <Prop label="Trigger">
            {[run.invocationSource, run.triggerDetail].filter(Boolean).join(" · ") || null}
          </Prop>
          <Prop label="Started">{fmtWhen(run.startedAt)}</Prop>
          <Prop label="Finished">{fmtWhen(run.finishedAt)}</Prop>
          <Prop label="Duration">{fmtDuration(run.startedAt, run.finishedAt)}</Prop>
          <Prop label="Usage">
            {input !== null || output !== null
              ? `${(input ?? 0).toLocaleString()} in / ${(output ?? 0).toLocaleString()} out${cached ? ` (${cached.toLocaleString()} cached)` : ""}`
              : null}
          </Prop>
          <Prop label="Model">{[provider, model].filter(Boolean).join(" · ") || null}</Prop>
          <Prop label="Error code">{run.errorCode}</Prop>
          <Prop label="Exit">
            {typeof run.exitCode === "number" ? String(run.exitCode) : null}
          </Prop>
          <Prop label="Retry of">{run.retryOfRunId}</Prop>
          <Prop label="Liveness">
            {[run.livenessState, run.livenessReason].filter(Boolean).join(" — ") || null}
          </Prop>
          <Prop label="Next action">{run.nextAction}</Prop>
          <Prop label="Session before">{run.sessionIdBefore}</Prop>
          <Prop label="Session after">{run.sessionIdAfter}</Prop>
          <Prop label="Run id">{run.id}</Prop>
        </dl>
      </aside>
    </div>
  );
}
