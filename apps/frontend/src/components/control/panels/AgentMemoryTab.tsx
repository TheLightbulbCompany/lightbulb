"use client";

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";

import { useApi } from "@/lib/api";

interface Memory {
  id: string; text: string; createdAt: string; confirmed: boolean;
  status?: "current" | "contested" | "replaced";
  history?: { value: string; speaker: string; occurredAt: string }[];
}
interface MemoryList { private: Memory[]; team: Memory[]; truncated: boolean }

const BTN = "text-xs font-medium text-[var(--link)] hover:underline disabled:opacity-50";

// A new record is listed by GET /memory only ~60 s after it is written, so a share/confirm/delete
// is held in this module-level overlay (survives unmount) and merged into every fetched list.
// Any revalidation is then safe. Every entry carries the workspace+user scope ("org:user") it was
// written under and is invisible to any other scope, so switching workspace never shows A's rows in B.
const HOLD_MS = 90_000;
const added = new Map<string, { row: Memory; until: number; scope: string }>();
const removed = new Map<string, { until: number; scope: string }>();

/** TEST ONLY: clear the overlay between tests. */
export function resetMemoryOverlay() {
  added.clear();
  removed.clear();
}

function overlay(d: MemoryList, scope: string): MemoryList {
  const now = Date.now();
  for (const [k, v] of added) if (v.until < now) added.delete(k);
  for (const [k, v] of removed) if (v.until < now) removed.delete(k);
  const mine = <T extends { scope: string }>(v: T | undefined) => v?.scope === scope;
  const keep = (m: Memory) => !mine(removed.get(m.id)) && !mine(added.get(m.id));
  const rows = [...added.values()].filter(mine).map((v) => v.row);
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
  const { orgId, userId } = useAuth();
  const scope = `${orgId ?? ""}:${userId ?? ""}`;
  const { data: raw, error, isLoading, mutate } = useSWR<MemoryList>(
    ["/memory", scope],
    () => api.get("/memory") as Promise<MemoryList>,
    { shouldRetryOnError: false },
  );
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const [contestedOnly, setContestedOnly] = useState(false);
  const [, bump] = useState(0);
  // One refetch at HOLD_MS after each successful action: the overlay expires then, and the server list is current by now.
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(() => () => timers.current.forEach(clearTimeout), []);
  const data = raw ? overlay(raw, scope) : undefined;

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
      timers.current.push(setTimeout(() => void mutate(), HOLD_MS));
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
    if (id !== old.id) removed.set(old.id, { until, scope });
    added.set(id, { row: { ...old, id, confirmed: true }, until, scope });
  }

  function row(m: Memory, kind: "private" | "team") {
    return (
      <li key={m.id} className="border-b border-[var(--plane-edge)] py-2">
        <p className="whitespace-pre-wrap break-words text-sm text-[var(--fg)]">{m.text}</p>
        <div className="mt-1 flex items-center gap-3 text-xs text-[var(--fg-mute)]">
          <span>{new Date(m.createdAt).toLocaleDateString()}</span>
          {m.confirmed ? <span className="text-[var(--live)]">Confirmed</span> : null}
          {m.status === "contested" ? <span className="text-[var(--alert)]">Contested</span> : null}
          {added.get(m.id)?.scope === scope ? <span>Pending</span> : null}
          {kind === "private" ? (
            <button type="button" className={BTN} disabled={busy.has(m.id)}
              onClick={() => act(m.id, () => api.post(`/memory/${m.id}/share`, {}), (r) => hold(m, r.id))}>Share with team</button>
          ) : null}
          {kind === "team" && !m.confirmed ? (
            <button type="button" className={BTN} disabled={busy.has(m.id)}
              onClick={() => act(m.id, () => api.post(`/memory/${m.id}/confirm`, {}), (r) => hold(m, r.id))}>Confirm</button>
          ) : null}
          <button type="button" className={BTN} disabled={busy.has(m.id)}
            onClick={() => {
              if (window.confirm("Delete this memory? Agents will no longer recall it.")) {
                void act(m.id, () => api.del(`/memory/${m.id}`), () => {
                  removed.set(m.id, { until: Date.now() + HOLD_MS, scope });
                  added.delete(m.id);
                }, true);
              }
            }}>Delete</button>
        </div>
        {m.history?.map((h, i) => (
          <p key={i} className="mt-1 text-xs text-[var(--fg-mute)]">
            Was {h.value} · {h.speaker} · {new Date(h.occurredAt).toLocaleDateString()}
          </p>
        ))}
      </li>
    );
  }

  function section(title: string, note: string, rows: Memory[], kind: "private" | "team") {
    if (contestedOnly) rows = rows.filter((m) => m.status === "contested");
    return (
      <section className="mt-4">
        <h3 className="label-caps text-[var(--fg-mute)]">{title}</h3>
        <p className="mb-1 text-xs text-[var(--fg-mute)]">{note}</p>
        {rows.length ? <ul>{rows.map((m) => row(m, kind))}</ul>
          : <p className="py-2 text-sm text-[var(--fg-soft)]">Nothing remembered yet. New facts appear a few minutes after a conversation.</p>}
      </section>
    );
  }

  if (isLoading) return <p className="py-4 text-sm text-[var(--fg-mute)]">Loading memory…</p>;
  if (error || !data) return <p className="py-4 text-sm text-[var(--alert)]">Memory is unavailable right now.</p>;
  return (
    <div className="py-4">
      <div className="flex items-center justify-between text-xs text-[var(--fg-mute)]">
        <p>Shared by every agent in this workspace.</p>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={contestedOnly} onChange={(e) => setContestedOnly(e.target.checked)} />
          Contested only
        </label>
      </div>
      {err ? <p role="alert" className="mt-2 text-xs text-[var(--alert)]">{err}</p> : null}
      {section("Only you", "From your direct messages with agents. Only you and the agents in those messages see these.", data.private, "private")}
      {section("Team", "Every member and every agent can recall these.", data.team, "team")}
      {data.truncated ? <p className="mt-2 text-xs text-[var(--fg-mute)]">Showing 200 of them.</p> : null}
    </div>
  );
}
