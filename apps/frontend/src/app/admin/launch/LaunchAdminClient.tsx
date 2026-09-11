"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { Card } from "@/components/admin/Card";
import { ConfirmActionDialog } from "@/components/admin/ConfirmActionDialog";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { MetricStat } from "@/components/admin/MetricStat";
import { Button } from "@/components/ui/button";
import { formatMoney, formatPct } from "@/lib/launch";

import { closeLaunchSheet, refreshLaunch, removeLaunchEntry, setLaunchTeamEntry } from "../_actions/launch";
import type { LaunchCloseResult, LaunchOpsSheet } from "../_lib/api";
import { Table } from "../growth/sections";

// Values starting with =, +, -, or @ are formula triggers in Excel/Sheets when
// the CSV is opened there — prefix with a leading `'` (a no-op text marker in
// both apps) before deciding whether the value also needs quoting.
export function csvEscape(v: unknown): string {
  let s = v === null || v === undefined ? "" : String(v);
  if (/^[=+\-@]/.test(s)) s = `'${s}`;
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** CSV over the JOINED sheet rows, in the payload's own (effective_position,
 * position) order — built client-side off `sheet_rows`. */
export function sheetCsv(rows: LaunchOpsSheet["sheet_rows"]): string {
  const head = [
    "email",
    "position",
    "effective_position",
    "display_name",
    "public",
    "referral_link",
    "created_at",
    "joined_at",
    "user_id",
    "referred_by",
    "referrals_signed_up",
    "referrals_subscribed",
    "winner",
    "compete_eligible",
    "coupon_attached_at",
    "winner_email_sent_at",
  ];
  return [
    head.join(","),
    ...rows.map((r) =>
      [
        r.email,
        r.position,
        r.effective_position,
        r.display_name,
        r.public,
        r.referral_link,
        r.created_at,
        r.joined_at,
        r.user_id,
        r.referred_by,
        r.referrals_signed_up,
        r.referrals_subscribed,
        r.winner,
        r.compete_eligible,
        r.coupon_attached_at,
        r.winner_email_sent_at,
      ]
        .map(csvEscape)
        .join(","),
    ),
  ].join("\n");
}

function describeClose(r: LaunchCloseResult): string {
  const skipped = (r.coupon_skipped_nonflat ?? 0) > 0 ? ` / ${r.coupon_skipped_nonflat} skipped (non-Workspace)` : "";
  return (
    `Closed ${r.closed_at ?? "—"}: ${r.winners ?? 0} winners flagged of ${r.sheet_count ?? 0} on the sheet ` +
    `(${r.paying ?? 0} paying) · roster ${r.final_roster_count ?? 0} · pot ${formatMoney(r.final_pot_cents ?? 0, "usd")} · ` +
    `coupons ${r.coupons_attached ?? 0} attached / ${r.coupon_failures ?? 0} failed${skipped} · ` +
    `emails ${r.emails_sent ?? 0} sent (${r.emails_winner ?? 0} winner / ${r.emails_competing ?? 0} competing / ` +
    `${r.emails_not_selected ?? 0} not selected) / ${r.email_failures ?? 0} failed`
  );
}

