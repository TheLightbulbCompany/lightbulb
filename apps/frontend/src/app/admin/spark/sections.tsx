import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";

import type { Kn, SparkData } from "../_lib/api";

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

/** "k / n (p%)"; a dash when there is nothing to divide by (never a fake 0%). */
export const kn = (v: Kn | null | undefined) =>
  !v || v.n === 0 ? "—" : `${v.k} / ${v.n} (${Math.round((v.k / v.n) * 100)}%)`;

const weekLabel = (w: string, partial: boolean) => (partial ? `${w} (partial)` : w);

export function Headline({ data }: { data: SparkData }) {
  const unavailable = data.brain_unavailable_installs_24h;
  return (
    <Card title="Headline">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
        <MetricStat label="Weekly Active Doers" value={data.wad.last7} hint="last 7 days, human tasks on 2+ days" />
        <MetricStat label="previous 7 days" value={data.wad.prev7} />
        <MetricStat
          label="installs with brain.unavailable"
          value={unavailable}
          hint="last 24h"
          tone={unavailable > 0 ? "error" : "default"}
        />
      </div>
    </Card>
  );
}

export function Funnel({ data }: { data: SparkData }) {
  return (
    <Card title="Funnel: installs in the last 30 days reaching each step">
      <Table head={["Step", "Installs"]} rows={data.funnel.map((f) => [f.step, kn(f)])} />
      <p className="mt-2 text-xs text-muted-foreground">
        Download clicks (anonymous web traffic, not a step):{" "}
        {data.downloads.map((d) => `${d.key} ${d.count}`).join(", ") || "none"}. Channel attribution for non-referred
        users is not yet measurable.
      </p>
    </Card>
  );
}

export function Cohorts({ data }: { data: SparkData }) {
  return (
    <Card title="Install cohorts by week and platform">
      <Table
        head={["Week", "Platform", "Installs", "Activated 24h", "Activated d7", "Week-4 retained"]}
        rows={data.cohorts.map((c) => [
          weekLabel(c.week, c.partial),
          c.platform,
          c.installs,
          kn(c.activated_24h),
          kn(c.activated_d7),
          c.w4 ? kn(c.w4) : "too early",
        ])}
      />
    </Card>
  );
}

export function Channels({ data }: { data: SparkData }) {
  return (
    <Card title="Activated in 24h by channel (installs in the last 60 days; creator codes and referral)">
      <Table head={["Channel", "Activated 24h"]} rows={data.channels.map((c) => [c.channel, kn(c.activated_24h)])} />
    </Card>
  );
}

export function People({ data }: { data: SparkData }) {
  return (
    <Card title="People (installs in the last 60 days, newest first)">
      <Table
        head={["Installed", "Platform", "Version", "Brain", "Channel", "Furthest step", "Last task", "Email"]}
        rows={data.people.map((p) => [p.installed, p.platform, p.version, p.provider, p.channel, p.furthest, p.last_task ?? "—", p.email ?? "—"])}
      />
    </Card>
  );
}

/** Reliability guardrail, shown below the growth numbers. */
export function Reliability({ data }: { data: SparkData }) {
  const s = data.success;
  return (
    <div className="space-y-6">
      <Card title="Task success: human completed / (completed + failed)">
        <Table
          head={["Week", "Platform", "Success"]}
          rows={s.weekly.map((w) => [weekLabel(w.week, w.partial), w.platform, kn(w.success)])}
        />
      </Card>
      <div className="grid gap-6 md:grid-cols-2">
        <Card title="By app version, 30d">
          <Table head={["Version", "Success"]} rows={s.by_version.map((r) => [r.key, kn(r.success)])} />
        </Card>
        <Card title="By brain provider, 30d">
          <Table head={["Provider", "Success"]} rows={s.by_provider.map((r) => [r.key, kn(r.success)])} />
        </Card>
        <Card title="Top task_failed reasons, 7d">
          <Table head={["Reason", "Count"]} rows={s.failed_reasons.map((r) => [r.key, r.count])} />
        </Card>
        <Card title="Top problem_raised kinds, 7d">
          <Table head={["Kind", "Count"]} rows={s.problem_kinds.map((r) => [r.key, r.count])} />
        </Card>
      </div>
    </div>
  );
}

