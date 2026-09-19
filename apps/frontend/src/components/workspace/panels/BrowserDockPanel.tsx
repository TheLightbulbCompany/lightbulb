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
// Discover every 5s during a known run, and keep watching a discovered session
// until it ends. Retain each signed URL for 240s (AWS expires it at 300s).
// This panel never starts a session or offers take-control: taking control
// destroys the agent's automation session (aws/bedrock-agentcore-sdk-python#518).

import { Suspense, lazy, useEffect, useState } from "react";
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
  loadedAt?: number;
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
  const workspace = useMessagingWorkspaceOptional();
  const runActive = !!workspace?.roomProps?.runs?.some((run) => run.state === "running") ||
    !!workspace?.directoryProps.participants.some((member) => member.agentActivity === "running");

  const [view, setView] = useState<LiveViewResponse | null>(null);

  const ready = host === "agentcore";

  useEffect(() => {
    if (!ready) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      const body = await api.get("/browser/live-view").catch(() => INACTIVE) as LiveViewResponse;
      if (cancelled) return;
      setView((previous) => {
        // Signatures change on every poll. Keep the connection for this session
        // until refresh is due; a different URL path means a different session.
        if (body.active && body.url && previous?.url &&
            new URL(body.url).pathname === new URL(previous.url).pathname &&
            Date.now() - (previous.loadedAt ?? 0) < REMINT_MS) return previous;
        return { ...body, loadedAt: Date.now() };
      });
      if (runActive || body.url) {
        timer = setTimeout(load, POLL_WHILE_RUNNING_MS);
      }
    };
    void load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [ready, api, runActive]);

  if (!ready) return null;
  if (!view) return <Notice>Looking for a live browser session…</Notice>;
  if (!view.active || !view.url) {
    return <Notice>No browser session running — your agents start one when they browse.</Notice>;
  }

  return (
    <Suspense fallback={<Notice>Connecting to the live view…</Notice>}>
      <BrowserLiveView
        key={view.url}
        signedUrl={view.url}
        remoteWidth={view.remoteWidth}
        remoteHeight={view.remoteHeight}
      />
    </Suspense>
  );
}
