"use client";

// "Friday is browsing example.com" under a live run's Working card, with a small live
// preview of the shared browser, so nobody has to press a button to see what the agent is
// doing. The preview is the default view: nothing opens the Browser panel on its own, and
// clicking the preview opens it.
//
// The preview is a copy of the page's one live-view connection (sharedLiveView), never a
// connection of its own: AgentCore streams a session to one viewer only.

import { useEffect, useState } from "react";
import { Globe } from "lucide-react";
import { browsingSite, frameText, type RoomRunView } from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { showBrowserBesideChat, showMacRunInBrowser } from "@/components/workspace/workspaceController";
import { BrowserFrameView } from "@/components/workspace/panels/BrowserFrameView";
import { LiveViewMirror, useLiveViewWatch, useSharedLiveView } from "@/components/workspace/panels/sharedLiveView";

const REMINT_MS = 240_000;

interface LiveView {
  active: boolean;
  url?: string | null;
  sessionId?: string | null;
  remoteWidth?: number;
  remoteHeight?: number;
}

type DesktopCall = { name: string; args?: unknown; result?: unknown; status?: string };
type MacTarget = { deviceId?: string; deviceName?: string };

/** The run's last `lightbulb_desktop` call naming a browser target: `use_browser` /
 *  `use_cloud_browser`. */
function lastBrowserSelection(run: { tools: ReadonlyArray<DesktopCall> }): DesktopCall | undefined {
  return [...run.tools].reverse().find((call) => {
    if (call.name !== "lightbulb_desktop") return false;
    const action = (call.args as { action?: unknown } | undefined)?.action;
    return action === "use_browser" || action === "use_cloud_browser";
  });
}

