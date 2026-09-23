"use client";

// Shared AgentCore Browser live view with one workspace controller.
//
// Transport: AWS's NICE DCV web client, rendered by `BrowserLiveView` from the
// `bedrock-agentcore` npm package. The DCV SDK is BUNDLED inside that package
// (not a peer dep, not CDN-hosted): the two bare `dcv` / `dcv-ui` imports are
// aliased in `next.config.ts`, and DCV loads its own workers/wasm at runtime
// from the same-origin `baseUrl` `/nice-dcv-web-client-sdk/dcvjs-esm`, which
// `scripts/copy-dcv-assets.mjs` copies into `public/` on predev/prebuild.
// `lazy()` keeps those ~2 MB out of the workspace shell chunk for the desktop
// sessions that never open this panel.
//
// Discover every 5s during a known run, and keep watching a discovered session
// until it ends. Retain each signed URL for 240s (AWS expires it at 300s).
// The server owns the controller lease and automation gate; this component only
// coordinates the member's controls and keeps noncontrollers inert.

import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { useStatusLoaded } from "@lightbulb/shared";
import { ApiError, useApi } from "@/lib/api";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { ensureDcvMediaShim } from "./dcvWebKitShim";

const BrowserLiveView = lazy(() =>
  import("./DirectDcvLiveView").then((m) => ({ default: m.DirectDcvLiveView })),
);

/** The route's constant key set (`GET /api/v1/browser/live-view`). */
interface LiveViewResponse {
  active: boolean;
  url?: string | null;
  expiresAt?: string | null;
  remoteWidth?: number;
  remoteHeight?: number;
  sessionId?: string | null;
  generation?: string;
  mode?: "idle" | "agent" | "human" | "transition" | "error";
  controllerId?: string | null;
  isController?: boolean;
  canControl?: boolean;
  savedAt?: string | null;
  error?: string | null;
  loadedAt?: number;
}

const POLL_WHILE_RUNNING_MS = 5_000;
const REMINT_MS = 240_000;
const INACTIVE: LiveViewResponse = { active: false, mode: "idle", canControl: false };

function Notice({ children }: { children: React.ReactNode }): React.ReactElement {
  return (
    <div className="flex h-full items-center justify-center p-6 text-center text-sm text-[var(--midnight-muted)]">
      <p>{children}</p>
    </div>
  );
}

function DcvLiveView(props: React.ComponentProps<typeof BrowserLiveView>): React.ReactElement {
  if (!ensureDcvMediaShim()) {
    return <Notice>This device cannot initialize the shared browser viewer.</Notice>;
  }
  return <BrowserLiveView {...props} />;
}

function normalizeBrowserUrl(value: string): string {
  const raw = value.trim();
  if (!raw) throw new Error("Enter a website address.");
  const hasScheme = /^[a-z][a-z\d+.-]*:/i.test(raw);
  const parsed = new URL(hasScheme ? raw : `https://${raw}`);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("Use an http:// or https:// address.");
  }
  if (parsed.username || parsed.password) throw new Error("Website addresses cannot include credentials.");
  return parsed.toString();
}

