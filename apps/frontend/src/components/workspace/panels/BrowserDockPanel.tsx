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
//
// Shared like a screen: whoever is watching can click and type straight into the
// stream while an agent works, and nobody takes or hands back control. The panel
// only tells the backend that a person is using it (throttled), so the agent's
// browser tools yield to them and the backend saves sign-ins once they go quiet.

import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { useStatusLoaded } from "@lightbulb/shared";
import { ApiError, useApi } from "@/lib/api";
import { agentDisplayName, useAgents } from "@/hooks/useAgents";
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
  mode?: "idle" | "agent" | "transition" | "error";
  /** The agent run using the browser, and its agent, while one is. */
  controllerId?: string | null;
  agentId?: string | null;
  savedAt?: string | null;
  error?: string | null;
  loadedAt?: number;
}

const POLL_WHILE_RUNNING_MS = 5_000;
const REMINT_MS = 240_000;
/** At most one "a person is using it" signal per window; the agent yields for 3 s after one. */
export const INPUT_THROTTLE_MS = 2_000;
/** Hold a deploy reload this long after a person last used the stream (the 5 s poll re-renders). */
const RELOAD_HOLD_MS = 60_000;
const INACTIVE: LiveViewResponse = { active: false, mode: "idle" };

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
  const [menuHost, setMenuHost] = useState<HTMLSpanElement | null>(null);
  const viewEpoch = useRef(0);
  const mutationSeq = useRef(0);
  const alive = useRef(true);
  const lastInput = useRef(0);
  // A deploy reload would drop the live view under a person who is mid sign-in.
  const [inputAt, setInputAt] = useState(0);
  const { agents } = useAgents();

  const knownSession = useRef(false);

  useEffect(() => {
    setView(null);
    setPending(false);
    setControlError(null);
    knownSession.current = false;
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
        // A control change (new generation) re-signs and reconnects the viewer: AWS resets the
        // live view when automation is switched, and a viewer kept across that can go blank
        // (prod 2026-09-26). Watching must always show the current picture.
        const sameSession = !!body.sessionId && body.sessionId === previous?.sessionId &&
          body.generation === previous?.generation;
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
      setControlError(error instanceof Error ? error.message : "The browser request failed");
    } finally {
      if (alive.current && seq === mutationSeq.current) {
        setPending(false);
        setPollNonce((value) => value + 1);
      }
    }
  };

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
          <div inert className="relative min-h-0 flex-1"><DcvLiveView key={view.sessionId ?? view.generation ?? view.url} signedUrl={view.url} remoteWidth={view.remoteWidth} remoteHeight={view.remoteHeight} /></div>
        </Suspense>
      </div>
    );
  }
  if (!view.active || !view.url) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-[var(--midnight-muted)]">
        <p>No shared browser session is running.</p>
        <p className="max-w-sm text-xs">One browser for the whole workspace. Sign in once and it is saved for everyone, agents included.</p>
        <button type="button" disabled={pending} onClick={() => void mutate("/browser/start")} className="rounded-md bg-midnight px-3 py-2 text-xs font-medium text-cream disabled:opacity-50">
          {pending ? "Starting…" : "Start browser"}
        </button>
        {(controlError || view.error) && <p role="alert" className="text-xs text-err">{controlError || view.error}</p>}
      </div>
    );
  }

  const busy = pending || view.mode === "transition";
  // Any click, key or scroll in the stream goes straight to the remote browser; this only
  // tells the backend a person is there, at most once per INPUT_THROTTLE_MS.
  const using = () => {
    if (Date.now() - lastInput.current < INPUT_THROTTLE_MS) return;
    lastInput.current = Date.now();
    setInputAt(lastInput.current);
    void api.post("/browser/input", {}).catch(() => undefined);
  };
  const stop = () => void mutate("/browser/stop");
  const agent = view.mode === "agent" ? agents.find((candidate) => candidate.id === view.agentId) : undefined;
  const status = view.mode === "agent" ? `${agent ? agentDisplayName(agent) : "An agent"} is browsing`
    : view.mode === "transition" ? "Saving\u2026" : "idle";
  const saved = view.savedAt && new Date(view.savedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

  return (
    <div className="flex h-full min-h-0 flex-col" data-reload-blocker={Date.now() - inputAt < RELOAD_HOLD_MS || undefined}>
      <div className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-[var(--edge)] px-3 text-xs" data-testid="browser-bar">
        <span className="min-w-0 truncate text-[var(--midnight-muted)]">Shared browser · {status}{saved && <> · Saved {saved}</>}</span>
        <div className="flex shrink-0 items-center gap-1.5">
          <button type="button" disabled={busy} onClick={stop} title="Saves sign-ins for the workspace and ends this browser session." className="rounded-md border border-[var(--edge)] px-2 py-0.5 disabled:opacity-50">Stop</button>
          <span ref={setMenuHost} className="contents" />
        </div>
      </div>
      {controlError && <p role="alert" className="px-3 py-1 text-xs text-err">{controlError}</p>}
      <div className="relative min-h-0 flex-1">
        <Suspense fallback={<Notice>Connecting to the live view…</Notice>}>
          <div className="h-full" data-testid="browser-viewer" onPointerDownCapture={using} onKeyDownCapture={using} onWheelCapture={using}>
            <DcvLiveView key={`${view.sessionId ?? view.url}:${view.generation}`} signedUrl={view.url} remoteWidth={view.remoteWidth} remoteHeight={view.remoteHeight} menuHost={menuHost} />
          </div>
        </Suspense>
      </div>
      {view.error && <p role="alert" className="px-3 py-2 text-xs text-err">{view.error}</p>}
    </div>
  );
}
