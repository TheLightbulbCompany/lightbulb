"use client";

// Settings, the fourth place (spec 2026-11-07 §5). The chat shell's Settings panels, rendered here beside the rail
// (SettingsPlace itself needs the chat shell's panel router), with Billing & minutes first.
import { useSyncExternalStore } from "react";
import "@/app/settings/settings.css";
import "@/components/settings/settings-place.css";
import { PANELS } from "@/components/settings/SettingsPlace";
import { DailySummaryCard } from "@/components/settings/DailySummaryCard";
import { SessionNotificationsCard } from "@/components/settings/SessionNotificationsCard";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { useAgentConfig, useUsage } from "./data";
import { VoicePlanCard } from "./VoicePlanCard";
import { DesktopUpdater } from "@/components/desktop/DesktopUpdater";
import { isDesktopApp } from "@/lib/desktopNotify";

const noSubscribe = () => () => {};

export function BillingMinutes() {
  const { data: u, error, mutate } = useUsage();
  const { data: config, save } = useAgentConfig();
  if (error) return <section className="vx-card" role="alert"><h2 className="vx-h">Billing &amp; minutes</h2><p>Couldn&apos;t load measured usage. <button type="button" onClick={() => void mutate()}>Retry</button></p></section>;
  if (!u) return null;
  if (!u.paidAllowance) return <section className="vx-card vx-billing" aria-labelledby="vx-billing-h">
    <h2 className="vx-h" id="vx-billing-h">Billing &amp; minutes</h2>
    <p className="vx-minutes"><span className="vx-number">{u.used.toLocaleString()}</span> voice min used {u.periodStart && u.periodEnd ? "this billing period" : "this month"}</p>
    <p className="vx-sub">Included minutes apply while your voice plan has confirmed paid access. Check your plan status above.</p>
  </section>;
  const pct = Math.min(100, Math.round((u.used / u.included) * 100));
  const rate = `$${u.overageRate.toFixed(2)}`;
  const runOut = config?.runOut ?? u.runOut;
  const choice = (id: "keep" | "forward", label: string, note: string) => <label className="vx-choice">
    <input type="radio" name="runOut" checked={runOut === id} onChange={() => config && save({ ...config, runOut: id })} />
    <span>{label}<small>{note}</small></span>
  </label>;
  return <section className="vx-card vx-billing" aria-labelledby="vx-billing-h">
    <h2 className="vx-h" id="vx-billing-h">Billing &amp; minutes</h2>
    <p className="vx-minutes"><span className="vx-number">{u.used.toLocaleString()}</span> of {u.included.toLocaleString()} min used {u.periodStart && u.periodEnd ? "this billing period" : "this month"}</p>
    {u.periodStart && u.periodEnd && <p className="vx-sub">Billing period: {new Date(u.periodStart).toLocaleDateString(undefined, { timeZone: "UTC" })} – {new Date(u.periodEnd).toLocaleDateString(undefined, { timeZone: "UTC" })} (UTC).</p>}
    <div className="vx-meter" role="meter" aria-label="Minutes used" aria-valuemin={0} aria-valuemax={u.included} aria-valuenow={Math.min(u.used, u.included)}>
      <span style={{ width: `${pct}%` }} /></div>
    <p className="vx-sub">Your included allowance is {u.included.toLocaleString()} minutes. Your voice plan above shows its current billing status.</p>
    <fieldset className="vx-grid vx-runout" disabled={!config}>
      <legend className="vx-label">When the minutes run out</legend>
      {choice("keep", `Keep answering at ${rate}/min`, "No caller is ever turned away.")}
      {choice("forward", "Send calls to my phone", "The assistant stops answering and calls ring you.")}
    </fieldset>
  </section>;
}

/** One section failing (7 Nov: the whole page went to "Something went wrong") must not take the others with it. */
function Section({ name, children }: { name: string; children: React.ReactNode }) {
  return <ErrorBoundary fallback={<section className="vx-card" role="alert"><h2 className="vx-h">{name}</h2>
    <p className="vx-sub">This section couldn&apos;t load. The rest of Settings still works.</p></section>}>{children}</ErrorBoundary>;
}

export function VoiceSettings() {
  const desktop = useSyncExternalStore(noSubscribe, isDesktopApp, () => false);
  const { members: Members, approvals: Approvals, connections: Connections } = PANELS;
  return <div className="vx-page vx-settings">
    <h1 className="vx-title">Settings</h1>
    <Section name="Voice plan"><VoicePlanCard /></Section>
    <Section name="Billing & minutes"><BillingMinutes /></Section>
    <Section name="Members"><Members /></Section>
    <Section name="Connections"><Connections /></Section>
    <Section name="Approvals"><Approvals /></Section>
    <Section name="Notifications"><div className="settings-panel" id="panel-notifications">
      <h2 className="settings-page-title">Notifications</h2>
      <div className="settings-rows"><DailySummaryCard rows /><SessionNotificationsCard rows /></div>
    </div></Section>
    {desktop && <Section name="Desktop updates"><section className="vx-card" aria-labelledby="vx-desktop-updates-h">
      <h2 className="vx-h" id="vx-desktop-updates-h">Desktop updates</h2>
      <DesktopUpdater />
    </section></Section>}
  </div>;
}
