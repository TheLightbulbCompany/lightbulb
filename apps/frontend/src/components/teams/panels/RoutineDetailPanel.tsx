// apps/frontend/src/components/teams/panels/RoutineDetailPanel.tsx

// Restructured onto the editorial detail shell, mirroring
// goals/GoalDetailPage.tsx + projects/ProjectDetailPage.tsx: an
// `editorial-page` container with a [1fr 320px] grid — a main column
// (font-editorial-serif header + Schedule + Run-history sections) and a
// sticky non-scrollable Properties aside (Status / Assignee / Created /
// Updated). Status reads as a real swatch (RoutineStatusIcon, modeled on
// GoalStatusIcon) rather than an --edge fill.

"use client";

import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { cn } from "@/lib/utils";
import type {
  CompanyAgent,
  RoutineDetail,
  RoutineRun,
} from "@/components/teams/shared/types";

function runStatusColor(status: string): string {
  const s = status.toLowerCase();
  if (s === "failed" || s === "error" || s === "cancelled") return "var(--danger)";
  if (s === "succeeded" || s === "completed" || s === "done") return "var(--forest)";
  return "var(--mute)";
}

// Retheme-mapped onto the editorial palette (mirrors GoalStatusIcon):
//   active   -> --forest  (running / live)
//   paused   -> --warn    (suspended — attention amber)
//   draft    -> --mute    (not yet live)
//   archived -> --mute    (retired — muted)
const routineStatusColor: Record<string, string> = {
  active: "var(--forest)",
  paused: "var(--warn)",
  draft: "var(--mute)",
  archived: "var(--mute)",
};
const routineStatusColorDefault = "var(--mute)";

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// A status swatch + label, modeled on goals/GoalStatusIcon (read-only —
// the slim BFF has no routine status-PATCH path, so there is no picker).
function RoutineStatusIcon({
  status,
  showLabel,
  className,
}: {
  status: string;
  showLabel?: boolean;
  className?: string;
}) {
  const swatchColor = routineStatusColor[status] ?? routineStatusColorDefault;
  const isActive = status === "active";
  const circle = (
    <span
      className={cn(
        "relative inline-flex h-4 w-4 rounded-full border-2 shrink-0",
        className,
      )}
      style={{ color: swatchColor, borderColor: swatchColor }}
      aria-label={statusLabel(status)}
      title={statusLabel(status)}
    >
      {isActive && (
        <span className="absolute inset-0 m-auto h-2 w-2 rounded-full bg-current" />
      )}
    </span>
  );
  if (!showLabel) return circle;
  return (
    <span className="inline-flex items-center gap-1.5">
      {circle}
      <span className="text-sm">{statusLabel(status)}</span>
    </span>
  );
}

