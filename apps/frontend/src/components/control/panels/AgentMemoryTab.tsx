"use client";

import { useState } from "react";
import useSWR from "swr";

import { useApi } from "@/lib/api";

interface Memory { id: string; text: string; createdAt: string; confirmed: boolean }
interface MemoryList { private: Memory[]; team: Memory[]; truncated: boolean }

const BTN = "text-xs font-medium text-[var(--link)] hover:underline disabled:opacity-50";

// A new record is listed by GET /memory only ~60 s after it is written, so a share/confirm/delete
// is held in this module-level overlay (survives unmount) and merged into every fetched list.
// Any revalidation is then safe. The endpoint is per viewer, so nothing else keys it.
const HOLD_MS = 90_000;
const added = new Map<string, { row: Memory; until: number }>();
const removed = new Map<string, number>();

/** Test seam: clear the overlay between tests. */
export function resetMemoryOverlay() {
  added.clear();
  removed.clear();
}

function overlay(d: MemoryList): MemoryList {
  const now = Date.now();
  for (const [k, v] of added) if (v.until < now) added.delete(k);
  for (const [k, until] of removed) if (until < now) removed.delete(k);
  const keep = (m: Memory) => !removed.has(m.id) && !added.has(m.id);
  const rows = [...added.values()].map((v) => v.row);
  return {
    ...d,
    private: d.private.filter(keep),
    team: [...rows, ...d.team.filter(keep)],
  };
}

/**
 * What this workspace's agents remember (memory-scopes phases 2-4). Memory is per WORKSPACE,
 * not per agent (D1): "Only you" is the viewer's private scope (their 1:1 DMs with agents),
 * "Team" is shared by every agent and member. Share and Confirm are backend-only acts.
 */
export function AgentMemoryTab() {
  const api = useApi();
  const { data: raw, error, isLoading, mutate } = useSWR<MemoryList>(
    "/memory",
    () => api.get("/memory") as Promise<MemoryList>,
    { shouldRetryOnError: false },
  );
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const [, bump] = useState(0);
  const data = raw ? overlay(raw) : undefined;

  async function act(
    id: string,
    run: () => Promise<unknown>,
    apply: (res: { id?: string }) => void,
    revalidate = false,
  ) {
    setBusy((b) => new Set(b).add(id));
    setErr(null);
    try {
      apply((await run()) as { id?: string });
      bump((n) => n + 1);
      if (revalidate) void mutate();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy((b) => {
        const n = new Set(b);
        n.delete(id);
        return n;
      });
    }
  }

  // Put a confirmed team row in the overlay; the old record (if its id changed) stays hidden.
  function hold(old: Memory, newId: string | undefined) {
    const until = Date.now() + HOLD_MS;
    const id = newId ?? old.id;
    if (id !== old.id) removed.set(old.id, until);
    added.set(id, { row: { ...old, id, confirmed: true }, until });
  }

  function row(m: Memory, scope: "private" | "team") {
    return (
      <li key={m.id} className="border-b border-[var(--plane-edge)] py-2">
        <p className="whitespace-pre-wrap break-words text-sm text-[var(--fg)]">{m.text}</p>
        <div className="mt-1 flex items-center gap-3 text-xs text-[var(--fg-mute)]">
          <span>{new Date(m.createdAt).toLocaleDateString()}</span>
          {m.confirmed ? <span className="text-[var(--live)]">Confirmed</span> : null}
          {scope === "private" ? (
            <button type="button" className={BTN} disabled={busy.has(m.id)}
              onClick={() => act(m.id, () => api.post(`/memory/${m.id}/share`, {}), (r) => hold(m, r.id))}>Share with team</button>
          ) : null}
          {scope === "team" && !m.confirmed ? (
            <button type="button" className={BTN} disabled={busy.has(m.id)}
              onClick={() => act(m.id, () => api.post(`/memory/${m.id}/confirm`, {}), (r) => hold(m, r.id))}>Confirm</button>
          ) : null}
          <button type="button" className={BTN} disabled={busy.has(m.id)}
            onClick={() => {
              if (window.confirm("Delete this memory? Agents will no longer recall it.")) {
                void act(m.id, () => api.del(`/memory/${m.id}`), () => {
                  removed.set(m.id, Date.now() + HOLD_MS);
                  added.delete(m.id);
                }, true);
              }
            }}>Delete</button>
        </div>
      </li>
    );
  }

  function section(title: string, note: string, rows: Memory[], scope: "private" | "team") {
    return (
      <section className="mt-4">
        <h3 className="label-caps text-[var(--fg-mute)]">{title}</h3>
        <p className="mb-1 text-xs text-[var(--fg-mute)]">{note}</p>
        {rows.length ? <ul>{rows.map((m) => row(m, scope))}</ul>
          : <p className="py-2 text-sm text-[var(--fg-soft)]">Nothing remembered yet. New facts appear a few minutes after a conversation.</p>}
      </section>
    );
  }

  if (isLoading) return <p className="py-4 text-sm text-[var(--fg-mute)]">Loading memory…</p>;
  if (error || !data) return <p className="py-4 text-sm text-[var(--alert)]">Memory is unavailable right now.</p>;
  return (
    <div className="py-4">
      <p className="text-xs text-[var(--fg-mute)]">Shared by every agent in this workspace.</p>
      {err ? <p role="alert" className="mt-2 text-xs text-[var(--alert)]">{err}</p> : null}
      {section("Only you", "From your direct messages with agents. Only you and the agents in those messages see these.", data.private, "private")}
      {section("Team", "Every member and every agent can recall these.", data.team, "team")}
      {data.truncated ? <p className="mt-2 text-xs text-[var(--fg-mute)]">Showing the newest 200.</p> : null}
    </div>
  );
}
