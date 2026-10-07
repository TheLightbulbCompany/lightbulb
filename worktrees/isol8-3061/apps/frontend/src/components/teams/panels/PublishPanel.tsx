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
import { useConnections } from "@/hooks/useConnections";

// The social providers this tab can post through. Only the ones the
// /connections endpoint actually lists for this owner are ever named:
// `instagram` is gated to `environments: [local, dev]` plus named dogfood
// owners, so in prod most owners cannot see it, and telling them to connect
// it would be an exit that does not exist for them.
const SOCIAL = { instagram: "Instagram", x: "X" } as const;

const INTEGRATIONS = "/chat?panel=integrations";

// LOCAL time, not UTC: this is a calendar the owner reads, not a billing
// period. At 00:30Z on the 1st, an owner in California is still on the 31st
// and would otherwise see next month with day 1 highlighted.
function monthGrid(now: Date): { label: string; cells: (number | null)[] } {
  const y = now.getFullYear();
  const m = now.getMonth();
  const first = new Date(y, m, 1);
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  // Monday-first, matching result.dev and most business calendars.
  const lead = (first.getDay() + 6) % 7;
  const cells: (number | null)[] = Array(lead).fill(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);
  while (cells.length % 7) cells.push(null);
  const label = first.toLocaleDateString("en-US", { month: "long", year: "numeric" });
  return { label, cells };
}

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function PublishPanel() {
  const now = new Date();
  const { label, cells } = monthGrid(now);
  const today = now.getDate();
  const { connections } = useConnections();

  // What this owner can actually see and has actually connected. The
  // endpoint already hides env-gated providers, so "available" here means
  // "listed for you", and the copy names exactly those.
  const social = connections.filter((c) => c.provider in SOCIAL);
  const connected = social.filter((c) => c.status === "connected" && c.runtime_supported !== false).map((c) => SOCIAL[c.provider as keyof typeof SOCIAL]);
  const available = social.map((c) => SOCIAL[c.provider as keyof typeof SOCIAL]);
  const list = (xs: string[]) => new Intl.ListFormat("en", { type: "disjunction" }).format(xs);

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-5xl mx-auto w-full">
      <PanelMasthead
        overline="Growth"
        title="Publish"
        meta="Everything you've shipped and everything that's coming, on one calendar."
      />

      {connected.length ? (
        // Connected, but there is no post store yet, so the calendar has
        // nothing to render. Say that — not "nothing connected".
        <PanelNotice
          state="zero"
          icon={Send}
          headline={`${new Intl.ListFormat("en").format(connected)} connected.`}
          supporting="Posts your agents publish will appear on this calendar. Nothing has been scheduled or posted yet."
          prompt="Draft one post about what we shipped this week and post it."
        />
      ) : available.length ? (
        <PanelNotice
          state="unbound"
          icon={Send}
          headline="No social accounts connected yet."
          supporting={`Connect ${list(available)} in Integrations and this calendar starts filling in as your agents post.`}
          prompt={`Connect my ${available[0]} so you can post for me.`}
          href={INTEGRATIONS}
          action={{
            label: "Connect accounts",
            onClick: (e) => handleShellLinkClick(e, INTEGRATIONS),
          }}
        />
      ) : (
        // No social provider is listed for this owner at all — an honest
        // "not yet" rather than a connect button to a row that is not there.
        <PanelNotice
          state="soon"
          icon={Send}
          headline="Social publishing isn't available on your workspace yet."
          supporting="When it is, connected networks show up in Integrations and this calendar fills in as your agents post."
        />
      )}

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