export function LaunchAdminClient({ data }: { data: LaunchOpsSheet }) {
  const router = useRouter();
  const [note, setNote] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [closeStarted, setCloseStarted] = React.useState(false);
  const [closeError, setCloseError] = React.useState<string | null>(null);

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

  const close = async (force: boolean) => {
    setBusy(true);
    setCloseError(null);
    const r = await closeLaunchSheet(force);
    setBusy(false);
    if (r.ok) {
      // 202 — the close runs as a background task; the summary lands on the
      // sheet payload as close_result.
      setCloseStarted(true);
      setNote("Closing… (refresh for progress — the summary appears below when it finishes)");
    } else {
      // A 422 rides {"detail": {"errors": {field: msg}}} (the settings card's
      // envelope); a 409/502 detail is a plain string surfaced via r.error.
      const detail = (r.data as { detail?: unknown } | undefined)?.detail;
      const errors =
        detail && typeof detail === "object" && "errors" in detail
          ? (detail as { errors?: Record<string, string> }).errors
          : null;
      const msg = errors
        ? Object.entries(errors)
            .map(([field, m]) => `${field}: ${m}`)
            .join("; ")
        : (r.error ?? JSON.stringify(r.data ?? ""));
      setCloseError(`Close failed (${r.status}): ${msg}`);
    }
    router.refresh();
  };

  const exportCsv = () => {
    const blob = new Blob([sheetCsv(data.sheet_rows)], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "launch-sheet.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const closeResult = data.close_result;
  const closeRunning = data.close_running || (closeStarted && !closeResult);
  const closeFailures =
    closeResult != null && ((closeResult.coupon_failures ?? 0) > 0 || (closeResult.email_failures ?? 0) > 0);
  const closeSkipped = (closeResult?.coupon_skipped_nonflat ?? 0) > 0;

  return (
    <div className="space-y-6">
      {data.closed ? (
        <Card title="Sheet closed">
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
            <MetricStat label="closed at" value={data.closed_at ?? "—"} />
            <MetricStat label="final roster" value={data.final_roster_count ?? "—"} hint="winners + paying subscribers" />
            <MetricStat
              label="final pot (internal)"
              value={data.final_pot_cents !== null ? formatMoney(data.final_pot_cents, "usd") : "—"}
              hint="frozen at close — never shown publicly"
            />
          </div>
        </Card>
      ) : null}

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <MetricStat label="signups" value={data.signups} hint="every row incl. attribution-only" />
        <MetricStat label="on the sheet" value={data.sheet.joined} hint={`${data.sheet.total} rows total · joins are explicit`} />
        <MetricStat label="winners" value={data.sheet.winners} hint={`top ${data.schedule.comp_slots} by effective position at close`} />
        <MetricStat
          label={data.closed ? "final pot (internal)" : "projected pot (internal)"}
          value={formatMoney(data.closed ? (data.final_pot_cents ?? 0) : data.projected_pot_cents, "usd")}
          hint={`${formatMoney(data.pot_front_cents, "usd")} + ${formatMoney(data.pot_per_entrant_cents, "usd")} × roster + counted teams (${formatMoney(data.pot_per_entrant_cents, "usd")} + ${formatMoney(data.pot_per_seat_cents, "usd")} × seats) — never shown publicly`}
        />
        <MetricStat label="competing" value={data.competing} />
        <MetricStat label="on the board" value={data.on_board} />
        <MetricStat label="referrals signed up" value={data.referrals.signed_up} />
        <MetricStat label="referrals subscribed" value={data.referrals.subscribed} hint="the counted kind — drives jumps + credits" />
      </div>

      <Card
        title="Actions"
        action={
          <div className="flex gap-2">
            <button type="button" onClick={exportCsv} className="rounded-md border border-border bg-card px-3 py-1.5 text-xs text-foreground hover:bg-accent">Export sheet CSV</button>
            <button type="button" onClick={refresh} disabled={busy} className="rounded-md border border-border bg-card px-3 py-1.5 text-xs text-foreground hover:bg-accent disabled:opacity-50">{busy ? "Working…" : "Refresh all"}</button>
            {closeRunning ? (
              // A run is LIVE — neither a first close nor a force belongs in
              // the UI. The disabled state doubles as the progress indicator;
              // a crashed run surfaces its error result (or the admin
              // refreshes into the failed state before forcing).
              <Button type="button" variant="outline" size="sm" disabled>
                Close in progress…
              </Button>
            ) : !data.closed ? (
              <ConfirmActionDialog
                confirmText="close"
                actionLabel="Close the sign-up sheet — flag winners, freeze the pot, attach coupons, email winners"
                destructive
                onConfirm={() => close(false)}
              >
                <Button type="button" variant="destructive" size="sm" disabled={busy}>
                  Close the sheet
                </Button>
              </ConfirmActionDialog>
            ) : (
              <ConfirmActionDialog
                confirmText="close"
                actionLabel="Re-run the close (force) — retries failed coupons/emails only; winners and the pot stay frozen"
                onConfirm={() => close(true)}
              >
                <Button type="button" variant="outline" size="sm" disabled={busy}>
                  Re-run close (force)
                </Button>
              </ConfirmActionDialog>
            )}
          </div>
        }
      >
        <p className="text-xs text-muted-foreground">{note ?? "Export the sheet for the launch email; refresh before judging."}</p>
        {closeError ? <ErrorBanner error={closeError} /> : null}
        {closeRunning ? (
          <p className="text-xs text-foreground">Closing… (refresh for progress — the summary appears here when it finishes)</p>
        ) : null}
        {closeResult?.error ? (
          <ErrorBanner error={`Close run failed (${closeResult.error}) — re-run with force to retry.`} />
        ) : null}
        {closeResult && !closeResult.error ? (
          <div className="mt-2 space-y-2">
            <p className="text-xs text-foreground">{describeClose(closeResult)}</p>
            {closeSkipped ? (
              <ErrorBanner
                error={`${closeResult.coupon_skipped_nonflat} paying winner(s) are not on the Workspace plan — no coupon attached; they got the "free Workspace month at checkout" email instead.`}
              />
            ) : null}
            {closeFailures ? (
              <div className="flex items-center gap-2">
                <ErrorBanner error="Some coupon attaches or winner emails failed — a forced rerun retries only the failed steps." />
                <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => close(true)}>
                  Retry (force)
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}
      </Card>

      <Card title={`Sheet (${data.sheet_rows.length} joined)`}>
        <Table
          head={["eff #", "#", "name", "email", "joined", "refs subbed", "refs signed", "winner", "eligible", "coupon", "emailed"]}
          rows={data.sheet_rows.map((r) => [
            r.effective_position,
            r.position,
            r.display_name ?? "—",
            r.email,
            r.joined_at ?? "—",
            r.referrals_subscribed,
            r.referrals_signed_up,
            r.winner ? "yes" : "",
            r.compete_eligible ? "yes" : "no",
            r.coupon_attached_at ? "yes" : "",
            r.winner_email_sent_at ? "yes" : "",
          ])}
        />
      </Card>

      <TeamEntriesCard data={data} busy={busy} onChanged={() => router.refresh()} />

      <Card title={`Board entries (${data.entries.length})`}>
        <Table
          head={["#", "entrant", "owner", "sheet pos", "paid", "public", "baseline", "window", "lift", "growth", "refreshed", "error", ""]}
          rows={data.entries.map((r, i) => [
            i + 1,
            r.display_name ?? "—",
            r.owner_id,
            r.waitlist_position ?? "—",
            r.has_subscription ? "yes" : "no",
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

/**
 * Team entries — the founders' manual lane. Teams never join the sheet (POST
 * /launch/join refuses an org context), so a submission sits here until a
 * founder marks it counted; only then does it reach the pot. The seat count
 * is editable because the submitter's number is a claim, not a fact.
 */
export function TeamEntriesCard({
  data,
  busy,
  onChanged,
}: {
  data: LaunchOpsSheet;
  busy: boolean;
  onChanged: () => void;
}) {
  const [saving, setSaving] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const save = async (ownerId: string, body: { seats?: number; counted?: boolean }) => {
    setSaving(ownerId);
    setError(null);
    const r = await setLaunchTeamEntry(ownerId, body);
    setSaving(null);
    if (!r.ok) {
      // Same 422 envelope as the settings PUT: {detail: {errors: {field: msg}}}.
      const detail = (r.data as { detail?: unknown } | undefined)?.detail;
      const errors =
        detail && typeof detail === "object" && "errors" in detail
          ? (detail as { errors?: Record<string, string> }).errors
          : null;
      setError(
        errors
          ? Object.entries(errors)
              .map(([f, m]) => `${f}: ${m}`)
              .join("; ")
          : `Team entry update failed (${r.status}): ${r.error ?? ""}`,
      );
    }
    onChanged();
  };

  return (
    <Card title={`Team entries (${data.teams.counted} of ${data.teams.total} counted)`}>
      <p className="text-xs text-muted-foreground">
        Teams raise their hand here and the founders enter them by hand — nothing is automatic. Only a counted entry
        contributes to the pot: {formatMoney(data.pot_per_entrant_cents, "usd")} +{" "}
        {formatMoney(data.pot_per_seat_cents, "usd")} per seat, {formatMoney(data.teams.contribution_cents, "usd")}{" "}
        counted so far. If a team wins, their free month covers the $50 Workspace plan. Ultra is $150/month; both
        plans include unlimited members and inference costs are separate.
      </p>
      {error ? <ErrorBanner error={error} /> : null}
      <Table
        head={["team", "seats", "counts", "contribution", "submitter", "submitted", "edited", "note"]}
        rows={data.team_entries.map((t) => [
          t.team_name,
          <input
            // Keyed on the SERVER's seat count: a failed save must not leave
            // the admin looking at a number the server never took.
            key={`${t.owner_id}:${t.seats}`}
            type="number"
            min={1}
            max={500}
            step={1}
            defaultValue={t.seats}
            disabled={busy || saving === t.owner_id}
            aria-label={`Seats for ${t.team_name}`}
            onBlur={(e) => {
              const next = Number(e.target.value);
              if (next !== t.seats) void save(t.owner_id, { seats: next });
            }}
            className="w-20 rounded-md border border-border bg-card px-2 py-0.5 text-xs text-foreground"
          />,
          <label key="counted" className="flex items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={t.counted}
              disabled={busy || saving === t.owner_id}
              aria-label={`Count ${t.team_name} toward the pot`}
              onChange={(e) => void save(t.owner_id, { counted: e.target.checked })}
            />
            {t.counted ? "yes" : "no"}
          </label>,
          formatMoney(t.contribution_cents, "usd"),
          t.submitter_email || t.submitter_user_id,
          t.submitted_at ?? "—",
          // A resubmission keeps submitted_at, so without this column a
          // post-review edit moves nothing on screen.
          t.updated_at ?? "—",
          t.note || "—",
        ])}
      />
    </Card>
  );
}