const NO_DATA = <div className="text-xs text-muted-foreground">no data yet</div>;
const usd = (v: number) => `$${v.toFixed(2)}`;
const counts = (c: { key: string; count: number }[]) => c.map((x) => `${x.key} ${x.count}`).join(", ") || "none";
const WITHIN: Record<string, string> = { lt5m: "< 5 min", lt30m: "< 30 min", lt2h: "< 2 h", lt24h: "< 24 h", later: "later", never: "never" };

export function Sources({ data }: { data: SparkData }) {
  return (
    <Card title="How they heard (installs in the last 60 days)">
      {data.heard_from ? (
        <Table
          head={["Answer", "Installs", "Activated 24h"]}
          rows={data.heard_from.map((h) => [h.choice, h.installs, kn(h.activated_24h)])}
        />
      ) : (
        NO_DATA
      )}
    </Card>
  );
}

export function Setup({ data }: { data: SparkData }) {
  const s = data.setup;
  return (
    <div className="space-y-6">
      <Card title="Time from first launch to first human task (installs 24h+ old, last 30 days)">
        <Table head={["Within", "Installs"]} rows={data.time_to_first_task.map((b) => [WITHIN[b.bucket] ?? b.bucket, kn(b)])} />
      </Card>
      <Card title="Setup, last 30 days (people)">
        {s ? (
          <div className="space-y-4">
            <Table
              head={["Step", "Shown", "Completed", "Skipped"]}
              rows={s.steps.map((r) => [r.step, r.shown, kn(r.completed), kn(r.skipped)])}
            />
            <Table
              head={["Permission", "Prompted", "Denied"]}
              rows={s.permissions.map((r) => [r.permission, r.prompted, kn(r.denied)])}
            />
            <p className="text-xs text-muted-foreground">
              Brain sign-in failures: {counts(s.signin_failures)}. Tasks started and not finished after 30 minutes:{" "}
              {s.unfinished ? kn(s.unfinished) : "no data yet"}.
            </p>
          </div>
        ) : (
          NO_DATA
        )}
      </Card>
    </div>
  );
}

export function Money({ data }: { data: SparkData }) {
  const { subs, voice, price_usd } = data.money;
  return (
    <Card title="Money">
      <div className="space-y-4">
        {subs ? (
          <>
            <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
              <MetricStat label="Paying users" value={subs.paying} hint="latest status is active" />
              <MetricStat label="MRR at list price" value={`$${subs.mrr_usd}`} hint={`paying x $${price_usd}`} />
              <MetricStat label="Status unknown" value={subs.unknown} hint="subscribed before status tracking" />
            </div>
            <Table head={["Account created", "Reached paying"]} rows={subs.trial_to_paid.map((t) => [weekLabel(t.month, t.partial), kn(t)])} />
            <Table
              head={["Cancelled in", "Count", "Reasons", "Feedback"]}
              rows={subs.cancellations.map((c) => [c.month, c.count, counts(c.reasons), counts(c.feedback)])}
            />
          </>
        ) : (
          NO_DATA
        )}
        {voice ? (
          <>
            <Table
              head={["Month", "Minutes", "Users", "Median / user", "p90 / user", "Cost (estimate)"]}
              rows={voice.months.map((m) => [m.month, m.minutes, m.users, m.p50, m.p90, usd(m.cost_usd)])}
            />
            <p className="text-xs text-muted-foreground">
              Cost is an estimate: minutes x ${voice.rate_usd_per_min}/min. Top 20 by voice minutes this month.
            </p>
            <Table
              head={["User", "Status", "Minutes", "Cost (estimate)", "Margin"]}
              rows={voice.top.map((u) => [u.email ?? "—", u.status, u.minutes, usd(u.cost_usd), usd(u.margin_usd)])}
            />
          </>
        ) : (
          NO_DATA
        )}
      </div>
    </Card>
  );
}

