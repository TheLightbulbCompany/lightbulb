"use client";

// The card under Lightbulb's "Set up below. Confirm on the card." (lane A's propose route): the schedule
// in the viewer's zone, the next runs, who runs it and what it may spend. Only Turn on activates it;
// typed text never does (contracts-v2 §8). Not now deletes the draft.

import { useState, type ReactElement } from "react";
import { nextRunWords, useAutomation, type AutomationView } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { budgetLine, scheduleWords, useAutomationActions, useAutomationSource, useCanDecide, useCanEdit, useSpecialistLabel, viewerTimeZone, waitingWords } from "./automations";

export function AutomationConfirmCard({ automationId, viewerTz = viewerTimeZone() }: { automationId: string; viewerTz?: string }): ReactElement {
  const { data: a, error } = useAutomation(useAutomationSource(), automationId);
  const specialistName = useSpecialistLabel();
  const { act, remove } = useAutomationActions();
  const canDecide = useCanDecide();
  const canEdit = useCanEdit();
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  if (gone || (!a && error)) {
    return <p className="mt-1.5 max-w-xl rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">
      {gone ? "Not set up." : "This automation isn't available."}
    </p>;
  }
  // The card's own height while it loads, so the conversation doesn't jump when it fills in.
  if (!a) return <div aria-busy="true" aria-label="Loading automation" className="mt-1.5 h-24 max-w-xl rounded-lg border border-border bg-muted/40 motion-safe:animate-pulse" />;
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
    <p className="text-ui text-muted-foreground">{lines(a, viewerTz, specialistName).join(" · ")}</p>
    {a.nextRuns.length > 0 && <p className="text-ui text-muted-foreground">Next: {a.nextRuns.slice(0, 3).map((iso) => nextRunWords(iso, viewerTz)).join(", ")}</p>}
    {a.status !== "draft"
      ? <p className="mt-1 text-ui text-ok">{a.status === "active" ? "On" : "Paused"}</p>
      // The server's rules (routers/automations.py): Turn on is an activate (room → admin only); Not now is an edit (admin or owner).
      : canDecide(a) || canEdit(a)
        ? <div className="mt-2 flex items-center gap-2">
          {canDecide(a) && <Button type="button" size="sm" disabled={pending} onClick={() => void run("on")}>Turn on</Button>}
          {!canDecide(a) && <span className="text-ui text-muted-foreground">{waitingWords(a)}</span>}
          {canEdit(a) && <Button type="button" size="sm" variant="ghost" disabled={pending} onClick={() => void run("off")}>Not now</Button>}
        </div>
        : <p className="mt-1 text-ui text-muted-foreground">{waitingWords(a)}</p>}
    {note && <p role="alert" className="mt-1 text-ui text-err">{note}</p>}
  </section>;
}

function lines(a: AutomationView, viewerTz: string, specialistName: (id: string | null | undefined) => string): string[] {
  const out: string[] = [];
  const schedule = scheduleWords(a, viewerTz);
  if (schedule) out.push(schedule);
  out.push(specialistName(a.specialistId));
  const budget = budgetLine(a);
  if (budget) out.push(budget);
  return out;
}

