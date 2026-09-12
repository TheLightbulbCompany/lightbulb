import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";
import { Tile } from "@/components/admin/Tile";
import type { GrowthData, GrowthSegment } from "../_lib/api";

// Exported so charts.tsx formats a share exactly the same way — one source of
// truth, and `null` (nothing to divide by, or a messaging read that failed) is
// an em dash everywhere, never 0%.
export function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

/** A count, or an em dash when the source that feeds it is unavailable. */
export function num(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : String(value);
}

/** `ok` reads as "this is going well", which zero is not — neutral at 0 and at
 * an unknown, `ok` only above it. */
function countTone(value: number | null | undefined): "ok" | "unknown" {
  return value ? "ok" : "unknown";
}

export function Table({ head, rows }: { head: string[]; rows: React.ReactNode[][] }) {
  if (rows.length === 0) return <div className="text-xs text-muted-foreground">none</div>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-muted-foreground">
            {head.map((h) => (
              <th key={h} className="pb-1 pr-4 font-normal">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-border text-foreground">
              {r.map((c, j) => (
                <td key={j} className="py-1 pr-4 tabular-nums">{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const secs = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v}s`);

/** The messaging card: what the room layer is doing right now, plus the one
 * counter that says replies are being lost (audit §6 metric 3). */
export function Messaging({ seg, data }: { seg: GrowthSegment; data: GrowthData }) {
  const a = seg.answered;
  const dlq = data.dlq_depth;
  return (
    <Card title="Messaging">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <MetricStat label="rooms" value={num(seg.messaging.rooms)} />
        <MetricStat label="human messages this week" value={num(seg.messaging.human_messages_this_week)} />
        <MetricStat
          label="answered within 2 min"
          value={pct(a.rate)}
          hint={`${num(a.answered)} of ${num(a.dispatched)} addressed, last 8 weeks`}
          tone={a.rate !== null && a.rate < 0.95 ? "error" : "default"}
        />
        <MetricStat label="p50 reply" value={secs(a.p50_reply_s)} />
        <MetricStat
          label="work DLQ"
          value={dlq === null ? "—" : dlq}
          hint={dlq === null ? "CloudWatch read failed" : "replies that will never arrive"}
          tone={dlq === null ? "unknown" : dlq > 0 ? "error" : "default"}
        />
      </div>
    </Card>
  );
}

/** The five §6 metrics as one row of headline tiles — the weekly-review shape. */
export function Headline({ seg, audience }: { seg: GrowthSegment; audience: string }) {
  const thisWeek = seg.weekly_active[seg.weekly_active.length - 1];
  const a = seg.activation;
  const r = seg.retention_w4;
  return (
    <Card title={audience}>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Tile title="Activated rate" label={pct(a.rate)} sub={`${num(a.activated)} of ${num(a.measurable)} measurable`} tone={a.rate ? "ok" : "unknown"} />
        <Tile title="Weekly active" label={num(thisWeek?.workspaces)} sub={`multiplayer ${pct(thisWeek?.multiplayer_share)}`} tone={countTone(thisWeek?.workspaces)} />
        <Tile
          title="Answered rate"
          label={pct(seg.answered.rate)}
          sub={`p50 ${secs(seg.answered.p50_reply_s)}`}
          tone={seg.answered.rate === null ? "unknown" : seg.answered.rate < 0.95 ? "error" : "ok"}
        />
        <Tile title="Paying" label={String(seg.workspaces.paying)} sub={`of ${seg.workspaces.total} workspaces`} tone={countTone(seg.workspaces.paying)} />
        <Tile title="W4 retention" label={pct(r.rate)} sub={`${num(r.retained)} of ${num(r.eligible)} eligible`} tone={r.rate ? "ok" : "unknown"} />
      </div>
    </Card>
  );
}

export function Workspaces({ seg }: { seg: GrowthSegment }) {
  const w = seg.workspaces;
  const fmt = (m: Record<string, number>) => Object.entries(m).map(([k, v]) => `${k} ${v}`).join(" · ") || "—";
  return (
    <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
      <MetricStat label="workspaces" value={w.total} />
      <MetricStat label="paying" value={w.paying} hint={fmt(w.paying_by_plan)} />
      <MetricStat label="paying by provider" value={fmt(w.paying_by_provider)} />
      <MetricStat label="trials active" value={w.trial_active} />
      <MetricStat label="trials expired" value={w.trial_expired} tone={w.trial_expired ? "unknown" : "default"} />
    </div>
  );
}

export function WeeklyActive({ seg }: { seg: GrowthSegment }) {
  return (
    <>
      <Table
        head={["week", "active workspaces", "≥2 human senders", "multiplayer share"]}
        rows={seg.weekly_active.map((w) => [w.week, num(w.workspaces), num(w.multiplayer), pct(w.multiplayer_share)])}
      />
      <div className="mt-2 text-xs text-muted-foreground">
        Active = at least one human room message in that ISO week. Distinct senders, not members: a workspace with two
        humans where only one speaks is not multiplayer.
      </div>
    </>
  );
}

export function Cohorts({ seg }: { seg: GrowthSegment }) {
  return (
    <>
      <Table
        head={["signup week", "signups", "measurable", "activated", "activated rate", "paying now", "trial → paid"]}
        rows={seg.cohorts.map((c) => [
          c.week,
          c.signups,
          num(c.measurable),
          num(c.activated),
          pct(c.measurable && c.activated !== null ? c.activated / c.measurable : null),
          c.paying,
          pct(c.signups ? c.paying / c.signups : null),
        ])}
      />
      <div className="mt-2 text-xs text-muted-foreground">
        Activated = within 7 days of the trial start, one human sent ≥5 room messages on ≥2 distinct UTC days AND the
        workspace received ≥1 agent message. &ldquo;Measurable&rdquo; excludes cohorts whose 7-day window is still open.
        &ldquo;Paying now&rdquo; is the cohort&apos;s CURRENT status, not a recorded trial→paid transition: a workspace
        that converted and then cancelled reads here as never converted.
      </div>
    </>
  );
}

export function Acquisition({ seg, data }: { seg: GrowthSegment; data: GrowthData }) {
  const funnel = new Map<string, Record<string, number>>();
  for (const r of data.posthog.funnel) funnel.set(r.week, { ...(funnel.get(r.week) ?? {}), [r.event]: r.owners });
  const events = ["landing_viewed", "signup_started", "trial_started"];
  const activatedByWeek = new Map(seg.cohorts.map((c) => [c.week, c.activated]));
  return (
    <>
      <div className="grid gap-6 md:grid-cols-[1fr_auto]">
        <Table
          head={["week", ...events, "activated (server)"]}
          rows={data.weeks.map((w) => [
            w,
            ...events.map((e) => funnel.get(w)?.[e] ?? 0),
            num(activatedByWeek.get(w)),
          ])}
        />
        <div>
          <div className="mb-1 text-xs text-muted-foreground">signup_started by first touch</div>
          <Table head={["source", "owners"]} rows={data.posthog.signup_sources.map((s) => [s.source, s.owners])} />
        </div>
      </div>
      <div className="mt-2 text-xs text-muted-foreground">
        &ldquo;activated (server)&rdquo; is the §6 definition for that week&apos;s signup cohort, not a PostHog event —
        the three client stages count both environments, it counts this one.
      </div>
    </>
  );
}

export function Counters({ data }: { data: GrowthData }) {
  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div>
        <div className="mb-1 text-xs text-muted-foreground">provision.gate.blocked, last 7d (CloudWatch, all workspaces)</div>
        <Table head={["code", "count"]} rows={data.gate_blocked_7d.map((g) => [g.code, g.n])} />
      </div>
      <div>
        <div className="mb-1 text-xs text-muted-foreground">source freshness</div>
        <Table head={["source", "state"]} rows={Object.entries(data.sources).map(([k, v]) => [k, v])} />
      </div>
    </div>
  );
}
