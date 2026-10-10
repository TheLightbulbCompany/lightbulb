"use client";

import { useState } from "react";
import useSWR from "swr";

import { useApi } from "@/lib/api";

interface Memory { id: string; text: string; createdAt: string; confirmed: boolean }
interface MemoryList { private: Memory[]; team: Memory[]; truncated: boolean }

const BTN = "text-xs font-medium text-[var(--link)] hover:underline disabled:opacity-50";

/**
 * What this workspace's agents remember (memory-scopes phases 2-4). Memory is per WORKSPACE,
 * not per agent (D1): "Only you" is the viewer's private scope (their 1:1 DMs with agents),
 * "Team" is shared by every agent and member. Share and Confirm are backend-only acts.
 */
export function AgentMemoryTab() {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<MemoryList>(
    "/memory",
    () => api.get("/memory") as Promise<MemoryList>,
    { shouldRetryOnError: false },
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // After a call, show the new state at once and revalidate later: a new record is listed only
  // ~60 s after it is written, so an immediate refetch would drop the row we just added.
  // ponytail: a refetch inside that minute (focus) can still drop it; upgrade = keep a pending overlay.
  async function act(
    id: string,
    run: () => Promise<unknown>,
    next: (d: MemoryList, res: { id?: string }) => MemoryList,
    delayed = false,
  ) {
    setBusy(id);
    setErr(null);
    try {
      const res = (await run()) as { id?: string };
      if (data) await mutate(next(data, res), { revalidate: !delayed });
      if (delayed) window.setTimeout(() => void mutate(), 65_000);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(null);
    }
  }

  function row(m: Memory, scope: "private" | "team") {
    return (
      <li key={m.id} className="border-b border-[var(--plane-edge)] py-2">
        <p className="whitespace-pre-wrap break-words text-sm text-[var(--fg)]">{m.text}</p>
        <div className="mt-1 flex items-center gap-3 text-xs text-[var(--fg-mute)]">
          <span>{new Date(m.createdAt).toLocaleDateString()}</span>
          {m.confirmed ? <span className="text-[var(--live)]">Confirmed</span> : null}
          {scope === "private" ? (
            <button type="button" className={BTN} disabled={busy === m.id}
              onClick={() => act(m.id, () => api.post(`/memory/${m.id}/share`, {}), (d, r) => ({
                ...d,
                private: d.private.filter((x) => x.id !== m.id),
                team: [{ ...m, id: r.id ?? m.id, confirmed: true }, ...d.team],
              }), true)}>Share with team</button>
          ) : null}
          {scope === "team" && !m.confirmed ? (
            <button type="button" className={BTN} disabled={busy === m.id}
              onClick={() => act(m.id, () => api.post(`/memory/${m.id}/confirm`, {}), (d, r) => ({
                ...d,
                team: d.team.map((x) => (x.id === m.id ? { ...x, id: r.id ?? m.id, confirmed: true } : x)),
              }), true)}>Confirm</button>
          ) : null}
          <button type="button" className={BTN} disabled={busy === m.id}
            onClick={() => {
              if (window.confirm("Delete this memory? Agents will no longer recall it.")) {
                void act(m.id, () => api.del(`/memory/${m.id}`), (d) => ({
                  ...d,
                  private: d.private.filter((x) => x.id !== m.id),
                  team: d.team.filter((x) => x.id !== m.id),
                }));
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
