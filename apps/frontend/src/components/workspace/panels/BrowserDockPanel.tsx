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
//
// Watching starts it: while this panel is visible (in the pane, and the page not
// hidden) it posts a viewer heartbeat, which starts the browser when none is running and
// keeps it from idle-stopping. Hidden, the heartbeat stops and the 30-min idle clock runs.
// So a stopped browser someone is looking at reads "Starting" with nothing to click. Start
// is offered only where watching does not start it: a person pressed Stop (that stands until
// Start or an agent needs the browser), or the start failed.

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { useStatusLoaded } from "@lightbulb/shared";
import { ApiError, useApi } from "@/lib/api";
import { usePageVisible } from "@/lib/visibility";
import { agentDisplayName, useAgents } from "@/hooks/useAgents";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { ensureDcvMediaShim } from "./dcvWebKitShim";
import { KeyRound, Puzzle } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { BrowserExtensionsSection } from "@/components/control/panels/BrowserExtensionsSection";
import { WebsiteLoginsSection } from "@/components/control/panels/WebsiteLoginsSection";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { selectBrowserMacRun, useBrowserMacRun } from "../workspaceController";
import { MacRunFrames, macBrowser, usesMacBrowser } from "@/components/messaging/BrowserRunPreview";
import { useTargets } from "@/components/messaging/NewSessionButton";
import { useLiveViewHold, useLiveViewWatch } from "./sharedLiveView";

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
  /** The agent using the browser, while one is. */
  agentId?: string | null;
  savedAt?: string | null;
  error?: string | null;
  /** Why no session is running: a person's Stop, our idle stop, or it ended at AWS (8 h ceiling, error). */
  endedReason?: "stopped" | "idle" | "expired" | "restart" | null;
  idleStopSeconds?: number;
  /** While `mode` is "transition": starting a session or saving one. */
  operation?: "start" | "save" | null;
  loadedAt?: number;
}

const POLL_WHILE_RUNNING_MS = 5_000;
/** The visible panel's "someone is watching" signal; the backend counts it for 60 s. */
export const VIEW_HEARTBEAT_MS = 20_000;
const REMINT_MS = 240_000;
/** At most one "a person is using it" signal per window; the agent yields for 3 s after one. */
export const INPUT_THROTTLE_MS = 2_000;
/** Hold a deploy reload this long after a person last used the stream (the 5 s poll re-renders). */
const RELOAD_HOLD_MS = 60_000;
const INACTIVE: LiveViewResponse = { active: false, mode: "idle" };

/** A save still running after this reads as failed (with Retry), never an endless spinner. */
export const SAVE_TIMEOUT_MS = 30_000;

const savedTime = (at?: string | null) =>
  at ? new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : null;

/** A restart for a new extension (stop, then start) still in flight. Bounded by the save it
 * began with, so a restart that died never leaves the panel without its Start button. */
export const restarting = (view: LiveViewResponse): boolean => view.endedReason === "restart" && view.mode !== "error"
  && (view.mode === "transition" || Date.now() - Date.parse(view.savedAt ?? "") < 120_000);

/** The one line that says why the browser is not running, and what was kept. */
export function stoppedMessage(view: LiveViewResponse): string {
  const saved = savedTime(view.savedAt);
  const why = view.endedReason === "idle"
    ? `Browser stopped after ${Math.round((view.idleStopSeconds ?? 1800) / 60)} min idle`
    : view.endedReason === "expired" ? "Browser session ended"
    : view.endedReason === "stopped" ? "Browser stopped" : "No shared browser session is running";
  return saved ? `${why} \u00b7 state saved ${saved}` : `${why}.`;
}

function Notice({ children }: { children: React.ReactNode }): React.ReactElement {
  return (
    <div className="flex h-full items-center justify-center p-6 text-center text-sm text-[var(--midnight-muted)]">
      <p>{children}</p>
    </div>
  );
}

/** The bar's popovers hold whole Connections sections: bounded to the panel, scrolling inside. */
const popover = "w-[min(28rem,calc(100vw-2rem))] max-h-[min(32rem,70vh)] overflow-y-auto p-0";

