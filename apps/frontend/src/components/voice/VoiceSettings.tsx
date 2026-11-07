"use client";

// Settings, the fourth place (spec 2026-11-07 §5). The chat shell's Settings panels, rendered here beside the rail
// (SettingsPlace itself needs the chat shell's panel router), with Billing & minutes first.
import "@/app/settings/settings.css";
import "@/components/settings/settings-place.css";
import { PANELS } from "@/components/settings/SettingsPlace";
import { DailySummaryCard } from "@/components/settings/DailySummaryCard";
import { SessionNotificationsCard } from "@/components/settings/SessionNotificationsCard";
import { useAgentConfig, useUsage } from "./data";

export function BillingMinutes() {
  const { data: u } = useUsage();
  const { data: config, save } = useAgentConfig();
  if (!u) return null;
  const pct = Math.min(100, Math.round((u.used / u.included) * 100));
  const rate = `$${u.overageRate.toFixed(2)}`;
  const runOut = config?.runOut ?? u.runOut;
  const choice = (id: "keep" | "forward", label: string, note: string) => <label className="vx-choice">
    <input type="radio" name="runOut" checked={runOut === id} onChange={() => config && save({ ...config, runOut: id })} />
    <span>{label}<small>{note}</small></span>
  </label>;
  return <section className="vx-card vx-billing" aria-labelledby="vx-billing-h">
    <h2 className="vx-h" id="vx-billing-h">Billing &amp; minutes</h2>
    <p className="vx-minutes"><span className="vx-number">{u.used.toLocaleString()}</span> of {u.included.toLocaleString()} min used this month</p>
    <div className="vx-meter" role="meter" aria-label="Minutes used" aria-valuemin={0} aria-valuemax={u.included} aria-valuenow={Math.min(u.used, u.included)}>
      <span style={{ width: `${pct}%` }} /></div>
    <p className="vx-sub">$250 a month includes {u.included.toLocaleString()} minutes. After that, {rate} a minute.</p>
    <fieldset className="vx-grid vx-runout" disabled={!config}>
      <legend className="vx-label">When the minutes run out</legend>
      {choice("keep", `Keep answering at ${rate}/min`, "No caller is ever turned away.")}
      {choice("forward", "Send calls to my phone", "The assistant stops answering and calls ring you.")}
    </fieldset>
  </section>;
}

export function VoiceSettings() {
  const { members: Members, approvals: Approvals, connections: Connections } = PANELS;
  return <div className="vx-page vx-settings">
    <h1 className="vx-title">Settings</h1>
    <BillingMinutes />
    <Members />
    <Connections />
    <Approvals />
    <div className="settings-panel" id="panel-notifications">
      <h2 className="settings-page-title">Notifications</h2>
      <div className="settings-rows"><DailySummaryCard rows /><SessionNotificationsCard rows /></div>
    </div>
  </div>;
}
