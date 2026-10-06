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

/** One registrable domain: its saved logins, and whether the workspace browser is signed in there. */
export interface WebsiteSite {
  site: string;
  signedIn: boolean;
  logins: WebsiteLogin[];
}

const KEY = "/website-logins";
const SITES = `${KEY}/sites`;
const field = "w-full rounded-md border border-[var(--edge)] bg-[var(--plane-pane)] px-2 py-1.5 text-sm text-[var(--fg)]";

function message(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

/**
 * Website sign-ins: everything the workspace's agents sign in to websites with, one row per
 * site. A site can have saved logins (a username whose password the browser fills in; the
 * password is posted once and never comes back) and a live sign-in the shared browser keeps
 * on its own (its cookies). Agents also ask for a missing login from Activity; an answer
 * there lands in this same list.
 *
 * Mounted in Connections and Integrations, and opened from the Browser panel's bar.
 * Admin-only on the server; non-admins see an explanation instead of the list.
 */
export function WebsiteLoginsSection({ canManage }: { canManage: boolean }) {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<{ sites: WebsiteSite[] }>(
    canManage ? SITES : null,
    () => api.get(SITES) as Promise<{ sites: WebsiteSite[] }>,
  );
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<{ id: string; what: "username" | "password" } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const sites = data?.sites ?? [];
  const self = useRef<HTMLElement>(null);
  // The Browser panel's menu opens Connections at #website-logins.
  useEffect(() => {
    if (window.location.hash === "#website-logins") self.current?.scrollIntoView?.({ block: "start" });
  }, []);

  const run = async (key: string, fallback: string, work: () => Promise<unknown>) => {
    setBusy(key);
    setFailure(null);
    try {
      await work();
      await mutate();
    } catch (e) {
      setFailure(message(e, fallback));
    } finally {
      setBusy(null);
    }
  };

  const remove = (login: WebsiteLogin) => {
    if (!window.confirm(`Remove ${login.username} for ${host(login.origin)}? Agents will be asked for it again.`)) return;
    void run(login.id, "Couldn't remove that login.", () => api.del(`${KEY}/${encodeURIComponent(login.id)}`));
  };

  const signOut = (site: string) => {
    if (!window.confirm(`Agents will be signed out of ${site} in the workspace browser.`)) return;
    void run(`site:${site}`, "Couldn't sign out. Try again.", () => api.post(`${SITES}/sign-out`, { site }));
  };

  return (
    <section ref={self} id="website-logins" aria-labelledby="website-logins-heading"
      className="overflow-hidden rounded-md border border-[var(--edge)] bg-[var(--plane-pane)]">
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 border-b border-[var(--edge)] px-5 py-4">
        <div className="min-w-0 flex-1">
          <h3 id="website-logins-heading" className="font-editorial-serif text-base font-semibold text-[var(--fg)]">
            Website sign-ins
          </h3>
          <p className="mt-1 max-w-prose text-sm leading-relaxed text-[var(--fg-soft)]">
            The sites your agents are signed in to in the workspace browser, and the logins
            saved for them. The browser fills a saved password in for them — agents never see
            it, and it never appears in chat.
          </p>
        </div>
        {canManage && !adding && <Button size="sm" onClick={() => setAdding(true)}>Add login</Button>}
      </header>

      {!canManage
        ? <p className="px-5 py-4 text-sm text-[var(--fg-soft)]" role="status">
          Only organization admins can manage website sign-ins.
        </p>
        : <div className="space-y-3 px-5 py-4">
          {adding && <AddLoginForm onCancel={() => setAdding(false)} onSaved={async () => { setAdding(false); await mutate(); }} />}
          {error
            ? <p role="alert" className="text-sm text-[var(--err)]">Couldn&apos;t load website sign-ins.</p>
            : isLoading
              ? <p role="status" className="text-sm text-[var(--fg-soft)]">Loading…</p>
              : sites.length === 0
                ? <p className="text-sm text-[var(--fg-soft)]">
                  Nothing yet. Add a login here, answer an agent&apos;s request for one in Activity,
                  or sign in yourself in the Browser panel.
                </p>
                : <ul className="divide-y divide-[var(--edge)]">
                  {sites.map((site) => <li key={site.site} aria-label={site.site} className="space-y-2 py-3 first:pt-0 last:pb-0">
                    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
                      <span className="min-w-0 break-all text-sm font-medium text-[var(--fg)]">{site.site}</span>
                      {site.signedIn
                        ? <span className="flex items-center gap-2 text-sm text-[var(--fg-soft)]">
                          <span aria-hidden className="size-1.5 rounded-full bg-[var(--live)]" />
                          Browser session saved
                          <Button size="sm" variant="ghost" disabled={busy === `site:${site.site}`}
                            aria-label={`Sign out of ${site.site}`} onClick={() => signOut(site.site)}>
                            Sign out
                          </Button>
                        </span>
                        : <span className="text-sm text-[var(--fg-mute)]">No browser session</span>}
                    </div>
                    {site.logins.length === 0
                      ? <p className="text-sm text-[var(--fg-mute)]">No saved password. The browser keeps a session for this site.</p>
                      : <ul className="space-y-2">
                        {site.logins.map((login) => <li key={login.id} className="text-sm">
                          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
                            <div className="min-w-0">
                              <span className="break-all text-[var(--fg)]">{login.username}</span>
                              <span className="block text-xs text-[var(--fg-mute)]">
                                {host(login.origin)} · Added by {login.addedByName ?? "a workspace admin"} · {login.lastUsedAt ? `Used ${timeAgo(login.lastUsedAt)}` : "Never used"}
                              </span>
                            </div>
                            <div className="flex shrink-0 gap-1">
                              <Button size="sm" variant="ghost" disabled={busy === login.id}
                                aria-label={`Edit the username ${login.username}`}
                                onClick={() => setEditing({ id: login.id, what: "username" })}>Edit</Button>
                              <Button size="sm" variant="ghost" disabled={busy === login.id}
                                aria-label={`Replace the password for ${login.username}`}
                                onClick={() => setEditing({ id: login.id, what: "password" })}>Replace password</Button>
                              <Button size="sm" variant="ghost" disabled={busy === login.id}
                                aria-label={`Remove ${login.username} for ${host(login.origin)}`}
                                onClick={() => remove(login)}>Remove</Button>
                            </div>
                          </div>
                          {editing?.id === login.id && <EditLoginForm key={editing.what} login={login} what={editing.what}
                            onCancel={() => setEditing(null)} onSaved={async () => { setEditing(null); await mutate(); }} />}
                        </li>)}
                      </ul>}
                  </li>)}
                </ul>}
          {failure && <p role="alert" className="text-sm text-[var(--err)]">{failure}</p>}
        </div>}
    </section>
  );
}

function host(origin: string): string {
  return origin.replace(/^https:\/\//, "");
}

/** One field at a time: fix the username, or replace the password (never shown, never read back). */
function EditLoginForm({ login, what, onCancel, onSaved }: {
  login: WebsiteLogin; what: "username" | "password"; onCancel: () => void; onSaved: () => Promise<void>;
}) {
  const api = useApi();
  const [value, setValue] = useState(what === "username" ? login.username : "");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const sent = what === "username" ? value.trim() : value;
  const ready = Boolean(sent) && (what === "password" || sent !== login.username);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ready) return;
    setBusy(true);
    setFailure(null);
    try {
      await api.patch(`${KEY}/${encodeURIComponent(login.id)}`, { [what]: sent });
      setValue("");
      await onSaved();
    } catch (err) {
      setFailure(message(err, "Couldn't save that change."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)} autoComplete="off" className="mt-2 space-y-2 rounded-md border border-[var(--edge)] p-3">
      <label className="block text-sm">
        <span className="text-[var(--fg-soft)]">{what === "username" ? "Username or email" : `New password for ${login.username}`}</span>
        <input className={field} value={value} onChange={(e) => setValue(e.target.value)} autoFocus
          type={what === "password" ? "password" : "text"} autoComplete={what === "password" ? "new-password" : "off"} />
      </label>
      {failure && <p role="alert" className="text-sm text-[var(--err)]">{failure}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || !ready}>{what === "username" ? "Save username" : "Save password"}</Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>Cancel</Button>
      </div>
    </form>
  );
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
      setFailure(message(err, "Couldn't save that login."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)} autoComplete="off" className="space-y-2 rounded-md border border-[var(--edge)] p-3">
      <label className="block text-sm">
        <span className="text-[var(--fg-soft)]">Sign-in page address</span>
        <input className={field} value={origin} onChange={(e) => setOrigin(e.target.value)} autoFocus
          placeholder="https://app.example.com" inputMode="url" />
      </label>
      <label className="block text-sm">
        <span className="text-[var(--fg-soft)]">Username or email</span>
        <input className={field} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />
      </label>
      <label className="block text-sm">
        <span className="text-[var(--fg-soft)]">Password</span>
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