export function BrowserDockPanel(): React.ReactElement | null {
  const api = useApi();
  const { orgId, userId } = useAuth();
  const scope = orgId ?? userId ?? "signed-out";
  // `false` means no status body has landed for this workspace yet, and firing
  // on a guess is exactly what that hold exists to prevent.
  const ready = useStatusLoaded();
  // `roomProps.runs` is `useRooms`' live run state — the `run_active` frames
  // from `useLiveEvents` merged with the `activeRuns` the room detail seeds.
  // Optional because the panel must not throw outside a messaging provider.
  const workspace = useMessagingWorkspaceOptional();
  const runActive = !!workspace?.roomProps?.runs?.some((run) => run.state === "running") ||
    !!workspace?.directoryProps.participants.some((member) => member.agentActivity === "running");

  const [view, setView] = useState<LiveViewResponse | null>(null);
  const [pending, setPending] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);
  const [pollNonce, setPollNonce] = useState(0);
  const [address, setAddress] = useState("");
  const viewEpoch = useRef(0);
  const mutationSeq = useRef(0);
  const alive = useRef(true);
  const autoTakeGeneration = useRef<string | undefined>(undefined);

  const knownSession = useRef(false);

  useEffect(() => {
    setView(null);
    setPending(false);
    setControlError(null);
    setAddress("");
    knownSession.current = false;
    autoTakeGeneration.current = undefined;
  }, [api, ready, scope]);

  useEffect(() => {
    if (!ready) return;
    alive.current = true;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    let timeout: ReturnType<typeof setTimeout>;
    let request: AbortController;
    const load = async () => {
      const epoch = viewEpoch.current;
      request = new AbortController();
      timeout = setTimeout(() => request.abort(), 10_000);
      let body: LiveViewResponse;
      try {
        body = await api.get("/browser/live-view", { signal: request.signal }) as LiveViewResponse;
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          knownSession.current = false;
          setView(INACTIVE);
          return;
        }
        setView((previous) => previous ?? INACTIVE);
        // A failed discovery request is not evidence that a known session ended.
        if (runActive || knownSession.current) timer = setTimeout(load, POLL_WHILE_RUNNING_MS);
        return;
      } finally {
        clearTimeout(timeout);
      }
      if (cancelled) return;
      if (epoch !== viewEpoch.current) return;
      if (!body) return;
      knownSession.current = body.active && !!body.url;
      setView((previous) => {
        const sameSession = !!body.sessionId && body.sessionId === previous?.sessionId;
        const keepUrl = sameSession && body.url && previous?.url &&
          Date.now() - (previous.loadedAt ?? 0) < REMINT_MS;
        return { ...previous, ...body, url: keepUrl ? previous.url : body.url, loadedAt: keepUrl ? previous?.loadedAt : Date.now() };
      });
      if (runActive || body.url) {
        timer = setTimeout(load, POLL_WHILE_RUNNING_MS);
      }
    };
    void load();
    return () => {
      cancelled = true;
      alive.current = false;
      viewEpoch.current += 1;
      mutationSeq.current += 1;
      clearTimeout(timer);
      clearTimeout(timeout);
      request?.abort();
    };
  }, [ready, api, runActive, pollNonce, scope]);

  const mutate = async (path: string, body: Record<string, unknown> = {}) => {
    const seq = ++mutationSeq.current;
    viewEpoch.current += 1;
    setPending(true);
    setControlError(null);
    try {
      const next = await api.post(path, body) as LiveViewResponse;
      if (!alive.current || seq !== mutationSeq.current) return;
      knownSession.current = !!next.active && !!next.url;
      setView((previous) => ({ ...previous, ...next, loadedAt: next.url && next.url !== previous?.url ? Date.now() : previous?.loadedAt }));
    } catch (error) {
      if (!alive.current || seq !== mutationSeq.current) return;
      setControlError(error instanceof Error ? error.message : "Browser control failed");
    } finally {
      if (alive.current && seq === mutationSeq.current) {
        setPending(false);
        setPollNonce((value) => value + 1);
      }
    }
  };

  useEffect(() => {
    if (!view?.active || view.mode !== "idle" || !view.generation || !view.canControl || view.isController) return;
    if (autoTakeGeneration.current === view.generation) return;
    autoTakeGeneration.current = view.generation;
    void mutate("/browser/control", { action: "take", generation: view.generation });
    // `mutate` is intentionally the single serialized mutation path; this effect
    // only fires once per idle generation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view?.active, view?.mode, view?.generation, view?.canControl, view?.isController]);

  if (!ready) return null;
  if (!view) return <Notice>Looking for a live browser session…</Notice>;
  if (view.mode === "error") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-[var(--midnight-muted)]">
        <p role="alert" className="text-err">{view.error || "The shared browser needs to restart."}</p>
        <p className="max-w-sm text-xs">Restarting restores the last saved sign-in state. Any page currently open may be lost.</p>
        <button type="button" disabled={pending} onClick={() => void mutate("/browser/start")} className="rounded-md bg-midnight px-3 py-2 text-xs font-medium text-cream disabled:opacity-50">
          {pending ? "Restarting…" : "Restart browser"}
        </button>
        {controlError && <p role="alert">{controlError}</p>}
      </div>
    );
  }
  if (view.active && view.url && view.generation == null) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <p className="p-3 text-xs text-[var(--midnight-muted)]">Shared browser controls become available after this existing session finishes.</p>
        <Suspense fallback={<Notice>Connecting to the live view…</Notice>}>
          <div inert className="min-h-0 flex-1"><DcvLiveView key={view.sessionId ?? view.generation ?? view.url} signedUrl={view.url} remoteWidth={view.remoteWidth} remoteHeight={view.remoteHeight} /></div>
        </Suspense>
      </div>
    );
  }
  if (!view.active || !view.url) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-[var(--midnight-muted)]">
        <p>No shared browser session is running.</p>
        <p className="max-w-sm text-xs">Sign-in state is shared with your workspace. Starting the browser gives you control so you can sign in once for the team.</p>
        <button type="button" disabled={pending} onClick={() => void mutate("/browser/start")} className="rounded-md bg-midnight px-3 py-2 text-xs font-medium text-cream disabled:opacity-50">
          {pending ? "Starting…" : "Start browser"}
        </button>
        {(controlError || view.error) && <p role="alert" className="text-xs text-err">{controlError || view.error}</p>}
      </div>
    );
  }

  const isController = !!view.isController;
  const canTake = !!view.canControl;
  const busy = pending || view.mode === "transition";
  const take = () => void mutate("/browser/control", { action: "take", generation: view.generation });
  const release = () => void mutate("/browser/control", { action: "release", generation: view.generation });
  const stop = () => void mutate("/browser/stop", { generation: view.generation });
  const navigate = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    try {
      const url = normalizeBrowserUrl(address);
      void mutate("/browser/navigate", { generation: view.generation, url });
    } catch (error) {
      setControlError(error instanceof Error ? error.message : "Enter a valid website address.");
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between gap-3 border-b border-[var(--edge)] px-3 py-2 text-xs">
        <span className="text-[var(--midnight-muted)]">Shared workspace browser · {view.mode === "idle" ? "Taking control…" : view.mode === "human" ? (isController ? "You control it" : "A teammate controls it") : "Agent controls it"}</span>
        <div className="flex items-center gap-2">
          {isController ? <button type="button" disabled={busy} onClick={release} className="rounded-md border border-[var(--edge)] px-2 py-1 disabled:opacity-50">Done &amp; hand back</button> : canTake && <button type="button" disabled={busy} onClick={take} className="rounded-md bg-midnight px-2 py-1 text-cream disabled:opacity-50">Take control</button>}
          {isController && <button type="button" disabled={busy} onClick={stop} className="rounded-md border border-err px-2 py-1 text-err disabled:opacity-50">Done &amp; stop</button>}
        </div>
      </div>
      {controlError && <p role="alert" className="px-3 py-2 text-xs text-err">{controlError} Try the action again; your current browser session is still open.</p>}
      <div className="px-3 py-1 text-[11px] text-[var(--midnight-muted)]">
        {isController ? "Done saves your sign-in and returns control to automation." : view.mode === "agent" ? "Taking control pauses browser automation; the agent run can continue other work." : "Closing this panel only hides it; the shared browser keeps running."}
        {view.savedAt && <> Last saved {new Date(view.savedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}.</>}
      </div>
      {isController && <form onSubmit={navigate} className="flex gap-2 px-3 pb-2" aria-label="Navigate shared browser">
        <input value={address} onChange={(event) => setAddress(event.target.value)} placeholder="Enter website address" disabled={busy} aria-label="Website address" className="min-w-0 flex-1 rounded-md border border-[var(--edge)] bg-cream-deep px-2 py-1 text-xs text-[var(--midnight)] outline-none focus:border-[var(--midnight)] disabled:opacity-50" />
        <button type="submit" disabled={busy} className="rounded-md border border-[var(--edge)] px-2 py-1 text-xs disabled:opacity-50">Go</button>
      </form>}
      <div className="relative min-h-0 flex-1">
        <Suspense fallback={<Notice>Connecting to the live view…</Notice>}>
          <div aria-disabled={!isController} inert={!isController || busy} className="h-full">
            <DcvLiveView key={view.sessionId ?? view.generation ?? view.url} signedUrl={view.url} remoteWidth={view.remoteWidth} remoteHeight={view.remoteHeight} />
          </div>
        </Suspense>
        {!isController && <div aria-label="Read-only browser view" role="region" tabIndex={0} onKeyDown={(event) => event.preventDefault()} onPointerDown={(event) => event.preventDefault()} className="absolute inset-0 z-10 cursor-default" />}
      </div>
      {view.error && <p role="alert" className="px-3 py-2 text-xs text-err">{view.error}</p>}
    </div>
  );
}
