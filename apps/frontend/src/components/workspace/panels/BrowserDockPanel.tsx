"use client";

// Watch-only live view of the AgentCore Browser session an agent is driving.
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
// Cadence (design §2g, ruling R14): a browsing turn spawns one or two sessions
// living 4-15 s each, so the panel polls every 5 s WHILE THE ROOM REPORTS AN
// ACTIVE RUN and not at all otherwise — a 240 s poll would catch a session ~6%
// of the time and read empty while the agent was visibly browsing. 240 s is
// only the re-mint bound for a URL already on screen: AWS hard-caps the
// presigned URL at 300 s and the route always mints `now + 300 s`, so a
// constant bound is always inside `expiresAt` without any clock-skew math —
// which is why the response's `expiresAt` is not read here.
//
// The panel never STARTS a session: a browser costs $0.1273/hour and the agent
// could not join one the panel opened. It also offers no take-control
// affordance — take-control destroys the agent's automation session
// (aws/bedrock-agentcore-sdk-python#518).

import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import { useRuntimeHost } from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";

const BrowserLiveView = lazy(() =>
  import("bedrock-agentcore/browser/live-view").then((m) => ({ default: m.BrowserLiveView })),
);

/** The route's constant key set (`GET /api/v1/browser/live-view`). */
interface LiveViewResponse {
  active: boolean;
  url?: string | null;
  remoteWidth?: number;
  remoteHeight?: number;
}

const POLL_WHILE_RUNNING_MS = 5_000;
const REMINT_MS = 240_000;
const INACTIVE: LiveViewResponse = { active: false };

function Notice({ children }: { children: React.ReactNode }): React.ReactElement {
  return (
    <div className="flex h-full items-center justify-center p-6 text-center text-sm text-[var(--midnight-muted)]">
      <p>{children}</p>
    </div>
  );
}

export function BrowserDockPanel(): React.ReactElement | null {
  const api = useApi();
  // One call site, so no `useShownOnAgentCore` twin: `undefined` means no status
  // body has landed for this workspace yet, and firing on a guess is exactly
  // what that hold exists to prevent.
  const host = useRuntimeHost();
  // `roomProps.runs` is `useRooms`' live run state — the `run_active` frames
  // from `useLiveEvents` merged with the `activeRuns` the room detail seeds.
  // Optional because the panel must not throw outside a messaging provider.
  const runs = useMessagingWorkspaceOptional()?.roomProps?.runs;
  const runActive = !!runs?.some((run) => run.state === "running");

  const [view, setView] = useState<LiveViewResponse | null>(null);

  const load = useCallback(() => {
    void api
      .get("/browser/live-view")
      .then((body) => setView(body as LiveViewResponse))
      .catch(() => setView(INACTIVE));
  }, [api]);

  const ready = host === "agentcore";

  useEffect(() => {
    if (ready) load();
  }, [ready, load]);

  // Separate from the mount fetch on purpose: this effect re-runs whenever the
  // cadence changes (a run starting or ending, a URL appearing), and folding
  // the immediate fetch in here would fire an extra request on every such flip.
  const period = runActive ? POLL_WHILE_RUNNING_MS : view?.url ? REMINT_MS : null;
  useEffect(() => {
    if (!ready || period === null) return;
    const timer = setInterval(load, period);
    return () => clearInterval(timer);
  }, [ready, period, load]);

  if (!ready) return null;
  if (!view) return <Notice>Looking for a live browser session…</Notice>;
  if (!view.active || !view.url) {
    return <Notice>No browser session running — your agents start one when they browse.</Notice>;
  }

  return (
    <Suspense fallback={<Notice>Connecting to the live view…</Notice>}>
      <BrowserLiveView
        signedUrl={view.url}
        remoteWidth={view.remoteWidth}
        remoteHeight={view.remoteHeight}
      />
    </Suspense>
  );
}
