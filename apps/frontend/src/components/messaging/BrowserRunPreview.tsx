"use client";

// "Friday is browsing example.com" under a live run's Working card, with a small live
// thumbnail of the shared browser, so nobody has to press a button to see what the agent
// is doing. The first time an agent starts browsing in the room a person is reading, the
// Browser panel also opens beside the chat (once per browser tab session, wide docks only);
// closing it leaves this thumbnail, and clicking the thumbnail brings the panel back.
//
// One live stream per page: while the Browser panel is open, it carries the stream and this
// shows the line alone.

import { Suspense, lazy, useEffect, useState } from "react";
import { Globe } from "lucide-react";
import { useApi } from "@/lib/api";
import { showBrowserBesideChat, useWorkspaceState } from "@/components/workspace/workspaceController";
import { ensureDcvMediaShim } from "@/components/workspace/panels/dcvWebKitShim";

const LiveView = lazy(() =>
  import("@/components/workspace/panels/DirectDcvLiveView").then((m) => ({ default: m.DirectDcvLiveView })),
);

/** sessionStorage key: the panel auto-opens once per browser tab session, not on every run. */
export const AUTO_OPENED_KEY = "lightbulb.browser.autoOpened";
const REMINT_MS = 240_000;

interface LiveView {
  active: boolean;
  url?: string | null;
  sessionId?: string | null;
  remoteWidth?: number;
  remoteHeight?: number;
}

function autoOpenOnce(): void {
  try {
    if (window.sessionStorage.getItem(AUTO_OPENED_KEY)) return;
    if (showBrowserBesideChat()) window.sessionStorage.setItem(AUTO_OPENED_KEY, "1");
  } catch {
    // Storage unavailable (private mode): opening on every run would be worse than never.
  }
}

export function BrowserRunPreview({ agentName, site }: { agentName: string; site: string | null }): React.ReactElement {
  const api = useApi();
  // Only a Browser panel on screen carries the stream; one open behind Messages does not.
  const panelOpen = useWorkspaceState().browserVisible;
  const [view, setView] = useState<LiveView | null>(null);

  useEffect(autoOpenOnce, []);

  useEffect(() => {
    if (panelOpen) return;
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
  }, [api, panelOpen]);

  const label = `${agentName} is browsing${site ? ` ${site}` : ""}`;
  const open = () => showBrowserBesideChat({ focus: true, asTabWhenNarrow: true });
  const stream = !panelOpen && view?.active && view.url && ensureDcvMediaShim();
  return (
    <div className="mt-1.5 flex flex-col gap-1.5" data-testid="browser-run-preview">
      <button type="button" onClick={open} className="flex w-fit items-center gap-1.5 text-xs text-[var(--midnight-muted)] hover:text-[var(--midnight)]">
        <Globe size={12} aria-hidden="true" />{label}
      </button>
      {stream && (
        <button type="button" onClick={open} aria-label={`${label}. Open the shared browser`}
          className="relative aspect-video w-72 max-w-full overflow-hidden rounded-md border border-[var(--edge)]">
          <Suspense fallback={null}>
            {/* A picture, not a control: input goes to the panel, where the person can see what they click. */}
            <div inert className="pointer-events-none h-full">
              <LiveView key={view.sessionId ?? view.url ?? ""} signedUrl={view.url as string}
                remoteWidth={view.remoteWidth} remoteHeight={view.remoteHeight} />
            </div>
          </Suspense>
        </button>
      )}
    </div>
  );
}
