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
// coordinates the member's controls and keeps noncontrollers inert. Watching never
// takes or renews control: only an explicit Take control does, and the controller's
// own input in the viewer renews it (throttled). A lease with no input lapses on the
// server, and the agent may then take the browser over.

import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { useStatusLoaded } from "@lightbulb/shared";
import { ApiError, useApi } from "@/lib/api";
import { notify } from "@/lib/notify";
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
/** At most one input heartbeat per window; the server's idle lease is minutes long. */
export const RENEW_THROTTLE_MS = 30_000;
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
  const taking = useRef(false);
  const viewEpoch = useRef(0);
  const mutationSeq = useRef(0);
  const alive = useRef(true);
  const lastRenew = useRef(0);
  const wasController = useRef(false);
  const [tookOver, setTookOver] = useState(false);

  const knownSession = useRef(false);

  useEffect(() => {
    setView(null);
    setPending(false);
    setControlError(null);
    knownSession.current = false;
    wasController.current = false;
    setTookOver(false);
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

  const mutate = async (path: string, body: Record<string, unknown> = {}): Promise<boolean> => {
    const seq = ++mutationSeq.current;
    viewEpoch.current += 1;
    setPending(true);
    setControlError(null);
    try {
      const next = await api.post(path, body) as LiveViewResponse;
      if (!alive.current || seq !== mutationSeq.current) return false;
      knownSession.current = !!next.active && !!next.url;
      setView((previous) => ({ ...previous, ...next, loadedAt: next.url && next.url !== previous?.url ? Date.now() : previous?.loadedAt }));
      return !!next.isController;
    } catch (error) {
      if (!alive.current || seq !== mutationSeq.current) return false;
      setControlError(error instanceof Error ? error.message : "Browser control failed");
      return false;
    } finally {
      if (alive.current && seq === mutationSeq.current) {
        setPending(false);
        setPollNonce((value) => value + 1);
      }
    }
  };

  // Losing control to the agent is only observable by polling: it happens when the
  // server-side idle lease lapses and the agent's acquire claims the browser.
  useEffect(() => {
    if (view?.isController) setTookOver(false);
    else if (wasController.current && view?.mode === "agent") setTookOver(true);
    wasController.current = !!view?.isController;
  }, [view?.isController, view?.mode]);

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
        <p className="max-w-sm text-xs">Sign-in state is shared with your workspace. Starting the browser gives you control so you can sign in once for the team.</p>
        <button type="button" disabled={pending} onClick={() => void mutate("/browser/start")} className="rounded-md bg-midnight px-3 py-2 text-xs font-medium text-cream disabled:opacity-50">
          {pending ? "Starting…" : "Start browser"}
        </button>
        {(controlError || view.error) && <p role="alert" className="text-xs text-err">{controlError || view.error}</p>}
      </div>
    );
  }

  const isController = !!view.isController;
  const lapsed = !isController && view.mode === "human" && !!userId && view.controllerId === userId;
  const canTake = !!view.canControl;
  const renew = () => {
    if (!isController || !view.generation || Date.now() - lastRenew.current < RENEW_THROTTLE_MS) return;
    lastRenew.current = Date.now();
    // Best effort: a refused renew means control already changed, which the next poll shows.
    void api.post("/browser/control", { action: "renew", generation: view.generation }).catch(() => undefined);
  };
  const busy = pending || view.mode === "transition";
  // Clicking or typing into the stream IS the take-control gesture. The overlay
  // swallows that first event (DCV never sees it) and one request goes out no
  // matter how many events follow; once control lands the overlay unmounts and
  // input reaches DCV directly.
  const takeFromInput = (event: React.PointerEvent | React.KeyboardEvent) => {
    // Tabbing through the page is navigation, not a request to drive the browser.
    if ("key" in event && event.key === "Tab") return;
    event.preventDefault();
    if (!canTake || busy || taking.current) return;
    taking.current = true;
    void mutate("/browser/control", { action: "take", generation: view.generation }).then((granted) => {
      // The gesture that asked for control was swallowed; say so once.
      if (granted) notify("You\u2019re in control. Click again to continue.", { dedupeKey: "browser-control" });
    }).finally(() => {
      taking.current = false;
    });
  };
  const release = () => void mutate("/browser/control", { action: "release", generation: view.generation });
  const stop = () => void mutate("/browser/stop", { generation: view.generation });
  const runAgent = workspace?.roomProps?.runs?.find((run) => run.runId === view.controllerId)?.agentId;
  const agentName = workspace?.directoryProps.participants.find((member) => member.id === runAgent)?.displayName ?? "An agent";
  const status = isController ? "You\u2019re in control"
    : view.mode === "agent" ? (tookOver ? `${agentName} took over` : `${agentName} is using it`)
      : view.mode === "human" ? (lapsed ? "Your control lapsed" : "A teammate is using it")
        : view.mode === "transition" ? "Switching\u2026" : "No one is using it";
  const saved = view.savedAt && new Date(view.savedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

  return (
    <div className="flex h-full min-h-0 flex-col" data-reload-blocker={isController || undefined}>
      <div className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-[var(--edge)] px-3 text-xs" data-testid="browser-bar">
        <span className="min-w-0 truncate text-[var(--midnight-muted)]">Shared browser · {status}{saved && <> · Saved {saved}</>}</span>
        <div className="flex shrink-0 items-center gap-1.5">
          {isController && <>
            <button type="button" disabled={busy} onClick={release} title="Saves your sign-ins for the workspace and hands the browser back to automation." className="rounded-md border border-[var(--edge)] px-2 py-0.5 disabled:opacity-50">Done &amp; hand back</button>
            <button type="button" disabled={busy} onClick={stop} title="Saves your sign-ins for the workspace and stops the browser." className="rounded-md border border-err px-2 py-0.5 text-err disabled:opacity-50">Done &amp; stop</button>
          </>}
          <span ref={setMenuHost} className="contents" />
        </div>
      </div>
      {controlError && <p role="alert" className="px-3 py-1 text-xs text-err">{controlError} Try again; your current browser session is still open.</p>}
      <div className="relative min-h-0 flex-1">
        <Suspense fallback={<Notice>Connecting to the live view…</Notice>}>
          <div aria-disabled={!isController} inert={!isController || busy} className="h-full" data-testid="browser-viewer" onPointerDownCapture={renew} onPointerMoveCapture={renew} onKeyDownCapture={renew} onWheelCapture={renew}>
            <DcvLiveView key={`${view.sessionId ?? view.url}:${view.generation}`} signedUrl={view.url} remoteWidth={view.remoteWidth} remoteHeight={view.remoteHeight} menuHost={menuHost} />
          </div>
        </Suspense>
        {!isController && <div
          aria-label={canTake ? "Click to take control of the shared browser" : "Read-only browser view"}
          role="region"
          tabIndex={0}
          data-testid="browser-take-overlay"
          onKeyDown={canTake ? takeFromInput : (event) => event.preventDefault()}
          onPointerDown={canTake ? takeFromInput : (event) => event.preventDefault()}
          className={`absolute inset-0 z-10 flex items-center justify-center ${canTake ? "cursor-pointer" : "cursor-default"}`}
        >
          {canTake && pending && taking.current && <span role="status" className="rounded-md bg-midnight/80 px-3 py-1.5 text-xs text-cream">Taking control…</span>}
        </div>}
      </div>
      {view.error && <p role="alert" className="px-3 py-2 text-xs text-err">{view.error}</p>}
    </div>
  );
}
