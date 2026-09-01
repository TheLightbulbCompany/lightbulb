"use client";

// Growth → Publish (`?panel=teams-publish`).
//
// result.dev renders a full month calendar with a `+` on every day, and above
// it a banner naming the six networks to connect. The calendar is empty, the
// exit is real, and the owner learns the page's shape before there is data —
// which is the whole argument for shells with named exits.
//
// Ours is the same shape with a real exit: `instagram` and `x` are wired in
// the connections rail (`actor: cli` — the agent publishes), so "Connect
// accounts" goes to Integrations, where those rows already live. What does
// NOT exist yet is a store of published/scheduled posts, so the calendar has
// nothing to render into its cells. It is drawn anyway, empty, for the same
// reason result.dev draws theirs: an owner who sees a calendar knows what
// this tab is for.
//
// No charting or calendar dependency — it is a 7-column CSS grid of the
// current month. Pulling in a library to draw thirty numbered boxes would be
// the kind of thing nobody removes later.

import { Send } from "lucide-react";
import { PanelMasthead, PanelNotice } from "@/components/teams/shell";
import { handleShellLinkClick } from "@/lib/shellNav";

const INTEGRATIONS = "/chat?panel=integrations";

function monthGrid(now: Date): { label: string; cells: (number | null)[] } {
  const y = now.getUTCFullYear();
  const m = now.getUTCMonth();
  const first = new Date(Date.UTC(y, m, 1));
  const daysInMonth = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
  // Monday-first, matching result.dev and most business calendars.
  const lead = (first.getUTCDay() + 6) % 7;
  const cells: (number | null)[] = Array(lead).fill(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);
  while (cells.length % 7) cells.push(null);
  const label = first.toLocaleDateString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });
  return { label, cells };
}

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function PublishPanel() {
  const now = new Date();
  const { label, cells } = monthGrid(now);
  const today = now.getUTCDate();

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-5xl mx-auto w-full">
      <PanelMasthead
        overline="Growth"
        title="Publish"
        meta="Everything you've shipped and everything that's coming, on one calendar."
      />

      <PanelNotice
        state="unbound"
        icon={Send}
        headline="No social accounts connected yet."
        supporting="Connect Instagram or X in Integrations and this calendar starts filling in as your agents post."
        prompt="Connect my Instagram so you can post for me."
        href={INTEGRATIONS}
        action={{
          label: "Connect accounts",
          onClick: (e) => handleShellLinkClick(e, INTEGRATIONS),
        }}
      />

      <section aria-label={`${label} calendar`} className="flex flex-col gap-2">
        <p className="font-editorial-serif text-lg text-[var(--midnight)]">{label}</p>
        <div className="grid grid-cols-7 overflow-hidden rounded-md border border-[var(--edge)]">
          {DOW.map((d) => (
            <div
              key={d}
              className="border-b border-[var(--edge)] bg-card px-2 py-1.5 text-xs text-[var(--midnight-muted)]"
            >
              {d}
            </div>
          ))}
          {cells.map((day, i) => (
            <div
              key={i}
              className={`min-h-[72px] border-b border-r border-[var(--edge)] px-2 py-1.5 text-xs ${
                day === null ? "bg-[var(--cream-deep)]" : ""
              } ${day === today ? "text-[var(--midnight)]" : "text-[var(--midnight-muted)]"}`}
            >
              {day !== null && (
                <span className={day === today ? "rounded-sm bg-[var(--cream-deep)] px-1 font-medium" : ""}>
                  {day}
                </span>
              )}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
