"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";
import { formatMoney, formatPct } from "@/lib/launch";

import { refreshLaunch, removeLaunchEntry } from "../_actions/launch";
import type { LaunchOpsData } from "../_lib/api";
import { Table } from "../growth/sections";

// Values starting with =, +, -, or @ are formula triggers in Excel/Sheets when
// the CSV is opened there — prefix with a leading `'` (a no-op text marker in
// both apps) before deciding whether the value also needs quoting.
export function csvEscape(v: unknown): string {
  let s = v === null || v === undefined ? "" : String(v);
  if (/^[=+\-@]/.test(s)) s = `'${s}`;
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function LaunchAdminClient({ data }: { data: LaunchOpsData }) {
  const router = useRouter();
  const [note, setNote] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const refresh = async () => {
    setBusy(true);
    const r = await refreshLaunch();
    setBusy(false);
    setNote(r.ok ? `Refreshed ${(r.data as { refreshed?: number })?.refreshed ?? 0} entries.` : `Refresh failed: ${r.error ?? r.status}`);
    router.refresh();
  };

  const remove = async (ownerId: string) => {
    setBusy(true);
    const r = await removeLaunchEntry(ownerId);
    setBusy(false);
    setNote(r.ok ? `Removed ${ownerId}.` : `Remove failed: ${r.error ?? r.status}`);
    router.refresh();
  };

  const exportCsv = () => {
    const head = ["email", "position", "referral_link", "created_at", "activated"];
    const lines = [head.join(","), ...data.waitlist_rows.map((r) => [r.email, r.position, r.referral_link, r.created_at, r.activated].map(csvEscape).join(","))];
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "launch-waitlist.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const e = data.economics;
  const cb = data.competing_breakdown;
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <MetricStat label="public pot" value={formatMoney(data.public_pot_cents, "usd")} hint={`${data.signups} signed up × $10 + $1,000`} />
        <MetricStat label="funded pot" value={formatMoney(data.funded_pot_cents, "usd")} hint={`${data.pot_counted} first invoices landed`} />
        <MetricStat label="signed up" value={data.signups} />
        <MetricStat label="competing" value={data.competing} hint={`${cb.paid} paid · ${cb.comp_eligible} eligible · ${cb.public} public`} />
        <MetricStat label="on the board" value={data.on_board} />
        <MetricStat label="waitlist" value={data.waitlist.total} hint={`${data.waitlist.top_slots_claimed} of top ${data.schedule.comp_slots} claimed`} />
        <MetricStat label="top slots activated" value={`${data.waitlist.top_slots_activated} / ${data.schedule.comp_slots}`} />
        <MetricStat label="referrals signed up" value={data.referrals.signed_up} />
        <MetricStat label="referrals paid" value={data.referrals.paid} hint={`${data.referrals.months_banked} months banked`} />
        <MetricStat label="P&L before credits" value={formatMoney(e.pnl_cents, "usd")} tone={e.pnl_cents < 0 ? "error" : "default"} />
        <MetricStat label="net after AWS credits" value={formatMoney(e.net_cents, "usd")} hint={`credits ${formatMoney(e.aws_credits_cents, "usd")}`} />
      </div>

      <Card
        title="Actions"
        action={
          <div className="flex gap-2">
            <button type="button" onClick={exportCsv} className="rounded-md border border-border bg-card px-3 py-1.5 text-xs text-foreground hover:bg-accent">Export waitlist CSV</button>
            <button type="button" onClick={refresh} disabled={busy} className="rounded-md border border-border bg-card px-3 py-1.5 text-xs text-foreground hover:bg-accent disabled:opacity-50">{busy ? "Refreshing…" : "Refresh all"}</button>
          </div>
        }
      >
        <p className="text-xs text-muted-foreground">{note ?? "Export the top rows for the launch email; refresh before judging."}</p>
      </Card>

      <Card title="Economics (sheet lines)">
        <Table
          head={["line", "amount"]}
          rows={[
            ["paid entrants profit", formatMoney(e.paid_profit_cents, "usd")],
            ["comped waitlist cost", formatMoney(-e.comp_cost_cents, "usd")],
            ["referral free months cost", formatMoney(-e.referral_cost_cents, "usd")],
            ["competition P&L", formatMoney(e.pnl_cents, "usd")],
            ["AWS credits", formatMoney(e.aws_credits_cents, "usd")],
            ["net", formatMoney(e.net_cents, "usd")],
          ]}
        />
      </Card>

      <Card title={`Entries (${data.entries.length})`}>
        <Table
          head={["#", "entrant", "owner", "pos", "eligible", "public", "baseline", "window", "lift", "growth", "refreshed", "error", ""]}
          rows={data.entries.map((r, i) => [
            i + 1,
            r.display_name ?? "—",
            r.owner_id,
            r.waitlist_position ?? "—",
            r.comp_eligible ? "yes" : "no",
            r.public ? "yes" : "no",
            formatMoney(r.baseline_cents, r.currency ?? "usd"),
            formatMoney(r.window_cents, r.currency ?? "usd"),
            formatMoney(r.delta_cents, r.currency ?? "usd"),
            formatPct(r.pct),
            r.refreshed_at ?? "never",
            r.refresh_error ?? "",
            <button
              key="remove"
              type="button"
              onClick={() => remove(r.owner_id)}
              disabled={busy}
              className="rounded-md border border-border px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
            >
              Remove
            </button>,
          ])}
        />
      </Card>
    </div>
  );
}