export function Signals({ data }: { data: SparkData }) {
  const { stopped, automations, features, crash, versions, updates } = data.signals;
  return (
    <div className="space-y-6">
      <div className="grid gap-6 md:grid-cols-2">
        <Card title="Signs of a bad result, last 30 days">
          {stopped ? (
            <div className="space-y-2 text-xs text-foreground">
              <p>
                {/* a stopped automation task has no counted start, so stops can exceed starts: no share then */}
                Stopped {stopped.k} / started {stopped.n}
                {stopped.k <= stopped.n && stopped.n > 0 ? ` (${Math.round((stopped.k / stopped.n) * 100)}%)` : ""}
              </p>
              <p className="text-muted-foreground">How they stopped: {counts(stopped.how)}.</p>
            </div>
          ) : (
            NO_DATA
          )}
        </Card>
        <Card title="Automations, last 30 days">
          {automations ? (
            <div className="space-y-2 text-xs text-foreground">
              <p>
                Runs {automations.runs}; started {kn(automations.ok)}; with a brain {kn(automations.with_brain)}
              </p>
              <p className="text-muted-foreground">
                Started = the actions were started, not that the work finished. {automations.people_30d} people.
              </p>
            </div>
          ) : (
            NO_DATA
          )}
        </Card>
      </div>
      <Card title="Features, last 30 days (people)">
        {features ? (
          <Table
            head={["Feature", "People, 30 days", "People, 7 days"]}
            rows={features.map((f) => [f.feature, f.people_30d, f.people_7d ?? "—"])}
          />
        ) : (
          NO_DATA
        )}
      </Card>
      <Card title="Versions and crashes">
        <div className="space-y-4">
          {versions || updates ? (
            <div className="grid gap-6 md:grid-cols-2">
              {versions ? (
                <Table head={["Version in use, 7 days", "People"]} rows={versions.map((v) => [v.version, v.people_7d])} />
              ) : null}
              {updates ? (
                <Table head={["Updated to, 30 days", "Installs"]} rows={updates.map((u) => [u.to_version, u.installs_30d])} />
              ) : null}
            </div>
          ) : null}
          {crash ? (
            <p className="text-xs text-muted-foreground">
              People with a crash in 7 days, of people who opened the app: {kn(crash)} (about: someone who updated that week
              counts twice). Undercounts crashes found more than 10 minutes after they happened.
            </p>
          ) : null}
          {!versions && !updates && !crash ? NO_DATA : null}
        </div>
      </Card>
    </div>
  );
}

export function Trials({ data }: { data: SparkData }) {
  const t = data.trials;
  return (
    <Card title="Trials (started in the last 60 days)">
      {t ? (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
            <MetricStat label="Started" value={t.started} />
            <MetricStat label="Still running" value={t.running} />
            <MetricStat label="Used all minutes" value={t.ended_minutes} hint="ended by the voice limit" />
            <MetricStat label="Ran out of days" value={t.ended_days} hint="ended by the 7 days" />
            <MetricStat label="Subscribed" value={kn(t.subscribed)} />
          </div>
          <p className="text-xs text-muted-foreground">
            Voice minutes in the trial: {t.minutes ? `median ${t.minutes.median}, p90 ${t.minutes.p90}` : "—"}. Account to first
            conversation, median: {t.hours_to_first_conversation === null ? "—" : `${t.hours_to_first_conversation} h`}. A person
            counts once, as paying, else how the server ended the trial, else inferred from minutes and days, else running.
          </p>
          <Table
            head={["Paywall, of people who started", "People"]}
            rows={[
              ["Saw it (trial ended)", kn(t.paywall.saw)],
              ["Clicked Upgrade", kn(t.paywall.clicked)],
              ["Subscribed", kn(t.paywall.subscribed)],
            ]}
          />
          <Table
            head={["Email", "Started", "Minutes", "Status"]}
            rows={t.people.map((p) => [p.email ?? "—", p.started, p.minutes, p.inferred ? `${p.status} (inferred)` : p.status])}
          />
        </div>
      ) : (
        NO_DATA
      )}
    </Card>
  );
}

export function Invites({ data }: { data: SparkData }) {
  const i = data.invites;
  return (
    <Card title="Invites (last 60 days)">
      {i ? (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <MetricStat label="Copied their link" value={i.copied} hint="people" />
            <MetricStat label="Credited a day" value={i.credited_days} hint={`${i.referrers_credited} referrers`} />
            <MetricStat label="Invited people" value={i.invited} />
            <MetricStat label="Invited, subscribed" value={kn(i.subscribed)} />
          </div>
          <p className="text-xs text-muted-foreground">
            Invited people who signed in {kn(i.signed_in)}; did a first task {kn(i.first_task)}.
          </p>
          <Table
            head={["Referrer", "Links copied", "Friends signed up", "Friends with a task", "Friends paying"]}
            rows={i.referrers.map((r) => [r.email ?? "—", r.copies, r.friends, r.tasks, r.paying])}
          />
        </div>
      ) : (
        NO_DATA
      )}
    </Card>
  );
}
