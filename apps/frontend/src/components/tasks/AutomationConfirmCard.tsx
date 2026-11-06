"use client";

// The card under Lightbulb's "Set up below. Turn it on with the button on the card." (lane A's propose route):
// the schedule in the viewer's zone (never raw cron), the next run, who runs it and what it may spend. Only Turn on
// activates it, and it shows whenever it is off and the viewer may turn it on; typed text never does (contracts-v2 §8).
// Not now deletes the draft.

import { useState, type ReactElement } from "react";
import { automationStatusLine, nextRunWords, useAutomation, type AutomationView } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { budgetLine, scheduleWords, useAutomationActions, useAutomationSource, useCanDecide, useCanEdit, useDeliveryWords, useSpecialistLabel, viewerTimeZone, waitingWords } from "./automations";

export function AutomationConfirmCard({ automationId, viewerTz = viewerTimeZone() }: { automationId: string; viewerTz?: string }): ReactElement {
  const { data: a, error, mutate } = useAutomation(useAutomationSource(), automationId);
  const specialistName = useSpecialistLabel();
  const delivery = useDeliveryWords();
  const { act, remove } = useAutomationActions();
  const canDecide = useCanDecide();
  const canEdit = useCanEdit();
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  // Not now deletes the draft, so every later read of this card is a 404 (dev walk 10, W10-6): it says what
  // happened, in a plain line, never a dead box. Any other failed read offers Retry.
  if (gone || (!a && (error as { status?: number } | undefined)?.status === 404)) {
    return <p className="mt-1 text-ui text-muted-foreground">Not set up.</p>;
  }
  if (!a && error) {
    return <p role="alert" className="mt-1 text-ui text-muted-foreground">
      This automation couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => void mutate()}>Retry</button>
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
  // An unreadable date reads as nothing, never a bare "Next:".
  const next = a.nextRuns[0] ? nextRunWords(a.nextRuns[0], viewerTz) : "";
  return <section aria-label={`Turn on ${a.name}`} className="editorial-card mt-1.5 max-w-xl px-3 py-2" data-automation-card={a.id}>
    <p className="font-semibold">{a.status === "draft" ? "Turn on: " : ""}{a.name}</p>
    <p className="text-ui text-muted-foreground">{lines(a, viewerTz, specialistName, delivery).join(" · ")}</p>
    {next && <p className="text-ui text-muted-foreground">Next: {next}</p>}
    {/* Only On is green; Paused is a state, not a success. */}
    {a.status !== "draft" && <p className={`mt-1 text-ui ${a.status === "active" ? "text-ok" : "text-muted-foreground"}`}>{automationStatusLine(a) ?? "On"}</p>}
    {/* The server's rules (routers/automations.py): Turn on is an activate, also from paused (room → admin only);
        Not now is an edit (admin or owner) and only drops a draft. */}
    {a.status !== "active" && (canDecide(a) || (a.status === "draft" && canEdit(a))
      ? <div className="mt-2 flex items-center gap-2">
        {canDecide(a) ? <Button type="button" size="sm" disabled={pending} onClick={() => void run("on")}>Turn on</Button>
          : <span className="text-ui text-muted-foreground">{waitingWords(a)}</span>}
        {a.status === "draft" && canEdit(a) && <Button type="button" size="sm" variant="ghost" disabled={pending} onClick={() => void run("off")}>Not now</Button>}
      </div>
      : <p className="mt-1 text-ui text-muted-foreground">{waitingWords(a)}</p>)}
    {note && <p role="alert" className="mt-1 text-ui text-err">{note}</p>}
  </section>;
}

function lines(a: AutomationView, viewerTz: string, specialistName: (id: string | null | undefined) => string, delivery: (deliverTo: string) => string | null): string[] {
  const out: string[] = [];
  const schedule = scheduleWords(a, viewerTz);
  if (schedule) out.push(schedule);
  const where = delivery(a.deliverTo);
  if (where) out.push(where);
  out.push(specialistName(a.specialistId));
  const budget = budgetLine(a);
  if (budget) out.push(budget);
  return out;
}

