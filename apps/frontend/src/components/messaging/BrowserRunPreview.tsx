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

/** The run drives a tab in a person's paired Mac browser (`lightbulb_desktop` browser actions). */
export function usesMacBrowser(run: { tools: ReadonlyArray<{ name: string; args?: unknown }> }): boolean {
  return run.tools.some((call) => call.name === "lightbulb_desktop" && (call.args as { action?: unknown } | undefined)?.action === "browser");
}

/** The paired Mac a run's browser actions go to (its last `lightbulb_desktop` browser call). */
export function macBrowserDevice(run: { tools: ReadonlyArray<{ name: string; args?: unknown }> }): string | undefined {
  const call = [...run.tools].reverse().find((c) => c.name === "lightbulb_desktop" && (c.args as { action?: unknown } | undefined)?.action === "browser");
  const device = (call?.args as { deviceId?: unknown } | undefined)?.deviceId;
  return typeof device === "string" ? device : undefined;
}

/** A run's Mac tab, large and view-only: the Browser panel's Mac view (and mobile's full screen). */
export function MacRunFrames({ runId }: { runId: string }): React.ReactElement {
  return <BrowserFrameView runId={runId} heartbeat="/browser/frames" empty={
    <p role="status" className="p-6 text-center text-sm text-[var(--midnight-muted)]">Waiting for the page on the Mac…</p>} />;
}

/** "Friday is browsing on a Mac": the one tab the agent drives there, live and view-only, for
 * everyone reading the room. Frames flow only while someone watches and the run is active.
 * A click shows it large in the Browser panel, still view-only: nothing ever goes to the Mac. */
export function MacBrowserPreview({ agentName, runId }: { agentName: string; runId: string }): React.ReactElement {
  const label = `${agentName} is browsing on a Mac`;
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
        <BrowserFrameView runId={runId} heartbeat="/browser/frames" empty={
          <span className="p-3 text-xs text-[var(--midnight-muted)]">Waiting for the page…</span>} />
      </button>
      <Dialog open={fullScreen} onOpenChange={setFullScreen}>
        <DialogContent showCloseButton className="flex h-dvh max-w-none flex-col gap-2 rounded-none p-3 pt-4 shadow-none">
          <DialogTitle className="pr-8 text-sm">{label}</DialogTitle>
          <DialogDescription className="sr-only">View only: the live tab on that Mac.</DialogDescription>
          <div className="relative min-h-0 flex-1">{fullScreen && <MacRunFrames runId={runId} />}</div>
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
