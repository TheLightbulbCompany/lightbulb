"use client";

import { useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useApi } from "@/lib/api";

interface Person {
  userId: string;
  name: string;
}

interface WebhookAgent {
  agentId: string;
  name: string;
  kind: string;
  host: string;
  createdAt: string;
  allowedSenders: Person[];
  waiting: (Person & { count: number })[];
}

const KEY = "/settings/webhook-agents";
const input =
  "h-8 min-w-0 flex-1 rounded-md border border-[var(--edge)] bg-[var(--cream-deep)] px-3 text-sm text-[var(--midnight)] placeholder:text-[var(--midnight-faint)]";

/**
 * Agents that live in another cloud and wake on a webhook (Grok Bot first). Lightbulb POSTs each
 * message sent to one; it answers through the hosted MCP. The URL and key are never shown again.
 */
export function WebhookAgentsSection() {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<{ agents: WebhookAgent[] }>(
    KEY, () => api.get(KEY) as Promise<{ agents: WebhookAgent[] }>);
  const [form, setForm] = useState({ name: "Grok Bot", url: "", key: "" });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const run = async (action: () => Promise<unknown>, fallback: string) => {
    setBusy(true);
    setFailure(null);
    setNote(null);
    try {
      await action();
      await mutate();
    } catch (e) {
      setFailure(e instanceof Error && e.message ? e.message : fallback);
    } finally {
      setBusy(false);
    }
  };

  const register = () => run(async () => {
    await api.post(KEY, { name: form.name.trim(), kind: form.name.trim(), url: form.url.trim(), key: form.key });
    setForm({ name: "Grok Bot", url: "", key: "" });
  }, "Couldn't add that agent.");

  const ping = (a: WebhookAgent) => run(async () => {
    const r = await api.post(`${KEY}/${encodeURIComponent(a.agentId)}/test`, {}) as { delivered: boolean; reason?: string };
    setNote(r.delivered ? `${a.name} got the test ping.` : `${a.name} did not get it (${r.reason ?? "failed"}).`);
  }, "Couldn't send the test ping.");

  const path = (a: WebhookAgent, userId: string) =>
    `${KEY}/${encodeURIComponent(a.agentId)}/senders/${encodeURIComponent(userId)}`;

  const agents = data?.agents ?? [];
  return (
    <section id="webhook-agents" aria-labelledby="webhook-agents-heading"
      className="overflow-hidden rounded-md border border-[var(--edge)] bg-cream">
      <header className="border-b border-[var(--edge)] px-5 py-4">
        <h3 id="webhook-agents-heading" className="font-editorial-serif text-base font-semibold text-[var(--midnight)]">Webhook agents</h3>
        <p className="mt-1 max-w-prose text-sm leading-relaxed text-[var(--midnight-muted)]">
          For an agent that wakes on a webhook, such as a Grok Bot routine. Paste its webhook URL and
          key; messages sent to it arrive there, and it replies with an agent key. A teammate&apos;s
          message waits here until you allow them.
        </p>
      </header>
      <div className="space-y-3 px-5 py-4">
        {error
          ? <p role="alert" className="text-sm text-[var(--err)]">Couldn&apos;t load webhook agents.</p>
          : isLoading
            ? <p role="status" className="text-sm text-[var(--midnight-muted)]">Loading…</p>
            : agents.length === 0
              ? <p className="text-sm text-[var(--midnight-muted)]">No webhook agents yet.</p>
              : <ul className="divide-y divide-[var(--edge)] text-sm">
                {agents.map((a) => <li key={a.agentId} className="space-y-2 py-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="min-w-0">
                      <span className="text-[var(--midnight)]">{a.name}</span>
                      <span className="ml-2 text-[var(--midnight-muted)]">{a.host} · added {timeAgo(a.createdAt)}</span>
                    </span>
                    <span className="flex gap-1">
                      <Button size="sm" variant="outline" disabled={busy} onClick={() => void ping(a)}>Send a test ping</Button>
                      <Button size="sm" variant="ghost" disabled={busy} aria-label={`Remove ${a.name}`}
                        onClick={() => void run(() => api.del(`${KEY}/${encodeURIComponent(a.agentId)}`), "Couldn't remove that agent.")}>
                        Remove
                      </Button>
                    </span>
                  </div>
                  {a.waiting.map((w) => <div key={w.userId} className="flex flex-wrap items-center gap-2 text-[var(--midnight)]">
                    {w.name} sent {w.count} message{w.count === 1 ? "" : "s"}.
                    <Button size="sm" disabled={busy}
                      onClick={() => void run(() => api.post(path(a, w.userId), { decision: "allow" }), "Couldn't allow them.")}>Allow</Button>
                    <Button size="sm" variant="ghost" disabled={busy}
                      onClick={() => void run(() => api.post(path(a, w.userId), { decision: "decline" }), "Couldn't decline.")}>Decline</Button>
                  </div>)}
                  {a.allowedSenders.length > 0 && <div className="flex flex-wrap items-center gap-2 text-[var(--midnight-muted)]">
                    Allowed:
                    {a.allowedSenders.map((p) => <Button key={p.userId} size="sm" variant="ghost" disabled={busy}
                      aria-label={`Stop allowing ${p.name}`}
                      onClick={() => void run(() => api.del(path(a, p.userId)), "Couldn't change that.")}>{p.name} ×</Button>)}
                  </div>}
                </li>)}
              </ul>}
        <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); void register(); }}>
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} disabled={busy}
            maxLength={60} aria-label="Agent name" placeholder="Agent name" className={input} />
          <input value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} disabled={busy}
            type="url" aria-label="Webhook URL" placeholder="https://…" className={input} />
          <input value={form.key} onChange={(e) => setForm({ ...form, key: e.target.value })} disabled={busy}
            type="password" autoComplete="off" aria-label="Webhook key" placeholder="Webhook key" className={input} />
          <Button size="sm" type="submit" disabled={busy || !form.name.trim() || !form.url.trim() || !form.key}>Add agent</Button>
        </form>
        {note && <p role="status" className="text-sm text-[var(--midnight)]">{note}</p>}
        {failure && <p role="alert" className="text-sm text-[var(--err)]">{failure}</p>}
      </div>
    </section>
  );
}
