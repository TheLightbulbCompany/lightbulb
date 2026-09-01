"use client";

// A tab that exists on the map before its rail exists.
//
// Owner ruling 2026-09-01: ship shells with named exits, so an owner sees the
// whole product on day one and we learn demand before building rails.
// result.dev does exactly this — A/B Testing, Compliance and Contracts are
// bare "Coming soon" cards with one sentence of value, and their nav is no
// less legible for it.
//
// The rule that keeps this honest (agreed with the session that owns the
// backend rails): a shell must state its exit. A prompt is an exit ONLY when
// the agent can genuinely act on it today — "Run a site health check on my
// website" is real (the agent has a browser); "Incorporate my company" is not
// (no formation partner is wired). A prompt the agent answers with "I can't do
// that yet" is worse than no prompt, so the ones below are chosen, not
// generated.
//
// Deliberately NOT a PanelNotice: PanelNotice keeps a panel's chrome and
// explains why its rows are empty. There is no chrome here — the whole page
// is the sentence — so it is its own, smaller thing.

import type { LucideIcon } from "lucide-react";
import { PanelMasthead } from "@/components/teams/shell";

export interface SoonPanelProps {
  overline: string;
  title: string;
  icon: LucideIcon;
  /** One sentence: what this will do for the owner. Not a feature list. */
  value: string;
  /** Only when the agent can act on it today. Renders in quotes, as the
   *  sentence the owner can hand to Friday. */
  prompt?: string;
}

export function SoonPanel({ overline, title, icon: Icon, value, prompt }: SoonPanelProps) {
  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-5 max-w-3xl mx-auto w-full">
      <PanelMasthead overline={overline} title={title} />
      <div className="flex flex-col gap-3 pt-4">
        <Icon aria-hidden="true" size={22} strokeWidth={1.5} className="text-[var(--midnight-muted)]" />
        <p className="label-caps text-[var(--midnight-muted)]">Coming soon</p>
        <p className="max-w-[60ch] text-ui text-[var(--midnight-soft)]">{value}</p>
        {prompt && (
          <p className="mt-1 max-w-[60ch] text-xs text-[var(--midnight-muted)]">
            Until then, ask Friday:{" "}
            <span className="font-editorial-serif italic text-[var(--midnight-soft)]">
              &ldquo;{prompt}&rdquo;
            </span>
          </p>
        )}
      </div>
    </div>
  );
}
