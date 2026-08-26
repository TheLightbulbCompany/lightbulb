import Link from "next/link";

import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";
import type { GrowthData, GrowthSegment, GrowthOwnerStage } from "../_lib/api";

const STAGE_LABEL: Record<GrowthOwnerStage["stage"], string> = {
  signed_up: "signed up",
  subscribed: "subscribed",
  container_ready: "container ready",
  friday_first_run_delivered: "Friday's first run delivered",
  automation_armed: "automation armed",
  first_autonomous_success: "activated",
};

function Table({ head, rows }: { head: string[]; rows: React.ReactNode[][] }) {
  if (rows.length === 0) return <div className="text-xs text-zinc-500">none</div>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-zinc-500">
            {head.map((h) => (
              <th key={h} className="pb-1 pr-4 font-normal">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-zinc-800 text-zinc-200">
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

function owner(o: { owner_id: string; name: string }) {
  return (
    <Link href={`/admin/users/${encodeURIComponent(o.owner_id)}`} className="text-zinc-100 underline-offset-2 hover:underline">
      {o.name}
    </Link>
  );
}

export function Headline({ seg }: { seg: GrowthSegment }) {
  const h = seg.headline;
  return (
    <Card title={`External owners with automations firing ${h.weeks.length} consecutive weeks`}>
      <div className="flex items-baseline gap-6">
        <MetricStat label={`of ${h.bar} (the PMF bar)`} value={h.count} tone={h.count === 0 ? "unknown" : "default"} />
        <div className="text-xs text-zinc-400">
          weeks {h.weeks[0]} → {h.weeks[h.weeks.length - 1]} · {h.owners.length ? h.owners.join(", ") : "nobody yet"}
        </div>
      </div>
    </Card>
  );
}

export function Owners({ seg }: { seg: GrowthSegment }) {
  const o = seg.owners;
  const fmt = (m: Record<string, number>) => Object.entries(m).map(([k, v]) => `${k} ${v}`).join(" · ") || "—";
  return (
    <Card title="Owners">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <MetricStat label="owners" value={o.total} />
        <MetricStat label="paying" value={o.paid} hint={fmt(o.paid_by_plan)} />
        <MetricStat label="paid by provider" value={fmt(o.paid_by_provider)} />
        <MetricStat label="trials active" value={o.trial_active} />
        <MetricStat label="trials expired" value={o.trial_expired} tone={o.trial_expired ? "unknown" : "default"} />
      </div>
    </Card>
  );
}

export function Activation({ seg }: { seg: GrowthSegment }) {
  const a = seg.activation;
  return (
    <Card title="Activation gate (subscribed → ready → automation armed → first autonomous run succeeds)">
      <div className="grid grid-cols-3 gap-4 md:grid-cols-6">
        {(Object.keys(STAGE_LABEL) as GrowthOwnerStage["stage"][]).map((s) => (
          <MetricStat key={s} label={STAGE_LABEL[s]} value={a.stages[s]} />
        ))}
      </div>
      <div className="mt-4">
        <Table
          head={["owner", "stage", "status", "plan", "Friday kickoff", "first autonomous success"]}
          rows={a.owners.map((o) => [
            owner(o),
            STAGE_LABEL[o.stage],
            o.status ?? "—",
            o.plan ?? "—",
            o.bootstrap_state,
            o.first_autonomous_at ?? "—",
          ])}
        />
      </div>
    </Card>
  );
}

export function NorthStar({ seg }: { seg: GrowthSegment }) {
  return (
    <Card title="North Star — successful runs per week (nightly CEO shown, never counted)">
      <Table
        head={["week", "owners w/ autonomous run", "autonomous ok", "human ok", "autonomous share", "nightly CEO ok"]}
        rows={seg.north_star.weeks.map((w) => [
          w.week,
          w.owners_active,
          w.autonomous_ok,
          w.human_ok,
          w.autonomous_share === null ? "—" : `${Math.round(w.autonomous_share * 100)}%`,
          w.nightly_ok,
        ])}
      />
    </Card>
  );
}

export function RetentionDepth({ seg }: { seg: GrowthSegment }) {
  const s = seg.retention.automation_survival;
  return (
    <Card title="Automation survival (routines ≥30 days old) · depth">
      <div className="grid grid-cols-4 gap-4">
        <MetricStat label="still armed" value={s.armed} />
        <MetricStat label="paused by owner" value={s.paused_manual} />
        <MetricStat label="auto-paused (circuit breaker)" value={s.auto_paused} tone={s.auto_paused ? "error" : "default"} />
        <MetricStat label="archived" value={s.archived} />
      </div>
      <div className="mt-4 grid gap-6 md:grid-cols-2">
        <Table head={["owner", "armed automations", "trigger kinds"]} rows={seg.depth.armed_per_owner.map((o) => [owner(o), o.armed, o.trigger_kinds.join(", ")])} />
        <Table head={["owner", "connected providers"]} rows={seg.depth.providers_per_owner.map((o) => [owner(o), o.providers.join(", ")])} />
      </div>
    </Card>
  );
}

export function Counters({ seg, data }: { seg: GrowthSegment; data: GrowthData }) {
  return (
    <Card title="Counter-metrics (falsifiers)">
      <div className="grid gap-6 md:grid-cols-3">
        <div>
          <div className="mb-1 text-xs text-zinc-500">auto-paused automations</div>
          <Table head={["owner", "routine", "since"]} rows={seg.counters.auto_paused.map((r) => [owner(r), r.title, r.since ?? "—"])} />
        </div>
        <div>
          <div className="mb-1 text-xs text-zinc-500">archived within 7 days of creation</div>
          <Table head={["owner", "routine"]} rows={seg.counters.archived_within_7d.map((r) => [owner(r), r.title])} />
        </div>
        <div>
          <div className="mb-1 text-xs text-zinc-500">provision.gate.blocked, last 7d (CloudWatch, all owners)</div>
          <Table head={["code", "count"]} rows={data.gate_blocked_7d.map((g) => [g.code, g.n])} />
          <div className="mt-3 text-xs text-zinc-500">
            nightly CEO (paid owners): {seg.nightly_ceo.armed} armed · {seg.nightly_ceo.opted_out} opted out ·{" "}
            <span className={seg.nightly_ceo.never_armed ? "text-red-300" : ""}>{seg.nightly_ceo.never_armed} never armed</span>
          </div>
        </div>
      </div>
    </Card>
  );
}

const FUNNEL_EVENTS = [
  "landing_viewed",
  "signup_started",
  "trial_started",
  "first_run_brief_submitted",
  "trial_first_agent_response",
  "chat_completed",
  "trial_converted",
  "subscription_activated",
  "automation_armed",
  "subscription_cancelled",
];

function funnelByWeek(data: GrowthData) {
  const funnel = new Map<string, Record<string, number>>();
  for (const r of data.posthog.funnel) funnel.set(r.week, { ...(funnel.get(r.week) ?? {}), [r.event]: r.owners });
  return funnel;
}

export function Acquisition({ seg, data }: { seg: GrowthSegment; data: GrowthData }) {
  const funnel = funnelByWeek(data);
  return (
    <Card title="Acquisition — client funnel per week (PostHog, unique persons, unsegmented) · signups per week (billing rows)">
      <div className="grid gap-6 md:grid-cols-[1fr_auto_auto]">
        <Table head={["week", ...FUNNEL_EVENTS]} rows={data.weeks.map((w) => [w, ...FUNNEL_EVENTS.map((e) => funnel.get(w)?.[e] ?? 0)])} />
        <Table head={["week", "signups"]} rows={seg.owners.signups_by_week.map((w) => [w.week, w.n])} />
        <div>
          <div className="mb-1 text-xs text-zinc-500">signup_started by first touch</div>
          <Table head={["source", "owners"]} rows={data.posthog.signup_sources.map((s) => [s.source, s.owners])} />
        </div>
      </div>
      <div className="mt-2 text-xs text-zinc-500">
        trial → first agent response is the SEO plan&apos;s old activation read; it is a trial-stage signal only — trials cannot arm automations.
        Churn/NRR are not built (no cancel reason is stored); activated vs cancelled per week is the stand-in.
      </div>
    </Card>
  );
}

export function PosthogCrosscheck({ data }: { data: GrowthData }) {
  const byWeek = new Map<string, Record<string, number>>();
  for (const r of data.posthog.runs) byWeek.set(r.week, { ...(byWeek.get(r.week) ?? {}), [r.kind]: r.n });
  return (
    <Card title="PostHog cross-check — agent_run_completed by initiation (attribution there is lossy; the DB above is the ledger)">
      <Table
        head={["week", "autonomous", "human", "unknown"]}
        rows={data.weeks.map((w) => [w, byWeek.get(w)?.autonomous ?? 0, byWeek.get(w)?.human ?? 0, byWeek.get(w)?.unknown ?? 0])}
      />
    </Card>
  );
}
