"use client";

import { useState } from "react";

import { useTeamsApi } from "@/hooks/useTeamsApi";

/**
 * Post-deploy routines screen.
 *
 * A company deploy imports its routines into the buyer's Paperclip company in
 * the **paused** state (they must never start firing schedules the moment a
 * company lands). This card surfaces that — it lists the imported routine ids
 * and lets the buyer enable them one-by-one or all at once.
 *
 * Enable is a real Teams-BFF action: `PATCH /teams/routines/{id}` with
 * `{status: "active"}` (the upstream routine row has no `enabled` field — the
 * enable/disable toggle is the status). We route it through `useTeamsApi` so
 * the call carries the `.isol8.co` Paperclip cookie (+ its retry-once-on-401
 * reseed) alongside the Clerk bearer, exactly like the Teams routines panel.
 *
 * Known gap (follow-up): the job row carries only routine *ids*, not titles, so
 * rows render the id. Fetching titles would mean an extra Teams read per
 * routine; deferred. The "Open Teams" link is the fallback for anyone who
 * would rather manage them in the Routines panel.
 */
export function RoutinesEnableCard({ routineIds }: { routineIds: string[] }) {
  const { patch } = useTeamsApi();
  // Per-routine UI state. Absent → paused/idle; "enabling" → in-flight;
  // "enabled" → done; a string starting with "error:" → failed with message.
  const [state, setState] = useState<Record<string, string>>({});

  async function enableOne(id: string) {
    if (state[id] === "enabling" || state[id] === "enabled") return;
    setState((s) => ({ ...s, [id]: "enabling" }));
    try {
      await patch(`/routines/${encodeURIComponent(id)}`, { status: "active" });
      setState((s) => ({ ...s, [id]: "enabled" }));
    } catch (e) {
      setState((s) => ({
        ...s,
        [id]: `error:${e instanceof Error ? e.message : "enable failed"}`,
      }));
    }
  }

  async function enableAll() {
    // Fire sequentially — the BFF is per-tenant and a burst of PATCHes buys
    // nothing but contention; the list is short (one company's routines).
    for (const id of routineIds) {
      if (state[id] === "enabled") continue;
      await enableOne(id);
    }
  }

  const allEnabled =
    routineIds.length > 0 && routineIds.every((id) => state[id] === "enabled");

  return (
    <section className="mt-6 editorial-card p-6" data-testid="routines-enable-card">
      <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--mute)]">
        Company deployed
      </div>
      <h3 className="mt-2 font-editorial-serif text-2xl text-[var(--ink)]">
        {routineIds.length > 0 ? "Enable routines" : "You're all set"}
      </h3>

      {routineIds.length === 0 ? (
        <p className="mt-3 text-sm leading-6 text-[var(--ink-soft)]">
          Your new team is live in your container. This company ships no
          scheduled routines — open Teams to start working with the agents.
        </p>
      ) : (
        <>
          <p className="mt-3 text-sm leading-6 text-[var(--ink-soft)]">
            This company&apos;s routines were imported <strong>paused</strong> so
            nothing fires before you&apos;re ready. Enable the ones you want —
            you can always manage them later in Teams.
          </p>

          <div className="mt-5 flex items-center justify-between">
            <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-[var(--mute)]">
              {routineIds.length} {routineIds.length === 1 ? "routine" : "routines"}
            </span>
            <button
              type="button"
              onClick={enableAll}
              disabled={allEnabled}
              className="editorial-btn editorial-btn--primary h-9 px-4 text-xs disabled:opacity-50"
            >
              {allEnabled ? "All enabled" : "Enable all"}
            </button>
          </div>

          <ul className="mt-4 flex flex-col gap-2">
            {routineIds.map((id) => {
              const s = state[id];
              const errored = typeof s === "string" && s.startsWith("error:");
              return (
                <li
                  key={id}
                  className="flex items-center justify-between gap-3 rounded-lg border border-[var(--edge)] px-3 py-2"
                >
                  <span className="min-w-0 flex-1 truncate font-mono text-xs text-[var(--ink-soft)]">
                    {id}
                  </span>
                  {s === "enabled" ? (
                    <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-[var(--forest)]">
                      Enabled
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => enableOne(id)}
                      disabled={s === "enabling"}
                      className="editorial-btn h-8 px-3 text-xs disabled:opacity-50"
                    >
                      {s === "enabling" ? "Enabling…" : "Enable"}
                    </button>
                  )}
                  {errored && (
                    <span className="text-[11px] text-[var(--danger)]" role="alert">
                      {s.slice("error:".length)}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}

      <a
        href="/chat"
        className="mt-5 inline-block font-mono text-[11px] uppercase tracking-[0.14em] text-[var(--forest)] underline"
      >
        Open Teams
      </a>
    </section>
  );
}
