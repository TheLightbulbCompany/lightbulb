"use client";

import { useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { timeAgo } from "@/lib/timeAgo";
import { useApi } from "@/lib/api";

interface AgentKey {
  keyId: string;
  name: string;
  createdAt: string;
  expiresAt: number | null;
}

const KEY = "/settings/agent-keys";

/**
 * Keys for agents that cannot sign in with OAuth (Grok Bot, Muse, Gemini, Copilot). A key acts
 * with YOUR authority in this workspace and posts as "<name> via <you>". Shown once at creation.
 */
export function AgentKeysSection() {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<{ keys: AgentKey[] }>(KEY, () => api.get(KEY) as Promise<{ keys: AgentKey[] }>);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState<{ name: string; key: string } | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const run = async (action: () => Promise<unknown>, fallback: string) => {
    setBusy(true);
    setFailure(null);
    try {
      await action();
      await mutate();
    } catch (e) {
      setFailure(e instanceof Error && e.message ? e.message : fallback);
    } finally {
      setBusy(false);
    }
  };

  const create = () => run(async () => {
    const made = await api.post(KEY, { name: name.trim() }) as { key: string; name: string };
    setFresh({ name: made.name, key: made.key });
    setName("");
  }, "Couldn't create that key.");

  const keys = data?.keys ?? [];
  return (
    <section id="agent-keys" aria-labelledby="agent-keys-heading"
      className="overflow-hidden rounded-md border border-[var(--edge)] bg-cream">
      <header className="border-b border-[var(--edge)] px-5 py-4">
        <h3 id="agent-keys-heading" className="font-editorial-serif text-base font-semibold text-[var(--midnight)]">Agent keys</h3>
        <p className="mt-1 max-w-prose text-sm leading-relaxed text-[var(--midnight-muted)]">
          For agents that cannot sign in with OAuth. A key can do what you can do here, and its
          messages show as &ldquo;name via you&rdquo;. Keys expire after 90 days. Revoke one and its
          next call fails.
        </p>
      </header>
      <div className="space-y-3 px-5 py-4">
        {fresh && <div role="status" className="rounded-md border border-[var(--edge)] p-3 text-sm text-[var(--midnight)]">
          Key for {fresh.name}. Copy it now; it is not shown again.
          <code className="mt-2 block break-all font-mono text-xs">{fresh.key}</code>
          <Button size="sm" variant="outline" className="mt-2" onClick={() => setFresh(null)}>Done</Button>
        </div>}
        {error
          ? <p role="alert" className="text-sm text-[var(--err)]">Couldn&apos;t load agent keys.</p>
          : isLoading
            ? <p role="status" className="text-sm text-[var(--midnight-muted)]">Loading…</p>
            : keys.length === 0
              ? <p className="text-sm text-[var(--midnight-muted)]">No agent keys yet.</p>
              : <ul className="divide-y divide-[var(--edge)] text-sm">
                {keys.map((k) => <li key={k.keyId} className="flex items-center justify-between gap-3 py-2">
                  <span className="min-w-0">
                    <span className="text-[var(--midnight)]">{k.name}</span>
                    <span className="ml-2 text-[var(--midnight-muted)]">
                      added {timeAgo(k.createdAt)}{k.expiresAt ? ` · expires ${new Date(k.expiresAt * 1000).toLocaleDateString()}` : ""}
                    </span>
                  </span>
                  <Button size="sm" variant="ghost" disabled={busy} aria-label={`Revoke ${k.name}`}
                    onClick={() => void run(() => api.del(`${KEY}/${encodeURIComponent(k.keyId)}`), "Couldn't revoke that key.")}>
                    Revoke
                  </Button>
                </li>)}
              </ul>}
        <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); void create(); }}>
          <input value={name} onChange={(e) => setName(e.target.value)} disabled={busy} maxLength={60}
            aria-label="Agent name" placeholder="Agent name, for example Grok Bot"
            className="h-8 min-w-0 flex-1 rounded-md border border-[var(--edge)] bg-[var(--cream-deep)] px-3 text-sm text-[var(--midnight)] placeholder:text-[var(--midnight-faint)]" />
          <Button size="sm" type="submit" disabled={busy || !name.trim()}>Create key</Button>
        </form>
        {failure && <p role="alert" className="text-sm text-[var(--err)]">{failure}</p>}
      </div>
    </section>
  );
}
