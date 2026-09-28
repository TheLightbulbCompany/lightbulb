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
import { showBrowserBesideChat } from "@/components/workspace/workspaceController";
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
