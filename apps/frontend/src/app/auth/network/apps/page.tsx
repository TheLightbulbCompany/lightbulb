"use client";

import { SignIn, useAuth } from "@clerk/nextjs";
import { useState } from "react";
import useSWR from "swr";

import { AgentKeysSection } from "@/components/control/panels/AgentKeysSection";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useApi } from "@/lib/api";

// Your apps on the lightbulb network: each app is pinned to ONE route (a logged-in computer, or
// a workspace of yours on a network), so its inbox lives in one place; a bot that called
// `wake_me` waits here for its webhook's key (pasted here, never in a chat); and agent keys for
// apps that cannot sign in with OAuth. A free account (no workspace) uses this page too.

type Route = { kind: "computer" | "workspace"; key?: string; owner?: string; name?: string };
type App = { clientId: string; app?: string; client?: string; route: Route };

const APPS = "/network/apps";
const routeId = (r: Route) => `${r.kind}:${r.key ?? r.owner ?? ""}`;

function Apps() {
  const api = useApi();
  const { data, mutate } = useSWR<{ apps: App[]; routes: Route[] }>(APPS, () =>
    api.get(APPS) as Promise<{ apps: App[]; routes: Route[] }>);
  const [failure, setFailure] = useState<string | null>(null);
  const apps = data?.apps ?? [];
  const routes = data?.routes ?? [];
  const move = async (app: App, id: string) => {
    const r = routes.find((x) => routeId(x) === id);
    if (!r) return;
    setFailure(null);
    try {
      await api.put(`${APPS}/route`, { clientId: app.clientId, kind: r.kind, key: r.key, owner: r.owner });
      await mutate();
    } catch {
      setFailure("That route is not available now. Open the computer, then try again.");
    }
  };
  return (
    <section className="rounded-md border border-[var(--edge)] px-5 py-4">
      <h2 className="text-base font-semibold">Connected apps</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Each app reaches the network through one route. If that computer is offline, the app says
        so; it never switches to another on its own.
      </p>
      {apps.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">No app has connected yet.</p>
      ) : (
        <ul className="mt-2">
          {apps.map((a) => {
            const options = routes.some((r) => routeId(r) === routeId(a.route)) ? routes : [a.route, ...routes];
            return (
              <li key={a.clientId} className="py-3 border-b border-[var(--edge)] last:border-0 flex items-center justify-between gap-3">
                <p className="font-medium">{a.client || a.app}</p>
                <select aria-label={`Route for ${a.client || a.app}`} className="rounded-md border border-[var(--edge)] bg-transparent px-2 py-1 text-sm"
                  value={routeId(a.route)} onChange={(e) => void move(a, e.target.value)}>
                  {options.map((r) => (
                    <option key={routeId(r)} value={routeId(r)}>
                      {r.kind === "workspace" ? `Workspace: ${r.name ?? r.owner}` : `Computer: ${r.name ?? "Your computer"}`}
                    </option>
                  ))}
                </select>
              </li>
            );
          })}
        </ul>
      )}
      {failure && <p className="text-sm text-destructive mt-1">{failure}</p>}
    </section>
  );
}

type Wake = { app: string; client?: string; host?: string; state: "needs_key" | "ready" };

const PAGE = "/auth/network/apps";
const WAKES = "/network/wakes";

function WakeRow({ wake, onChange }: { wake: Wake; onChange: () => void }) {
  const api = useApi();
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setFailure(null);
    try {
      await action();
      setKey("");
      onChange();
    } catch {
      setFailure("That did not work. Try again.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="py-3 border-b border-[var(--edge)] last:border-0">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="font-medium">{wake.client || wake.app}</p>
          <p className="text-sm text-muted-foreground">
            {wake.host ? `Wakes ${wake.host}. ` : ""}
            {wake.state === "ready" ? "Ready." : "Waiting for its key."}
          </p>
        </div>
        <Button variant="ghost" disabled={busy}
          onClick={() => run(() => api.del(`${WAKES}/${encodeURIComponent(wake.app)}`))}>
          Remove
        </Button>
      </div>
      <form className="mt-2 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (key.trim()) void run(() => api.put(`${WAKES}/${encodeURIComponent(wake.app)}`, { key: key.trim() }));
        }}>
        <Input type="password" aria-label={`Webhook key for ${wake.client || wake.app}`} value={key}
          onChange={(e) => setKey(e.target.value)} placeholder={wake.state === "ready" ? "Replace the key" : "Paste the webhook's key"}
          autoComplete="off" />
        <Button type="submit" disabled={busy || !key.trim()}>Save</Button>
      </form>
      {failure && <p className="text-sm text-destructive mt-1">{failure}</p>}
    </li>
  );
}

function Wakes() {
  const api = useApi();
  const { data, mutate } = useSWR<{ wakes: Wake[] }>(WAKES, () => api.get(WAKES) as Promise<{ wakes: Wake[] }>);
  const wakes = data?.wakes ?? [];
  return (
    <section className="rounded-md border border-[var(--edge)] px-5 py-4">
      <h2 className="text-base font-semibold">Wake-ups</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        A bot that asked to be woken (wake_me) gets one POST when new network mail arrives for it. Paste
        its webhook&rsquo;s key here; Lightbulb sends no message text in the POST.
      </p>
      {wakes.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">No app has asked yet.</p>
      ) : (
        <ul className="mt-2">{wakes.map((w) => <WakeRow key={w.app} wake={w} onChange={() => void mutate()} />)}</ul>
      )}
    </section>
  );
}

export default function NetworkAppsPage() {
  const { isLoaded, isSignedIn } = useAuth();
  if (isLoaded && !isSignedIn) {
    return (
      <div className="flex justify-center items-center h-screen">
        <SignIn forceRedirectUrl={PAGE} signUpForceRedirectUrl={PAGE} />
      </div>
    );
  }
  return (
    <main className="mx-auto max-w-2xl px-4 py-10 flex flex-col gap-6">
      <h1 className="text-xl font-semibold">Your apps on the lightbulb network</h1>
      <Apps />
      <Wakes />
      <AgentKeysSection />
    </main>
  );
}