/** A tool result can arrive as a raw JSON string or as content blocks wrapping one. */
function parseJsonResult(result: unknown): unknown {
  const text = typeof result === "string" ? result : frameText(result);
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** A successful `use_browser` call's `{browser:"mac",deviceId,deviceName}` result. A failed
 *  or still-running call is not a Mac run (UX #7). */
function macResultOf(call: DesktopCall): MacTarget | undefined {
  if (call.status !== "done") return undefined;
  const parsed = parseJsonResult(call.result);
  if (!parsed || typeof parsed !== "object") return undefined;
  const { browser, deviceId, deviceName } = parsed as { browser?: unknown; deviceId?: unknown; deviceName?: unknown };
  if (browser !== "mac") return undefined;
  return {
    deviceId: typeof deviceId === "string" ? deviceId : undefined,
    deviceName: typeof deviceName === "string" ? deviceName : undefined,
  };
}

/** The run drives a tab in a person's paired Mac browser: a successful `use_browser`. */
export function usesMacBrowser(run: { tools: ReadonlyArray<DesktopCall> }): boolean {
  const call = lastBrowserSelection(run);
  if (!call) return false;
  const action = (call.args as { action?: unknown } | undefined)?.action;
  return action === "use_browser" && macResultOf(call) !== undefined;
}

/** The paired Mac a run's browser actions go to, from that same successful `use_browser`:
 *  its display name (UX #6, undefined for an unnamed Mac). */
export function macBrowser(run: { tools: ReadonlyArray<DesktopCall> }): MacTarget | undefined {
  const call = lastBrowserSelection(run);
  if (!call) return undefined;
  const action = (call.args as { action?: unknown } | undefined)?.action;
  return action === "use_browser" ? macResultOf(call) : undefined;
}

/** A run's Mac tab, large, live and interactive: the Browser panel's Mac view (and mobile's full
 * screen). Any member watching can click and type into it; the agent waits while they do. A run
 * that never named its Mac (legacy calls) has nowhere to send input: its thumbnail, view-only. */
export function MacRunFrames({ runId, deviceId }: { runId: string; deviceId?: string }): React.ReactElement {
  const empty = <p role="status" className="p-6 text-center text-sm text-[var(--midnight-muted)]">Waiting for the page on the Mac…</p>;
  return deviceId
    ? <BrowserFrameView runId={runId} mac={{ deviceId }} prefer="view" empty={empty} />
    : <BrowserFrameView runId={runId} heartbeat="/browser/frames" heartbeatBody={{ tier: "thumb" }} empty={empty} />;
}

/** "Friday is browsing on <device name>" (UX #6, falling back to "a Mac"): the one tab the
 * agent drives there, as a small thumbnail for everyone reading the room. Frames flow only
 * while someone watches and the run is active. A click shows it large and interactive in the
 * Browser panel (or full screen on mobile). */
export function MacBrowserPreview({ agentName, runId, deviceId, deviceName, site }: {
  agentName: string; runId: string; deviceId?: string; deviceName?: string; site?: string | null;
}): React.ReactElement {
  const label = `${agentName} is browsing${site ? ` ${site}` : ""} on ${deviceName || "a Mac"}`;
  // The mobile shell has no dock: the same frames, full screen.
  const [fullScreen, setFullScreen] = useState(false);
  const open = () => { if (!showMacRunInBrowser(runId)) setFullScreen(true); };
  return (
    <div className="mt-1.5 flex flex-col gap-1.5" data-testid="mac-browser-preview">
      <button type="button" onClick={open} className="flex w-fit items-center gap-1.5 text-xs text-[var(--midnight-muted)] hover:text-[var(--midnight)]">
        <Globe size={12} aria-hidden="true" />{label}
      </button>
      <button type="button" onClick={open} aria-label={`${label}. Show it in the Browser panel`}
        className="relative aspect-video w-72 max-w-full overflow-hidden rounded-md border border-[var(--edge)] bg-[var(--cream-deep)]">
        <BrowserFrameView runId={runId} heartbeat="/browser/frames" heartbeatBody={{ tier: "thumb" }} empty={
          <span className="p-3 text-xs text-[var(--midnight-muted)]">Waiting for the page…</span>} />
      </button>
      <Dialog open={fullScreen} onOpenChange={setFullScreen}>
        <DialogContent showCloseButton className="flex h-dvh max-w-none flex-col gap-2 rounded-none p-3 pt-4 shadow-none">
          <DialogTitle className="pr-8 text-sm">{label}</DialogTitle>
          <DialogDescription className="sr-only">The live tab on that Mac.</DialogDescription>
          <div className="relative min-h-0 flex-1">{fullScreen && <MacRunFrames runId={runId} deviceId={deviceId} />}</div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export function BrowserRunPreview({ agentName, site, runId }: { agentName: string; site: string | null; runId?: string }): React.ReactElement {
  const api = useApi();
  const [view, setView] = useState<LiveView | null>(null);
  const { status } = useSharedLiveView();

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      void (api.get("/browser/live-view") as Promise<LiveView>).then(
        (body) => { if (!cancelled) setView(body); },
        () => undefined,
      );
    };
    load();
    const timer = setInterval(load, REMINT_MS); // AWS expires the signed URL at 300 s
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [api]);

  const source = view?.active && view.url
    ? { url: view.url, sessionId: view.sessionId, remoteWidth: view.remoteWidth, remoteHeight: view.remoteHeight }
    : null;
  useLiveViewWatch(source);

  const label = `${agentName} is browsing${site ? ` ${site}` : ""}`;
  const open = () => showBrowserBesideChat({ focus: true, asTabWhenNarrow: true });
  return (
    <div className="mt-1.5 flex flex-col gap-1.5" data-testid="browser-run-preview">
      <button type="button" onClick={open} className="flex w-fit items-center gap-1.5 text-xs text-[var(--midnight-muted)] hover:text-[var(--midnight)]">
        <Globe size={12} aria-hidden="true" />{label}
      </button>
      {source && (
        <button type="button" onClick={open} aria-label={`${label}. Open the shared browser`}
          className="relative aspect-video w-72 max-w-full overflow-hidden rounded-md border border-[var(--edge)] bg-[var(--cream-deep)]">
          {status === "elsewhere"
            // Another screen holds the one live view: this run's own tab, as live frames.
            ? <span className="pointer-events-none" inert><BrowserFrameView runId={runId} empty={
                <span className="p-3 text-xs text-[var(--midnight-muted)]">Being watched elsewhere</span>} /></span>
            : <LiveViewMirror className="pointer-events-none h-full w-full object-contain" />}
        </button>
      )}
    </div>
  );
}

/** The one browser card under a live run. A Mac run drives the same browser_* tools as a cloud
 *  run, so it gets the Mac card only; the cloud card would show the idle cloud session instead. */
export function RunBrowserPreview({ agentName, run }: { agentName: string; run: RoomRunView }): React.ReactElement | null {
  const site = browsingSite(run);
  if (usesMacBrowser(run)) {
    // Only navigations since the switch to the Mac name its site; an earlier cloud page must not.
    const since = run.tools.slice((run.tools as ReadonlyArray<DesktopCall>).lastIndexOf(lastBrowserSelection(run)!) + 1);
    return <MacBrowserPreview agentName={agentName} site={browsingSite({ tools: since }) ?? null} runId={run.runId}
      deviceId={macBrowser(run)?.deviceId} deviceName={macBrowser(run)?.deviceName} />;
  }
  return site === undefined ? null : <BrowserRunPreview agentName={agentName} site={site} runId={run.runId} />;
}