function runTime(r: RoutineRun): number {
  const v = r.triggeredAt ?? r.startedAt ?? r.createdAt ?? null;
  const t = v ? new Date(v).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

// A trigger's `nextRunAt` is a FUTURE timestamp, so the past-only `timeAgo`
// would render every upcoming run as "just now". Format future-relative
// ("in 8h") and only fall back to `timeAgo` for a past/overdue next-run.
function nextRunLabel(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffSec = Math.round((then - Date.now()) / 1000);
  if (diffSec < 60) return timeAgo(iso); // overdue / imminent → "X ago" / "just now"
  if (diffSec < 3600) return `in ${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `in ${Math.floor(diffSec / 3600)}h`;
  return `in ${Math.floor(diffSec / 86400)}d`;
}

function PropertyRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className="grid grid-cols-[100px_1fr] gap-3 items-baseline py-2 text-sm"
      style={{ color: "var(--ink-soft)", borderBottom: "1px solid var(--edge)" }}
    >
      <dt className="section-label" style={{ marginTop: 0, marginBottom: 0 }}>
        <b>{label}</b>
      </dt>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

const EMPTY_DASH_STYLE = { color: "var(--mute)" } as const;

export function RoutineDetailPanel({ routineId }: { routineId: string }) {
  const { read } = useTeamsApi();
  const {
    data: routine,
    isLoading,
    error,
  } = read<RoutineDetail>(`/routines/${routineId}`);
  const { data: runsData, error: runsError } = read<{ runs: RoutineRun[] }>(
    `/routines/${routineId}/runs`,
  );
  const { data: agentsResp } = read<{ agents: CompanyAgent[] }>("/agents");
  const agents = agentsResp?.agents ?? [];
  const isError = !!error;

  if (isLoading && !routine) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-lora-serif), serif",
          color: "var(--mute)",
        }}
      >
        Loading…
      </div>
    );
  }

  if (isError && !routine) {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center p-8 text-sm"
        style={{ color: "var(--danger)" }}
      >
        <p>Failed to load routine.</p>
        {error?.message && (
          <p className="mt-1" style={{ color: "var(--mute)" }}>
            {error.message}
          </p>
        )}
      </div>
    );
  }

  if (!routine) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-lora-serif), serif",
          color: "var(--mute)",
        }}
      >
        Routine not found.
      </div>
    );
  }

  const runs = [...(runsData?.runs ?? [])].sort((a, b) => runTime(b) - runTime(a)); // newest first
  const triggers = routine.triggers ?? [];
  const assigneeAgent = routine.assigneeAgentId
    ? agents.find((a) => a.id === routine.assigneeAgentId)
    : null;
  const assigneeName =
    routine.assignee?.name ?? assigneeAgent?.name ?? routine.assigneeAgentId ?? null;

  return (
    <div
      className="editorial-page grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_320px] gap-6 p-4 sm:p-6"
      style={{ background: "var(--cream)", color: "var(--ink)" }}
    >
      <main className="flex flex-col gap-6 min-w-0">
        <nav
          className="flex items-center gap-1 text-xs"
          style={{ color: "var(--mute)" }}
          aria-label="Breadcrumb"
        >
          <Link href="/teams/routines" className="hover:underline">
            Routines
          </Link>
          <ChevronRight className="h-3 w-3" />
          <span className="truncate" style={{ color: "var(--ink-soft)" }}>
            {routine.title}
          </span>
        </nav>

        <div className="flex flex-col gap-2">
          <h1 className="font-editorial-serif text-2xl font-medium leading-tight text-[var(--ink)]">
            {routine.title}
          </h1>
          {routine.description && (
            <p
              className="text-sm whitespace-pre-wrap pl-3"
              style={{
                borderLeft: "2px solid var(--edge)",
                color: "var(--ink-soft)",
              }}
            >
              {routine.description}
            </p>
          )}
        </div>

        <section className="flex flex-col gap-3">
          <h2 className="section-label">
            <b>Schedule</b>
          </h2>
          {triggers.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--mute)" }}>
              No triggers.
            </p>
          ) : (
            <ul className="flex flex-col gap-1">
              {triggers.map((t) => (
                <li key={t.id} className="flex items-center gap-2 text-sm">
                  <span
                    className="text-[10px] uppercase tracking-wide font-mono px-1.5 py-0.5 rounded flex-shrink-0"
                    style={{ background: "var(--edge)", color: "var(--ink)" }}
                  >
                    {t.kind}
                  </span>
                  {t.cronExpression && (
                    <span className="font-mono text-xs text-[var(--ink)]">
                      {t.cronExpression}
                    </span>
                  )}
                  {t.timezone && (
                    <span className="text-xs text-[var(--mute)]">{t.timezone}</span>
                  )}
                  {t.nextRunAt && (
                    <span className="text-xs text-[var(--mute)]">
                      · next {nextRunLabel(t.nextRunAt)}
                    </span>
                  )}
                  {t.enabled === false && (
                    <span className="text-xs text-[var(--mute)]">· paused</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="section-label">
            <b>Run history</b>
          </h2>
          {runsError ? (
            <p role="alert" className="text-sm" style={{ color: "var(--danger)" }}>
              Failed to load run history.
            </p>
          ) : runs.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--mute)" }}>
              No runs yet.
            </p>
          ) : (
            <ul className="flex flex-col">
              {runs.map((r) => {
                const when = r.triggeredAt ?? r.startedAt ?? r.createdAt;
                return (
                  <li
                    key={r.id}
                    className="flex flex-col gap-0.5 px-3 py-2"
                    style={{ borderBottom: "1px solid var(--edge)" }}
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded flex-shrink-0"
                        style={{ background: "var(--edge)", color: "var(--mute)" }}
                      >
                        {r.source}
                      </span>
                      <span
                        className="text-xs capitalize flex-shrink-0"
                        style={{ color: runStatusColor(r.status) }}
                      >
                        {r.status.replace(/_/g, " ")}
                      </span>
                      {r.trigger && (
                        <span className="text-xs text-[var(--mute)] truncate">
                          {r.trigger.label ?? r.trigger.kind}
                        </span>
                      )}
                      {r.linkedIssue && (
                        <a
                          href={`/teams/issues/${r.linkedIssue.id}`}
                          className="text-xs hover:underline"
                          style={{ color: "var(--forest)" }}
                        >
                          {r.linkedIssue.identifier ?? r.linkedIssue.id.slice(0, 8)}
                        </a>
                      )}
                      <span className="flex-1" />
                      {when && (
                        <span className="text-xs text-[var(--mute)] flex-shrink-0">
                          {timeAgo(when)}
                        </span>
                      )}
                    </div>
                    {r.failureReason && (
                      <div className="text-xs pl-1" style={{ color: "var(--danger)" }}>
                        {r.failureReason}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </main>

      <aside className="lg:sticky lg:top-4 lg:self-start">
        <dl className="flex flex-col">
          <PropertyRow label="Status">
            {routine.status ? (
              <RoutineStatusIcon status={routine.status} showLabel />
            ) : (
              <span data-empty="true" style={EMPTY_DASH_STYLE}>
                —
              </span>
            )}
          </PropertyRow>
          <PropertyRow label="Assignee">
            {assigneeAgent ? (
              <Link
                href={`/teams/agents/${assigneeAgent.id}`}
                className="hover:underline"
                style={{ color: "var(--ink)" }}
              >
                {assigneeAgent.name}
              </Link>
            ) : assigneeName ? (
              <span style={{ color: "var(--ink)" }}>{assigneeName}</span>
            ) : (
              <span data-empty="true" style={EMPTY_DASH_STYLE}>
                —
              </span>
            )}
          </PropertyRow>
          <PropertyRow label="Created">
            {routine.createdAt ? (
              <time dateTime={routine.createdAt} style={{ color: "var(--mute)" }}>
                {timeAgo(routine.createdAt)}
              </time>
            ) : (
              <span data-empty="true" style={EMPTY_DASH_STYLE}>
                —
              </span>
            )}
          </PropertyRow>
          <PropertyRow label="Updated">
            {routine.updatedAt ? (
              <time dateTime={routine.updatedAt} style={{ color: "var(--mute)" }}>
                {timeAgo(routine.updatedAt)}
              </time>
            ) : (
              <span data-empty="true" style={EMPTY_DASH_STYLE}>
                —
              </span>
            )}
          </PropertyRow>
        </dl>
      </aside>
    </div>
  );
}
