"use client";

// apps/frontend/src/components/teams/panels/ActivityPanel.tsx
//
// The full "Activity" page. Ported from upstream Paperclip's Activity page
// (paperclip/ui/src/pages/Activity.tsx) (MIT, (c) 2025 Paperclip AI).
//
// Shares ONE correct data path + renderer with the Dashboard's "Recent
// activity" feed: `useActivityFeed` (normalizes the BFF's bare-array
// `/teams/activity` response and exposes the correct ActivityEvent shape —
// `action`/`actorType`/`entityType`/…) and `ActivityRow` (avatars, verb
// formatting, entity links). The type-filter facets on `entityType`, matching
// upstream's Activity page (`data.filter((e) => e.entityType === filter)`).
//
// Deferred (not yet ported): upstream's agentMap / userProfileMap /
// entityNameMap / entityTitleMap enrichment (resolves agent + entity display
// names from sibling reads) and the ActivityCharts. ActivityRow degrades to the
// raw entity id without those maps. Charts live on the Dashboard already.

import { useMemo, useState } from "react";
import { PanelMasthead } from "@/components/teams/shell";
import { ActivityRow } from "@/components/teams/dashboard/ActivityRow";
import { useActivityFeed } from "@/components/teams/dashboard/hooks/useActivityFeed";

// Matches upstream's ACTIVITY_PAGE_LIMIT (paperclip/ui/src/pages/Activity.tsx).
// `useActivityFeed` reads the full BFF response and slices to this cap, so the
// Activity page surfaces far more history than the Dashboard's 10-row preview.
const ACTIVITY_PAGE_LIMIT = 200;

export function ActivityPanel() {
  const { events, isLoading, isError } = useActivityFeed(ACTIVITY_PAGE_LIMIT);
  const [filter, setFilter] = useState("all");

  // Facet on entityType (upstream's Activity-page filter). Drop null/empty
  // entity types so the dropdown only offers real facets.
  const entityTypes = useMemo(
    () =>
      Array.from(
        new Set(events.map((e) => e.entityType).filter((t): t is string => !!t)),
      ).sort(),
    [events],
  );

  const filtered = useMemo(
    () => (filter === "all" ? events : events.filter((e) => e.entityType === filter)),
    [events, filter],
  );

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-4xl mx-auto w-full">
      <PanelMasthead
        overline="Operations"
        title="Activity"
        actions={
          entityTypes.length > 0 ? (
            <select
              aria-label="Filter by type"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              className="h-8 rounded-sm border border-[var(--edge)] bg-[var(--cream)] px-2 text-xs text-[var(--midnight)]"
            >
              <option value="all">All types</option>
              {entityTypes.map((type) => (
                <option key={type} value={type}>
                  {type.charAt(0).toUpperCase() + type.slice(1)}
                </option>
              ))}
            </select>
          ) : undefined
        }
      />

      <section aria-label="Activity feed" className="min-w-0">
        <div
          className="editorial-card overflow-hidden"
          style={{ borderColor: "var(--edge)" }}
        >
          {isLoading && events.length === 0 ? (
            <div
              className="px-4 py-3 font-editorial-serif text-base italic"
              style={{ color: "var(--midnight-muted)" }}
            >
              Loading activity…
            </div>
          ) : isError && events.length === 0 ? (
            <div
              role="alert"
              className="px-4 py-3 font-editorial-serif text-base italic"
              style={{ color: "var(--warn)" }}
            >
              Failed to load activity.
            </div>
          ) : filtered.length === 0 ? (
            <div
              className="px-4 py-3 font-editorial-serif text-base italic"
              style={{ color: "var(--midnight-muted)" }}
            >
              {events.length === 0 ? "No activity yet." : "No activity of this type."}
            </div>
          ) : (
            <div>
              {filtered.map((evt, i) => (
                <div
                  key={evt.id}
                  style={
                    i === 0 ? undefined : { borderTop: "1px solid var(--edge)" }
                  }
                >
                  <ActivityRow event={evt} />
                </div>
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
