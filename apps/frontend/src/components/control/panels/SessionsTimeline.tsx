"use client";

import { useState } from "react";
import useSWR from "swr";

import { useApi } from "@/lib/api";

interface SessionRow {
  id: string;
  harness: string;
  computer: string;
  repo: string;
  branch: string;
  started: string;
  ended: string | null;
  prompt_tokens: number;
  completion_tokens: number;
}

const BTN = "text-xs font-medium text-[var(--link)] hover:underline disabled:opacity-50";

function duration(a: string, b: string | null): string {
  const ms = b ? Date.parse(b) - Date.parse(a) : NaN;
  if (!(ms >= 0)) return "";
  const m = Math.round(ms / 60_000);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

/** Sessions uploaded from this workspace's computers, newest first, grouped by computer. */
export function SessionsTimeline() {
  const api = useApi();
  const { data, error, isLoading } = useSWR<{ sessions: SessionRow[] }>(
    "/sessions",
    () => api.get("/sessions") as Promise<{ sessions: SessionRow[] }>,
    { shouldRetryOnError: false },
  );
  const [links, setLinks] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);

  async function share(id: string) {
    setErr(null);
    try {
      const r = (await api.post(`/sessions/${id}/share`, {})) as { url: string };
      setLinks((l) => ({ ...l, [id]: r.url }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Something went wrong");
    }
  }

  if (isLoading) return <p className="py-4 text-sm text-[var(--fg-mute)]">Loading sessions…</p>;
  if (error || !data) return <p className="py-4 text-sm text-[var(--alert)]">Sessions are unavailable right now.</p>;

  const groups = new Map<string, SessionRow[]>();
  for (const s of data.sessions) {
    const k = s.computer || "Unnamed computer";
    groups.set(k, [...(groups.get(k) ?? []), s]); // the API sends newest first
  }
  if (!groups.size) return <p className="py-4 text-sm text-[var(--fg-soft)]">No sessions uploaded yet.</p>;

  return (
    <div className="py-4">
      {err ? <p className="text-xs text-[var(--alert)]">{err}</p> : null}
      {[...groups].map(([computer, rows]) => (
        <section key={computer} className="mt-4 first:mt-0">
          <h3 className="label-caps text-[var(--fg-mute)]">{computer}</h3>
          <ul>
            {rows.map((s) => (
              <li key={s.id} className="border-b border-[var(--plane-edge)] py-2 text-sm text-[var(--fg)]">
                <div>
                  {s.harness} · {s.repo || "no repo"}
                  {s.branch ? ` · ${s.branch}` : ""}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-[var(--fg-mute)]">
                  <span>{new Date(s.started).toLocaleString()}</span>
                  {duration(s.started, s.ended) ? <span>{duration(s.started, s.ended)}</span> : null}
                  <span>{s.prompt_tokens.toLocaleString()} prompt tokens</span>
                  <span>{s.completion_tokens.toLocaleString()} completion tokens</span>
                  {links[s.id] ? (
                    <input readOnly aria-label="Share link" value={links[s.id]} onFocus={(e) => e.target.select()}
                      className="min-w-0 flex-1 rounded border border-[var(--plane-edge)] px-1" />
                  ) : (
                    <button type="button" className={BTN} onClick={() => void share(s.id)}>Share</button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
