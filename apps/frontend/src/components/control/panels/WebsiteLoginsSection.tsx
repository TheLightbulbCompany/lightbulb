"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useApi } from "@/lib/api";

export interface WebsiteLogin {
  id: string;
  origin: string;
  username: string;
  addedBy: string | null;
  addedByName: string | null;
  createdAt: string | null;
  lastUsedAt: string | null;
}

const KEY = "/website-logins";

/**
 * The workspace's website logins: what every agent's browser signs in with. A password is
 * posted once and never comes back -- the list shows the site and username only. Agents
 * also ask for a missing login from Activity; an answer there lands in this same list.
 *
 * Mounted in Connections (and opened from the Browser panel's menu). Admin-only on the
 * server; non-admins see an explanation instead of the list.
 */
export function WebsiteLoginsSection({ canManage }: { canManage: boolean }) {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<{ logins: WebsiteLogin[] }>(
    canManage ? KEY : null,
    () => api.get(KEY) as Promise<{ logins: WebsiteLogin[] }>,
  );
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const logins = data?.logins ?? [];
  const self = useRef<HTMLElement>(null);
  // The Browser panel's menu opens Connections at #website-logins.
  useEffect(() => {
    if (window.location.hash === "#website-logins") self.current?.scrollIntoView?.({ block: "start" });
  }, []);

  const remove = async (login: WebsiteLogin) => {
    if (!window.confirm(`Remove the login for ${host(login.origin)}? Agents will be asked for it again.`)) return;
    setBusy(login.id);
    setFailure(null);
    try {
      await api.del(`${KEY}/${encodeURIComponent(login.id)}`);
      await mutate();
    } catch (e) {
      setFailure(e instanceof Error && e.message ? e.message : "Couldn't remove that login.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section ref={self} id="website-logins" aria-labelledby="website-logins-heading"
      className="overflow-hidden rounded-md border border-[var(--edge)] bg-cream">
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 border-b border-[var(--edge)] px-5 py-4">
        <div className="min-w-0 flex-1">
          <h3 id="website-logins-heading" className="font-editorial-serif text-base font-semibold text-[var(--midnight)]">
            Website logins
          </h3>
          <p className="mt-1 max-w-prose text-sm leading-relaxed text-[var(--midnight-muted)]">
            Sign-ins your agents use in the shared browser. The browser fills the password in
            for them — agents never see it, and it never appears in chat.
          </p>
        </div>
        {canManage && !adding && <Button size="sm" onClick={() => setAdding(true)}>Add login</Button>}
      </header>

      {!canManage
        ? <p className="px-5 py-4 text-sm text-[var(--midnight-muted)]" role="status">
          Only organization admins can manage website logins.
        </p>
        : <div className="px-5 py-4 space-y-3">
          {adding && <AddLoginForm onCancel={() => setAdding(false)} onSaved={async () => { setAdding(false); await mutate(); }} />}
          {error
            ? <p role="alert" className="text-sm text-[var(--err)]">Couldn&apos;t load website logins.</p>
            : isLoading
              ? <p role="status" className="text-sm text-[var(--midnight-muted)]">Loading…</p>
              : logins.length === 0
                ? <p className="text-sm text-[var(--midnight-muted)]">
                  No logins yet. Add one here, or answer an agent&apos;s request for one in Activity.
                </p>
                : <table className="w-full text-sm">
                  <thead className="text-left text-[var(--midnight-muted)]">
                    <tr><th className="py-1 font-normal">Site</th><th className="py-1 font-normal">Username</th>
                      <th className="py-1 font-normal">Added by</th><th className="py-1 font-normal">Last used</th><th /></tr>
                  </thead>
                  <tbody>
                    {logins.map((login) => <tr key={login.id} className="border-t border-[var(--edge)]">
                      <td className="py-2 pr-3 text-[var(--midnight)]">{host(login.origin)}</td>
                      <td className="py-2 pr-3 break-all">{login.username}</td>
                      <td className="py-2 pr-3 text-[var(--midnight-muted)]">{login.addedByName ?? "A workspace admin"}</td>
                      <td className="py-2 pr-3 text-[var(--midnight-muted)]">{login.lastUsedAt ? timeAgo(login.lastUsedAt) : "Never"}</td>
                      <td className="py-2 text-right">
                        <Button size="sm" variant="ghost" disabled={busy === login.id}
                          aria-label={`Remove the login for ${host(login.origin)}`} onClick={() => void remove(login)}>
                          Remove
                        </Button>
                      </td>
                    </tr>)}
                  </tbody>
                </table>}
          {failure && <p role="alert" className="text-sm text-[var(--err)]">{failure}</p>}
        </div>}
    </section>
  );
}

function host(origin: string): string {
  return origin.replace(/^https:\/\//, "");
}

function AddLoginForm({ onCancel, onSaved }: { onCancel: () => void; onSaved: () => Promise<void> }) {
  const api = useApi();
  const [origin, setOrigin] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const ready = Boolean(origin.trim() && username.trim() && password);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ready) return;
    setBusy(true);
    setFailure(null);
    try {
      await api.post(KEY, { origin: origin.trim(), username: username.trim(), password });
      setPassword("");
      await onSaved();
    } catch (err) {
      setFailure(err instanceof Error && err.message ? err.message : "Couldn't save that login.");
    } finally {
      setBusy(false);
    }
  };

  const field = "w-full rounded-md border border-[var(--edge)] bg-[var(--cream)] px-2 py-1.5 text-sm text-[var(--midnight)]";
  return (
    <form onSubmit={(e) => void submit(e)} autoComplete="off" className="space-y-2 rounded-md border border-[var(--edge)] p-3">
      <label className="block text-sm">
        <span className="text-[var(--midnight-muted)]">Sign-in page address</span>
        <input className={field} value={origin} onChange={(e) => setOrigin(e.target.value)} autoFocus
          placeholder="https://app.example.com" inputMode="url" />
      </label>
      <label className="block text-sm">
        <span className="text-[var(--midnight-muted)]">Username or email</span>
        <input className={field} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />
      </label>
      <label className="block text-sm">
        <span className="text-[var(--midnight-muted)]">Password</span>
        <input className={field} type="password" value={password} onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password" />
      </label>
      {failure && <p role="alert" className="text-sm text-[var(--err)]">{failure}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || !ready}>Save login</Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>Cancel</Button>
      </div>
    </form>
  );
}
