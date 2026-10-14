// apps/frontend/src/components/teams/routines/RoutineActivitySection.tsx

// Ported from upstream Paperclip's routine Activity section — `ActivitySection`
// (paperclip/ui/src/components/routine-sections/operate-sections.tsx) and
// `RoutineActivityRow` (paperclip/ui/src/components/RoutineActivityRow.tsx),
// both @ v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI). Day grouping, the
// time / action / three-key detail summary row and the expandable JSON payload
// are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - the client-side fan-out (`routinesApi.activity`: company activity per
//     routine, trigger and run id) -> one useTeamsApi read of
//     /teams/routines/{id}/activity; the BFF does the fan-out pinned to the
//     caller's company and 404s a routine outside it
//   - shadcn Badge / neutral-950 code block / EmptyState -> brand tokens

"use client";

import { useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { cn } from "@/lib/utils";
import type { RoutineActivityEvent } from "@/components/teams/shared/types";

function formatTime(value: string): string {
  try {
    return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return String(value);
  }
}

function summarizeDetails(details: Record<string, unknown> | null | undefined): string {
  if (!details) return "";
  const entries = Object.entries(details).slice(0, 3);
  return entries
    .map(([key, value]) => `${key.replaceAll("_", " ")}: ${formatDetailValue(value)}`)
    .join(" · ");
}

function formatDetailValue(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.length === 0 ? "[]" : value.map(formatDetailValue).join(", ");
  try {
    return JSON.stringify(value);
  } catch {
    return "[unserializable]";
  }
}

export function RoutineActivityRow({ event }: { event: RoutineActivityEvent }) {
  const [expanded, setExpanded] = useState(false);
  const hasPayload = event.details != null && Object.keys(event.details).length > 0;

  return (
    <div style={{ borderBottom: "1px solid var(--edge)" }}>
      <button
        type="button"
        disabled={!hasPayload}
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={hasPayload ? expanded : undefined}
        className={cn(
          "flex w-full items-center gap-3 px-1 py-2 text-left text-caption",
          hasPayload ? "hover:bg-[var(--cream-deep)]" : "cursor-default",
        )}
      >
        <span className="w-12 shrink-0 font-mono text-[var(--midnight-muted)]">
          {formatTime(event.createdAt)}
        </span>
        <span
          className="shrink-0 rounded-sm border px-1.5 font-mono"
          style={{ borderColor: "var(--edge)", color: "var(--midnight)" }}
        >
          {event.action}
        </span>
        <span className="min-w-0 flex-1 truncate text-[var(--midnight-muted)]">
          {summarizeDetails(event.details)}
        </span>
        {hasPayload ? (
          <ChevronRight
            size={14}
            aria-hidden="true"
            className={cn(
              "shrink-0 text-[var(--midnight-muted)] transition-transform",
              expanded && "rotate-90",
            )}
          />
        ) : null}
      </button>
      {expanded && hasPayload ? (
        <pre
          className="mx-1 mb-2 overflow-x-auto rounded-md p-3 font-mono text-caption"
          style={{ background: "var(--cream-deep)", color: "var(--midnight)" }}
        >
          {JSON.stringify(event.details, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}

export function RoutineActivitySection({ routineId }: { routineId: string }) {
  const { read } = useTeamsApi();
  const { data, error, isLoading } = read<{ events: RoutineActivityEvent[] }>(
    `/routines/${routineId}/activity`,
  );
  const events = useMemo(() => data?.events ?? [], [data]);

  const groups = useMemo(() => {
    const byDay = new Map<string, RoutineActivityEvent[]>();
    for (const event of events) {
      let label = "Earlier";
      try {
        label = new Date(event.createdAt).toLocaleDateString(undefined, {
          weekday: "short",
          month: "short",
          day: "numeric",
        });
      } catch {
        /* keep fallback label */
      }
      const bucket = byDay.get(label) ?? [];
      bucket.push(event);
      byDay.set(label, bucket);
    }
    return Array.from(byDay.entries());
  }, [events]);

  if (error) {
    return (
      <p role="alert" className="text-ui" style={{ color: "var(--err)" }}>
        Failed to load activity.
      </p>
    );
  }
  if (isLoading && !data) {
    return (
      <p className="text-ui italic" style={{ color: "var(--midnight-muted)" }}>
        Loading…
      </p>
    );
  }
  if (events.length === 0) {
    return (
      <p className="text-ui italic" style={{ color: "var(--midnight-muted)" }}>
        No activity yet.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {groups.map(([day, dayEvents]) => (
        <div key={day}>
          <div className="py-2 text-caption font-medium uppercase tracking-wide text-[var(--midnight-muted)]">
            {day}
          </div>
          <div>
            {dayEvents.map((event) => (
              <RoutineActivityRow key={event.id} event={event} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