export function BrowserDockPanel(): React.ReactElement | null {
  const api = useApi();
  const isAdmin = useIsWorkspaceAdmin();
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
  // Runs in this room driving a tab on a paired Mac: the panel can show one instead of the cloud
  // browser (read-only frames). Once the chosen run stops running it falls back to the cloud view.
  const macRuns = (workspace?.roomProps?.runs ?? []).filter((run) => run.state === "running" && usesMacBrowser(run));
  const chosenMacRun = useBrowserMacRun();
  const macRun = macRuns.find((run) => run.runId === chosenMacRun);
  const { data: macTargets } = useTargets(macRuns.length > 0);

  const showingMac = !!macRun;
  const [view, setView] = useState<LiveViewResponse | null>(null);
  const [pending, setPending] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);
  // Why the heartbeat's own start failed; until one lands the panel offers Start, not "Starting".
  const [startError, setStartError] = useState<string | null>(null);
  const [pollNonce, setPollNonce] = useState(0);
  const [menuHost, setMenuHost] = useState<HTMLSpanElement | null>(null);
  const viewEpoch = useRef(0);
  const mutationSeq = useRef(0);
  const alive = useRef(true);
  const lastInput = useRef(0);
  // A deploy reload would drop the live view under a person who is mid sign-in.
  const [inputAt, setInputAt] = useState(0);
  const { agents } = useAgents();
  // The generation whose save has run past SAVE_TIMEOUT_MS (each operation mints a new one).
  const [saveStuck, setSaveStuck] = useState<string | null>(null);
  const savingGeneration = view?.mode === "transition" && view.operation !== "start" ? view.generation ?? null : null;
  useEffect(() => {
    if (!savingGeneration) return;
    const timer = setTimeout(() => setSaveStuck(savingGeneration), SAVE_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [savingGeneration]);

  const knownSession = useRef(false);
  const running = !!view?.active && !!view.url;
  // An error is about the state it happened in: once the session changes, it is stale.
  useEffect(() => setControlError(null), [running, view?.sessionId]);

  // Mounted only while the pane shows it; the page itself may still be backgrounded.
  const pageVisible = usePageVisible();

  // Re-runs (beating at once) when the session comes or goes, so "Starting" is true the moment
  // a watched browser ends, not up to VIEW_HEARTBEAT_MS later.
  useEffect(() => {
    if (!ready || showingMac) return;
    let cancelled = false;
    const beat = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const next = await api.post("/browser/viewing", {}) as LiveViewResponse | undefined;
        if (cancelled || !next) return;
        setStartError(null);
        // The heartbeat's own response is authoritative: a cold start it kicked off may
        // already be live (or a session may have just stopped) by the time it lands, so
        // apply that straight away rather than waiting on the next poll tick to notice.
        setView((previous) => {
          if (previous && previous.active === next.active && previous.sessionId === next.sessionId &&
            previous.mode === next.mode) return previous;
          knownSession.current = !!next.active && !!next.url;
          return { ...previous, ...next, loadedAt: next.url && next.url !== previous?.url ? Date.now() : previous?.loadedAt };
        });
        // It may have just started one: poll until the live view is up.
        if (!next.active) setPollNonce((value) => value + 1);
      } catch (error) {
        // A missed heartbeat only lets the idle clock run, unless it was also this panel's
        // start: then say why and offer Start, and look again (another start may have won).
        if (cancelled) return;
        setStartError(error instanceof Error ? error.message : "The browser could not start");
        setPollNonce((value) => value + 1);
      }
    };
    const onVisibility = () => { if (document.visibilityState === "visible") void beat(); };
    void beat();
    const timer = setInterval(() => void beat(), VIEW_HEARTBEAT_MS);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [ready, showingMac, api, scope, running]);

  useEffect(() => {
    setView(null);
    setPending(false);
    setControlError(null);
    setStartError(null);
    knownSession.current = false;
    mutationSeq.current += 1; // a click made in the previous workspace must not land in this one
  }, [api, ready, scope]);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  // Re-arming this poll (a heartbeat, a run starting or ending, an extension change) never
  // drops a Start or Stop in flight: that left the panel pending for good.
  useEffect(() => {
    if (!ready) return;
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
        // A new generation re-signs the URL, but the viewer keeps its connection (it is keyed
        // by session): the URL is only used if that connection drops and reconnects.
        const sameSession = !!body.sessionId && body.sessionId === previous?.sessionId &&
          body.generation === previous?.generation;
        const keepUrl = sameSession && body.url && previous?.url &&
          Date.now() - (previous.loadedAt ?? 0) < REMINT_MS;
        return { ...previous, ...body, url: keepUrl ? previous.url : body.url, loadedAt: keepUrl ? previous?.loadedAt : Date.now() };
      });
      // A restart (a new extension) stops, then starts: keep watching until it is back.
      if (runActive || body.url || body.mode === "transition" || restarting(body)) {
        timer = setTimeout(load, POLL_WHILE_RUNNING_MS);
      }
    };
    void load();
    return () => {
      cancelled = true;
      viewEpoch.current += 1;
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

  // The page's one live-view client (sharedLiveView) shows its display in `slot` while this
  // panel is on screen; with the pane showing something else, the card previews keep it.
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);
  const streaming = ready && !showingMac && !!view?.active && !!view.url && view.mode !== "error";
  useLiveViewWatch(streaming ? { url: view?.url as string, sessionId: view?.sessionId, remoteWidth: view?.remoteWidth, remoteHeight: view?.remoteHeight } : null);
  useLiveViewHold(streaming && slot ? { slot, menuHost, busy: pending || view?.mode !== "idle" } : null);
  // Any click, key or scroll in the stream goes straight to the remote browser; this only
  // tells the backend a person is there, at most once per INPUT_THROTTLE_MS. Native
  // listeners: the display is a portal from the host, so React events never reach here.
  useEffect(() => {
    if (!slot) return;
    const using = () => {
      if (Date.now() - lastInput.current < INPUT_THROTTLE_MS) return;
      lastInput.current = Date.now();
      setInputAt(lastInput.current);
      void api.post("/browser/input", {}).catch(() => undefined);
    };
    const kinds = ["pointerdown", "keydown", "wheel"] as const;
    kinds.forEach((kind) => slot.addEventListener(kind, using, true));
    return () => kinds.forEach((kind) => slot.removeEventListener(kind, using, true));
  }, [slot, api]);

  // Each state fills the pane.
  const docked = (top: React.ReactNode) => <div className="relative min-h-0 flex-1"><div className="absolute inset-0">{top}</div></div>;

  const renderCloud = (): React.ReactElement | null => {
    if (!ready) return null;
    if (!view) return docked(<Notice>Looking for a live browser session…</Notice>);
    if (view.mode === "error") {
      return docked(
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
    if (!view.active || !view.url) {
      if (restarting(view)) {
        return docked(<Notice>Restarting the browser to load its extensions…</Notice>);
      }
      // Someone looking at it is already starting it (the heartbeat above): say so at once and
      // offer nothing to click, because a click could only collide with that start.
      const watching = pageVisible;
      if (pending || view.mode === "transition" || (watching && view.endedReason !== "stopped" && !startError)) {
        return docked(
          <div data-testid="browser-starting" className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-sm text-[var(--midnight-muted)]">
            <p>Starting the browser…</p>
            {(view.endedReason === "idle" || view.endedReason === "expired") && <p className="max-w-sm text-xs">{stoppedMessage(view)}</p>}
          </div>
        );
      }
      // A person stopped it, or the start failed. Any click here starts a fresh session that
      // restores the saved state: never a dead end.
      const error = controlError || startError || view.error;
      return docked(
        <div onClick={() => void mutate("/browser/start")} data-testid="browser-stopped" className="flex h-full cursor-pointer flex-col items-center justify-center gap-3 p-6 text-center text-sm text-[var(--midnight-muted)]">
          <p>{stoppedMessage(view)}</p>
          <p className="max-w-sm text-xs">One browser for the whole workspace. Sign in once and it is saved for everyone, agents included. Click to start it again; the saved state comes back.</p>
          <button type="button" className="rounded-md bg-midnight px-3 py-2 text-xs font-medium text-cream">Start browser</button>
          {error && <p role="alert" className="text-xs text-err">{error}</p>}
        </div>
      );
    }

    const busy = pending || view.mode === "transition";
    const stop = () => void mutate("/browser/stop");
    // A retry is a fresh "a person used it" signal: the autosave sweep saves again once quiet.
    const retrySave = () => {
      setSaveStuck(null);
      void api.post("/browser/input", {}).catch(() => undefined).finally(() => setPollNonce((value) => value + 1));
    };
    const saveFailed = view.mode === "transition" && saveStuck != null && saveStuck === view.generation;
    const agent = view.mode === "agent" ? agents.find((candidate) => candidate.id === view.agentId) : undefined;
    const status = view.mode === "agent" ? `${agent ? agentDisplayName(agent) : "An agent"} is browsing`
      : view.mode === "transition" ? (view.operation === "start" ? "Starting\u2026" : saveFailed ? "Save failed" : "Saving\u2026") : "idle";
    const saved = savedTime(view.savedAt);

    return (
      <div className="flex h-full min-h-0 flex-col" data-reload-blocker={Date.now() - inputAt < RELOAD_HOLD_MS || undefined}>
        <div className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-[var(--edge)] px-3 text-xs" data-testid="browser-bar">
          <span className="min-w-0 truncate text-[var(--midnight-muted)]">Shared browser · {status}{saved && <> · Saved {saved}</>}</span>
          <div className="flex shrink-0 items-center gap-1.5">
            {saveFailed && <button type="button" onClick={retrySave} className="rounded-md border border-[var(--edge)] px-2 py-0.5">Retry save</button>}
            {/* The same sections as Connections, one click from the browser they configure. */}
            <Popover>
              <PopoverTrigger aria-label="Extensions" title="Extensions" className="rounded-md border border-[var(--edge)] px-1.5 py-0.5"><Puzzle size={14} /></PopoverTrigger>
              <PopoverContent align="end" className={popover}><BrowserExtensionsSection onChanged={() => setPollNonce((value) => value + 1)} /></PopoverContent>
            </Popover>
            <Popover>
              <PopoverTrigger aria-label="Website sign-ins" title="Website sign-ins" className="rounded-md border border-[var(--edge)] px-1.5 py-0.5"><KeyRound size={14} /></PopoverTrigger>
              <PopoverContent align="end" className={popover}><WebsiteLoginsSection canManage={isAdmin} /></PopoverContent>
            </Popover>
            <button type="button" disabled={busy} onClick={stop} title="Saves sign-ins for the workspace and ends this browser session." className="rounded-md border border-[var(--edge)] px-2 py-0.5 disabled:opacity-50">Stop</button>
            <span ref={setMenuHost} className="contents" />
          </div>
        </div>
        {controlError && <p role="alert" className="px-3 py-1 text-xs text-err">{controlError}</p>}
        {docked(ensureDcvMediaShim()
          ? <div ref={setSlot} className="relative h-full" data-testid="browser-viewer" />
          : <Notice>This device cannot initialize the shared browser viewer.</Notice>)}
        {view.error && <p role="alert" className="px-3 py-2 text-xs text-err">{view.error}</p>}
      </div>
    );
  };

  if (!macRuns.length) return renderCloud();
  const agentName = (agentId: string) => {
    const agent = agents.find((candidate) => candidate.id === agentId);
    return agent ? agentDisplayName(agent) : "An agent";
  };
  const macName = (run: (typeof macRuns)[number]) => {
    const mac = macBrowser(run);
    return mac?.deviceName || macTargets?.devices.find((device) => device.deviceId === mac?.deviceId)?.name || "a Mac";
  };
  const tab = (selected: boolean) => `shrink-0 rounded-md px-2 py-0.5 ${selected
    ? "bg-[var(--cream-deep)] text-[var(--midnight)]" : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]"}`;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div role="tablist" aria-label="Which browser" className="flex h-9 shrink-0 items-center gap-1 overflow-x-auto border-b border-[var(--edge)] px-2 text-xs">
        <button type="button" role="tab" aria-selected={!macRun} onClick={() => selectBrowserMacRun(null)} className={tab(!macRun)}>Cloud browser</button>
        {macRuns.map((run) => (
          <button key={run.runId} type="button" role="tab" aria-selected={macRun?.runId === run.runId}
            onClick={() => selectBrowserMacRun(run.runId)} className={tab(macRun?.runId === run.runId)}>
            {agentName(run.agentId)} · {macName(run)}
          </button>
        ))}
      </div>
      <div className="relative min-h-0 flex-1"><div className="absolute inset-0 flex flex-col">
        {macRun ? (
          <>
            <p className="flex h-8 shrink-0 items-center truncate border-b border-[var(--edge)] px-3 text-xs text-[var(--midnight-muted)]" data-testid="mac-browser-bar">
              {agentName(macRun.agentId)} · on {macName(macRun)} · click or type to take over; {agentName(macRun.agentId)} waits while you do
            </p>
            {/* Mounted only while the pane shows this panel on a visible page: the view-tier cast
                (1280 px, 10 fps) and its frames subscription run only for someone who can see them. */}
            {docked(pageVisible ? <MacRunFrames runId={macRun.runId} roomId={macRun.roomId} deviceId={macBrowser(macRun)?.deviceId} /> : null)}
          </>
        ) : renderCloud()}
      </div></div>
    </div>
  );
}
