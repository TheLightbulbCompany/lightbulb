"use client";

// The card under Lightbulb's "Set up below. Confirm on the card." (lane A's propose route): the schedule
// in the viewer's zone, the next runs, who runs it and what it may spend. Only Turn on activates it;
// typed text never does (contracts-v2 §8). Not now deletes the draft.

import { useState, type ReactElement } from "react";
import { formatSchedule, specialistLabel, type AutomationView } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { money, useAutomation, useAutomationActions, viewerTimeZone } from "./automations";

export function AutomationConfirmCard({ automationId, viewerTz = viewerTimeZone() }: { automationId: string; viewerTz?: string }): ReactElement {
  const { data: a, error } = useAutomation(automationId);
  const { act, remove } = useAutomationActions();
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  if (gone || (!a && error)) {
    return <p className="mt-1.5 max-w-xl rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">
      {gone ? "Not set up." : "This automation isn't available."}
    </p>;
  }
  if (!a) return <p className="mt-1.5 text-ui text-muted-foreground">Loading…</p>;
  const run = async (what: "on" | "off") => {
    setPending(true);
    setNote(null);
    try {
      if (what === "on") await act(a.id, "activate");
      else { await remove(a.id); setGone(true); }
    } catch (e) {
      setNote((e as { message?: string })?.message || "That didn't go through. Try again.");
    } finally {
      setPending(false);
    }
  };
  return <section aria-label={`Turn on ${a.name}`} className="editorial-card mt-1.5 max-w-xl px-3 py-2" data-automation-card={a.id}>
    <p className="font-semibold">{a.status === "draft" ? "Turn on: " : ""}{a.name}</p>
    <p className="text-ui text-muted-foreground">{lines(a, viewerTz).join(" · ")}</p>
    {a.nextRuns.length > 0 && <p className="text-ui text-muted-foreground">Next: {a.nextRuns.slice(0, 3).map((iso) => when(iso, viewerTz)).join(", ")}</p>}
    {a.status === "draft"
      ? <div className="mt-2 flex items-center gap-2">
        <Button type="button" size="sm" disabled={pending} onClick={() => void run("on")}>Turn on</Button>
        <Button type="button" size="sm" variant="ghost" disabled={pending} onClick={() => void run("off")}>Not now</Button>
      </div>
      : <p className="mt-1 text-ui text-ok">{a.status === "active" ? "On" : "Paused"}</p>}
    {note && <p role="alert" className="mt-1 text-ui text-err">{note}</p>}
  </section>;
}

function lines(a: AutomationView, viewerTz: string): string[] {
  const out: string[] = [];
  if (a.schedule) {
    const words = formatSchedule(a.schedule.cron, a.schedule.tz, viewerTz);
    out.push(a.schedule.tz === viewerTz ? words : `${words} (${a.schedule.tz.replace(/_/g, " ").split("/").pop()} time: ${formatSchedule(a.schedule.cron, a.schedule.tz, a.schedule.tz)})`);
  }
  out.push(specialistLabel(a.specialistId));
  const perMonth = a.estRunsPerMonth ? `, about ${money(a.budgetPerRunUsd * a.estRunsPerMonth)} a month` : "";
  out.push(`up to ${money(a.budgetPerRunUsd)} a run${perMonth}`);
  return out;
}

function when(iso: string, tz: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit", hourCycle: "h23" }).format(d);
}
