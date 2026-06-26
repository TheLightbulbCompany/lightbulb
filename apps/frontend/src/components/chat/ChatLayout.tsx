"use client";

import "./ChatLayout.css";
import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { useAuth, useUser, UserButton } from "@clerk/nextjs";

import { capture } from "@/lib/analytics";
import { useRouter, useSearchParams } from "next/navigation";
import { Settings, Plus, CheckCircle, CreditCard, Menu, X, FolderOpen, Pencil, Trash2, HelpCircle, ListTodo, SlidersHorizontal, Pause, Play } from "lucide-react";
import Link from "next/link";

import { ProvisioningStepper } from "@/components/chat/ProvisioningStepper";
import { GallerySection } from "@/components/chat/GallerySection";
import { HealthIndicator } from "@/components/chat/HealthIndicator";
import { TrialBanner } from "@/components/chat/TrialBanner";
import { OutOfCreditsBanner } from "@/components/chat/OutOfCreditsBanner";
import { useGateway } from "@/hooks/useGateway";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { buildModelNameMap, displayModelName } from "@/lib/modelDisplay";
import { ExecApprovalsProvider } from "@/hooks/useExecApprovals";
import { useApi } from "@/lib/api";
import { useAgents, getAgentModelString, agentDisplayName, type Agent } from "@/hooks/useAgents";
import { useBilling } from "@/hooks/useBilling";
import { useOnboardingGate } from "@/hooks/useOnboardingGate";
import { CompanyNav } from "@/components/chat/CompanyNav";
import { FileViewer } from "@/components/chat/FileViewer";
import { Logo } from "@/components/Logo";
import { useSupport } from "@/components/support/SupportProvider";
import { AgentCreateDialog, AgentRenameDialog, AgentDeleteDialog } from "@/components/chat/AgentDialogs";
import { Monogram, StatusDot } from "@/components/agents/AgentIdentity";
import { fromInstalledAgent } from "@/lib/agentDesign";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { myListings } from "@/lib/marketplace/api";
import { DesktopUpdater } from "@/components/desktop/DesktopUpdater";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { emitStep } from "@/components/getstarted/getStartedEvents";
import { GetStarted } from "@/components/getstarted/GetStarted";
import { useProvisioningState } from "@/hooks/useProvisioningState";
import { TEAMS_PANEL_PREFIX } from "@/components/chat/InlineTeamsPanel";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";

/** Minimal shape of `/channels/links/me` — only what the guide's
 *  `channel`-complete derivation reads. A bot is keyed by its agent's UUID
 *  (channels are created per-agent, not on a fixed "main" agent); `linked`
 *  flips true once a member has paired their identity to the bot. */
interface ChannelBotEntry {
  agent_id: string;
  linked: boolean;
}
interface ChannelLinksResponse {
  telegram: ChannelBotEntry[];
  discord: ChannelBotEntry[];
  slack: ChannelBotEntry[];
  can_create_bots: boolean;
}

interface ChatLayoutProps {
  children: React.ReactNode;
  activePanel?: string;
  onPanelChange?: (panel: string) => void;
  openAgentIssues?: (agentId: string) => void;
  openAgentInspector?: (agentId: string) => void;
  onClearPanel?: () => void;
  onNewTask?: () => void;
  fileViewerOpen?: boolean;
  activeFilePath?: string | null;
  onOpenFile?: (path: string) => void;
  onCloseFileViewer?: () => void;
}

function dispatchSelectAgentEvent(agentId: string): void {
  window.dispatchEvent(
    new CustomEvent("selectAgent", { detail: { agentId } }),
  );
}

export function ChatLayout({
  children,
  activePanel,
  onPanelChange,
  openAgentIssues,
  openAgentInspector,
  onClearPanel,
  onNewTask,
  fileViewerOpen,
  activeFilePath,
  onOpenFile,
  onCloseFileViewer,
}: ChatLayoutProps): React.ReactElement {
  // Block the whole chat shell until Clerk hydration + onboarding state are
  // settled AND any pending org auto-activation has landed. This is the
  // hinge for the personal/org race fix: if we render ProvisioningStepper
  // (or any owner_id-aware hook) before the JWT has finished flipping into
  // its final context, it fires POST /container/provision and /billing
  // reads with a stale JWT and we end up with orphan personal rows for a
  // user who was always meant to be an org member. The state machine +
  // the router.replace / setActive side effects live in the hook.
  const onboardingPhase = useOnboardingGate();
  const { isSignedIn, getToken } = useAuth();
  const { user } = useUser();
  const router = useRouter();
  const api = useApi();
  const { agents, defaultId, createAgent, deleteAgent, updateAgent } = useAgents();
  const { refresh: refreshBilling, account, isSubscribed } = useBilling();
  const { nodeConnected } = useGateway();
  // Models catalog for the sidebar agent rows: resolves Bedrock
  // application-inference-profile ARNs to display names ("Claude Opus 4.6")
  // — the raw ARN's last segment is an opaque random suffix.
  const { data: configSnapshot } = useGatewayRpc<{ config?: unknown }>("config.get");
  const modelNames = useMemo(() => buildModelNameMap(configSnapshot?.config), [configSnapshot?.config]);
  const searchParams = useSearchParams();

  const [userSelectedId, setUserSelectedId] = useState<string | null>(null);
  // Stripe Checkout returns either ?subscription=success (legacy) or
  // ?checkout=success (new trial-checkout flow). Both should trigger the
  // billing-refresh + URL-cleanup effect.
  //
  // Two separate state slices keyed off the same URL param so the banner
  // and the polling lifecycle are independent — banner auto-dismisses at
  // 5s, polling continues for up to 30s. Without this split the
  // banner-dismiss tear-down clobbers the poll interval.
  const cameFromCheckout =
    searchParams.get("subscription") === "success" ||
    searchParams.get("checkout") === "success";
  const [showSubscriptionSuccess, setShowSubscriptionSuccess] = useState(() => cameFromCheckout);
  const [pollingForWebhook, setPollingForWebhook] = useState(() => cameFromCheckout);
  const [recoveryTriggered, setRecoveryTriggered] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { openSupport } = useSupport();
  const [createFormOpen, setCreateFormOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<Agent | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Agent | null>(null);

  // Derive effective agent: user selection > default > first agent
  const currentAgentId = userSelectedId ?? defaultId ?? agents[0]?.id ?? null;

  // Header agent-action cluster (next to the file-tree button): pause/resume +
  // inspector/work deep links for the active agent, so the user can act on the
  // agent they're chatting with without opening the full inspector.
  //
  // The gateway `agents.list` (above) carries no run state, so the paused flag
  // comes from the Teams BFF roster (`/teams/agents`), keyed by the same agent
  // UUID. `useAgentMutations.pause/resume` revalidate that same `/teams/agents`
  // key, so toggling here reflects back into the icon without a manual refetch.
  // `useTeamsApi` is bearer-only (no Teams provider context) so it's safe to
  // call from the chat shell. Best-effort — a roster fetch failure just leaves
  // the agent un-paused-looking; the toggle still works.
  const { read: readTeams } = useTeamsApi();
  const { data: teamsAgentsData } = readTeams<{ agents?: { id: string; status?: string | null }[] }>(
    "/agents",
  );
  const { pause: pauseAgent, resume: resumeAgent } = useAgentMutations();
  const currentTeamsAgent = teamsAgentsData?.agents?.find((a) => a.id === currentAgentId) ?? null;
  const currentAgentPaused = currentTeamsAgent?.status === "paused";
  const [agentActionBusy, setAgentActionBusy] = useState(false);

  async function handleTogglePauseResume(): Promise<void> {
    if (!currentAgentId || agentActionBusy) return;
    setAgentActionBusy(true);
    try {
      if (currentAgentPaused) {
        await resumeAgent(currentAgentId);
      } else {
        await pauseAgent(currentAgentId);
      }
    } finally {
      setAgentActionBusy(false);
    }
  }

  // Per-mount latch so the /users/sync effect (which re-runs on every render
  // because `api` identity churns) fires the sync at most once. See the effect
  // below for the full rationale.
  const syncStartedRef = useRef(false);

  const subscriptionStatus = account?.subscription_status ?? null;
  const userName = user?.fullName || user?.firstName || "User";
  const userInitials = userName
    .split(" ")
    .map((n: string) => n[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  // /users/sync waits for the gate to be ``ready``. Firing sync with a
  // pre-activation JWT used to create phantom personal billing rows — the
  // backend no longer does that write, but we gate here too so other
  // context-sensitive endpoints called from sync paths (future-proofing)
  // don't misresolve owner_id either.
  //
  // Synchronous in-flight latch (`syncStartedRef`): this effect depends on
  // `api`, which `useApi()` returns as a fresh object every render, so it
  // re-runs on every re-render — and ChatLayout re-renders frequently while
  // data hooks settle (e.g. `useProvisioningState` polling `/container/status`).
  // The sessionStorage guard below is only written *after* the async
  // `syncUser()` resolves, so during that in-flight window the re-render
  // burst would re-fire `syncUser` several times (a real over-fetch, since
  // sync is a network POST). The ref latches synchronously before the call
  // so the effect fires sync at most once per mount; sessionStorage handles
  // the cross-mount / cross-session dedupe. On failure we release the latch
  // (sessionStorage is only written on success) so a genuine error can retry.
  useEffect(() => {
    if (!isSignedIn) return;
    if (onboardingPhase !== "ready") return;
    if (syncStartedRef.current) return;
    // Once per browser session: sync is an idempotent Clerk→DDB upsert and
    // ChatLayout remounts on every view switch / page load — re-firing it
    // each time is pure backend load.
    const syncedKey = "isol8:user-synced";
    try {
      if (sessionStorage.getItem(syncedKey)) return;
    } catch {
      // sessionStorage unavailable (privacy mode) — fall through and sync.
    }
    syncStartedRef.current = true;
    api
      .syncUser()
      .then(() => {
        try {
          sessionStorage.setItem(syncedKey, "1");
        } catch {
          // best effort
        }
      })
      .catch((err: unknown) => {
        // Release the latch so the next render can retry — the session
        // guard is only set on success, so a failed sync must stay retryable.
        syncStartedRef.current = false;
        console.error("User sync failed:", err);
      });
  }, [isSignedIn, api, onboardingPhase]);

  // Dispatch DOM event so page.tsx picks up the current agent (external system sync)
  const lastDispatchedRef = useRef<string | null>(null);
  useEffect(() => {
    if (currentAgentId && currentAgentId !== lastDispatchedRef.current) {
      lastDispatchedRef.current = currentAgentId;
      dispatchSelectAgentEvent(currentAgentId);
    }
  }, [currentAgentId]);

  // Post-checkout confirmation: refresh billing + clean URL.
  // Strip ONLY ?checkout / ?subscription, preserving ?provider= so the
  // ProvisioningStepper can still render the provider-specific step
  // (ChatGPTOAuthStep / ByoKeyStep / CreditsStep) — without this, the
  // OAuth + BYO paths break because the wizard never gets to the step
  // that collects the user's credentials.
  useEffect(() => {
    if (!showSubscriptionSuccess) return;

    refreshBilling();
    const params = new URLSearchParams(searchParams.toString());
    params.delete("checkout");
    params.delete("subscription");
    const remaining = params.toString();
    router.replace(`/chat${remaining ? `?${remaining}` : ""}`, { scroll: false });

    const dismissTimer = setTimeout(() => setShowSubscriptionSuccess(false), 5000);
    return () => clearTimeout(dismissTimer);
  }, [showSubscriptionSuccess, refreshBilling, router, searchParams]);

  // Post-credit-checkout return. Stripe sends users back with
  // ?credits=success (or =cancel) when they finish the credit top-up
  // flow. We just strip the param — the credit balance refresh happens
  // automatically via useCredits' refreshInterval/onFocus, and the
  // ProvisioningStepper re-evaluates needsProviderStep on URL change so
  // the wizard advances out of CreditsStep on success.
  const cameFromCreditsCheckout = searchParams.get("credits") !== null;
  useEffect(() => {
    if (!cameFromCreditsCheckout) return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete("credits");
    const remaining = params.toString();
    router.replace(`/chat${remaining ? `?${remaining}` : ""}`, { scroll: false });
  }, [cameFromCreditsCheckout, router, searchParams]);

  // Race against Stripe webhook delivery: the user returns from Checkout
  // before customer.subscription.created lands at our webhook endpoint
  // (typically <2s, but variable). Without polling, the wizard would
  // bounce back to the ProviderPicker because is_subscribed stays False
  // until the webhook persists subscription_status. Poll every 2s for up
  // to 30s — once is_subscribed flips True we stop early, otherwise we
  // give up so we don't poll forever.
  //
  // Keyed on `pollingForWebhook` (independent of the banner state) so the
  // banner's 5s auto-dismiss doesn't cancel the longer poll loop.
  useEffect(() => {
    if (!pollingForWebhook || isSubscribed) return;
    const pollInterval = setInterval(() => refreshBilling(), 2000);
    const giveUpTimer = setTimeout(() => setPollingForWebhook(false), 30_000);
    return () => {
      clearInterval(pollInterval);
      clearTimeout(giveUpTimer);
    };
  }, [pollingForWebhook, isSubscribed, refreshBilling]);

  // Marketplace drawer: when a hire completes, the provider sets
  // `freshlyInstalledAgentId` so the matching sidebar row plays the
  // `slideIn` animation. Clear the flag after the animation has had time
  // to finish so the class isn't reapplied on subsequent renders.
  const { state: drawerState, setFreshlyInstalled, open: openMarketplace } = useMarketplaceDrawer();

  useEffect(() => {
    if (!drawerState.freshlyInstalledAgentId) return;
    emitStep("hire");
    const handle = setTimeout(() => setFreshlyInstalled(null), 600);
    return () => clearTimeout(handle);
  }, [drawerState.freshlyInstalledAgentId, setFreshlyInstalled]);

  // Fetch the seller's listings so we can render a gold pub-dot on the
  // sidebar avatar of agents the user has published. Cross-references
  // listing.manifest_json.agent_id (source agent at publish time) with the
  // current installed agent set. Best-effort — failure drops the badge
  // silently rather than breaking the chat shell.
  // SWR (vs. a mount effect) so the call dedupes across ChatLayout
  // remounts and never re-fires on Clerk's getToken identity churn — the
  // effect form refetched on every auth-state render. Best-effort: errors
  // drop the badge silently (no retry; the badge isn't load-bearing).
  const { data: sellerListings } = useSWR(
    user?.id ? ["seller-listings", user.id] : null,
    async ([, sellerId]: [string, string]) => {
      const jwt = await getToken();
      if (!jwt) return { items: [] };
      return myListings({ sellerId, jwt });
    },
    { revalidateOnFocus: false, dedupingInterval: 300_000, shouldRetryOnError: false },
  );
  const publishedAgentIds = useMemo(() => {
    const ids = new Set<string>();
    for (const listing of sellerListings?.items ?? []) {
      const sourceAgentId = (listing as unknown as { manifest_json?: { agent_id?: string } })
        .manifest_json?.agent_id;
      if (sourceAgentId) ids.add(sourceAgentId);
    }
    return ids;
  }, [sellerListings]);

  // "Get started" guide. Gate the floating guide — AND its completion-signal
  // fetches — on the signal that the chat surface is actually live and
  // interactive, NOT on `useProvisioningState().phase === "normal"`. That
  // phase is "normal" for ANY 200 from /container/status, which includes the
  // entire cold-start window (status="provisioning", phase="provisioning" /
  // "starting"). Gating on it would mount the launcher + the auto-opened
  // first-run card ON TOP of the ProvisioningStepper's "Provisioning your
  // container…" loader — handing off to surfaces that don't exist yet (empty
  // agents[], hidden composer) and burning the one-time `seen: true` flag
  // before the user can act.
  //
  // The canonical "chat is live" signal is the backend-reported cold-start
  // phase reaching "ready" — the exact gate the sibling ProvisioningStepper
  // uses to stop its loader and render the chat `children` (it shares the
  // `/container/status` SWR cache via the same `useProvisioningState` key, so
  // `container.phase` here flips to "ready" in the same render the stepper
  // does). It is strictly stronger than `phase === "normal"`: a non-null
  // `container` already implies a 200 ("normal"), and `.phase === "ready"`
  // additionally requires the gateway pool to have connected.
  const { container } = useProvisioningState();
  const chatReady = container?.phase === "ready";
  // `channel` complete = the user has actually connected a channel: ANY
  // provider has at least one bot a member has paired their identity to
  // (`linked: true`), for ANY agent. Channels are created per-agent (keyed by
  // the agent's UUID), so the old `agent_id === "main"` check matched no real
  // bot and the step never ticked once channels moved to the per-agent tab.
  // `linked` (not bare bot-exists) is the truest "connected" signal and mirrors
  // the `emitStep("channel")` producer in BotSetupWizard, which fires on the
  // same pairing success. Null SWR key until `chatReady` (SWR's canonical
  // conditional-fetch gate) so `/channels/links/me` never fires during the
  // cold-start window.
  const { data: channelLinks } = useSWR<ChannelLinksResponse>(
    chatReady ? "/channels/links/me" : null,
    () => api.get("/channels/links/me") as Promise<ChannelLinksResponse>,
    // Channel-link state changes rarely (only on a bot pairing) and feeds
    // a single boolean — don't revalidate on every window focus. Mirrors
    // the sibling seller-listings read.
    { revalidateOnFocus: false, dedupingInterval: 300_000 },
  );
  const channelLinked =
    !!channelLinks &&
    (["telegram", "discord", "slack"] as const).some((p) =>
      channelLinks[p]?.some((b) => b.linked),
    );
  // `task` complete = the team already has >= 1 task. We only consume the
  // task-exists boolean, so read just the `all` inbox tab directly (via the
  // same bearer-only `useTeamsApi` the roster fetch above uses) rather than
  // `useInboxData`, which fans out to three reads (mine/recent/all) for
  // surfaces that render the full inbox — two of those are pure waste here.
  // The key is `useInboxData`'s exact `all` key, so this shares SWR's cache
  // with the inbox panel when it's open (no duplicate fetch). Gated on
  // `chatReady` (null key) so it never fires during the cold-start window.
  // Residual: a legacy pre-Paperclip owner whose container is live but whose
  // Teams workspace was never provisioned can still see this read error-retry
  // once `chatReady` is true. We do NOT add a `useTeamsWorkspaceStatus` gate
  // here because that hook has no `enabled` param and would itself fire an
  // always-on `/teams/dashboard` poll during the provisioning window this
  // fix is meant to keep quiet.
  const { data: allInbox } = readTeams<{ items?: unknown[] } | unknown[]>(
    chatReady ? teamsQueryKeys.inbox.list("all", {}) : null,
  );
  const taskExists = Array.isArray(allInbox)
    ? allInbox.length > 0
    : (allInbox?.items?.length ?? 0) > 0;
  // `hire` complete = the user has at least one marketplace-installed
  // specialist. The truest persistent signal is the set of `.template`
  // sidecars EFS writes on every marketplace deploy (catalog_service), which
  // `/marketplace/me/installed-templates` enumerates. It mirrors the
  // `emitStep("hire")` producer (a fresh install) but survives reloads, so a
  // user who hired BEFORE the guide shipped still sees the step ticked. The
  // gateway roster (`agents.list`) carries NO installed-from-marketplace
  // marker — `agents.update`'s schema is strict, so no source/listing field
  // survives a deploy — and a "count past Friday + Atlas" heuristic can't tell
  // a hired specialist from a user-created agent (Atlas isn't even reliably
  // identifiable in the gateway roster), so the sidecar list is the only sound
  // source. Null SWR key until `chatReady`, low-frequency (mirrors the
  // seller-listings fetch) so it never fires during the cold-start window.
  const { data: installedTemplates } = useSWR<{ installed: { agent_id: string }[] }>(
    chatReady ? "/marketplace/me/installed-templates" : null,
    () =>
      api.get("/marketplace/me/installed-templates") as Promise<{
        installed: { agent_id: string }[];
      }>,
    { revalidateOnFocus: false, dedupingInterval: 300_000 },
  );
  const hireDone = (installedTemplates?.installed?.length ?? 0) > 0;
  // `chat` complete = the user has ever held a REAL conversation. OpenClaw
  // creates a session the first time a user actually chats, and `sessions.list`
  // enumerates them — the cheap persistent read that backfills the step for
  // users who chatted before the guide shipped (the in-app `emitStep("chat")`
  // producer only covers chats sent while the guide is mounted).
  //
  // Two guards keep this from false-positiving on a brand-new container, where
  // a bare session-exists count would pre-tick the step before the user types:
  //   - `includeUnknown: false` (plus `includeGlobal: false`): the
  //     Paperclip/openclaw adapter handshake (Friday/Atlas connecting) can
  //     create a non-global "unknown" session at provision time, before any
  //     user message. Counting those, or the implicit `global` session, would
  //     tick "chat done" on first load. Only genuine agent sessions count.
  //   - require `totalTokens > 0` on at least one session: even a genuine
  //     session can be opened empty (handshake / focus) with no model
  //     round-trip, so existence alone still mis-fires. A token-bearing
  //     session is the truest "a real chat happened" signal.
  // The newest session may itself be a zero-token shell, so fetch a small
  // bounded window (not `limit: 1`) to be sure a token-bearing one is visible —
  // still cheap (no last-message / derived-title enrichment). Gated on
  // `chatReady` via a null method (the gateway pool is connected by then) and
  // low-frequency (dedupingInterval bumped to 5min, overriding the hook's 10s)
  // so it is not an always-on poll; the hook's event-driven revalidation stays
  // throttled. The `emitStep("chat")` event still completes real in-app chats
  // instantly (OR'd in `useGetStarted`), so this read only backfills history.
  const { data: sessionsData } = useGatewayRpc<
    { sessions?: { totalTokens?: number }[] } | { totalTokens?: number }[]
  >(
    chatReady ? "sessions.list" : null,
    { includeGlobal: false, includeUnknown: false, limit: 25 },
    { dedupingInterval: 300_000 },
  );
  const chatSessions = Array.isArray(sessionsData)
    ? sessionsData
    : sessionsData?.sessions ?? [];
  const chatStarted = chatSessions.some((s) => (s.totalTokens ?? 0) > 0);
  const getStartedDerived = useMemo(
    () => ({ chat: chatStarted, hire: hireDone, channel: channelLinked, task: taskExists }),
    [chatStarted, hireDone, channelLinked, taskExists],
  );

  function handleSelectAgent(agentId: string): void {
    capture("agent_selected", { agent_id: agentId });
    setUserSelectedId(agentId);
    dispatchSelectAgentEvent(agentId);
    // There's no tab-switcher: the view is derived from ?panel. Selecting an
    // agent must drop ?panel so the content area falls back to chat (with the
    // just-selected agent).
    onClearPanel?.();
    setSidebarOpen(false);
  }

  async function handleCreateAgent(): Promise<void> {
    setCreateFormOpen(true);
  }

  // Block the chat shell until the gate is ``ready``. See
  // ``useOnboardingGate`` for the full state-machine; the rule is the
  // same as before — anything other than ``ready`` means we're either
  // still loading, mid-redirect to /onboarding, or auto-activating an org
  // membership, none of which should let owner_id-aware hooks render.
  if (onboardingPhase !== "ready") {
    return (
      <div className="app-shell" style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
        <div style={{ color: "#6b6b6b", fontSize: 14 }}>Loading…</div>
      </div>
    );
  }

  return (
    <ExecApprovalsProvider>
      <div className={`app-shell${fileViewerOpen ? " with-file-viewer" : ""}`}>
        <div className={`sidebar-backdrop${sidebarOpen ? " visible" : ""}`} onClick={() => setSidebarOpen(false)} />
        <div className={`cream-sidebar${sidebarOpen ? " mobile-open" : ""}`}>
          {/* Header */}
          <div className="sidebar-header">
            <div className="sidebar-logo">
              <Logo size="sm" href={null} />
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <Link href="/settings" className="sidebar-settings-link">
                <Settings size={18} />
              </Link>
              <button className="mobile-hamburger" onClick={() => setSidebarOpen(false)} aria-label="Close menu" style={{ display: sidebarOpen ? "flex" : undefined }}>
                <X size={18} />
              </button>
            </div>
          </div>

          {/* Health Indicator — hidden until the user has a subscription. The
              indicator reports the state of an existing per-user container; if
              the user hasn't subscribed there's no container to indicate, and
              "Starting..." is misleading. */}
          {isSubscribed && (
            <HealthIndicator onRecoveryReprovision={() => setRecoveryTriggered(true)} />
          )}

          {/* Node Status */}
          {nodeConnected && (
            <div style={{
              padding: '4px 12px',
              fontSize: '12px',
              color: '#16a34a',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}>
              <span style={{
                width: '6px',
                height: '6px',
                borderRadius: '50%',
                backgroundColor: '#16a34a',
                display: 'inline-block',
              }} />
              Local tools available
            </div>
          )}

          {/* ONE sidebar body regardless of view: the roster + CompanyNav cover
              both chat and control. The view is derived from ?panel (selecting
              an agent shows chat; a Company/System panel shows that panel), so
              there's no Chat|Control tab-switcher. */}
          {/* Co-equal create pill: the two central creation actions — New
              agent and New task — as equal halves of one forest control.
              New agent adds to the roster directly; New task routes to the
              Tasks panel and auto-opens its dialog (chat/page → onNewTask). */}
          <div className="create-pill" role="group" aria-label="Create">
            <button
              type="button"
              className="create-pill__seg"
              onClick={handleCreateAgent}
              aria-label="New agent"
            >
              <Plus size={13} />
              Agent
            </button>
            <span className="create-pill__divider" aria-hidden="true" />
            <button
              type="button"
              className="create-pill__seg"
              onClick={() => onNewTask?.()}
              aria-label="New task"
            >
              <Plus size={13} />
              Task
            </button>
          </div>

              {/* Agent List + CompanyNav + Gallery share a single
                  `.sidebar-scroll` region so the roster keeps its natural
                  height instead of collapsing under CompanyNav's own scroll. */}
              <div className="sidebar-scroll">
              <div className="agent-list">
                <div className="agent-section-label">
                  <span>My Agents</span>
                  <b>{agents.length}</b>
                </div>
                {agents.map((agent) => {
                  const isDefault = agent.id === defaultId;
                  const isNewArrival = drawerState.freshlyInstalledAgentId === agent.id;
                  const displayAgent = fromInstalledAgent(agent);
                  return (
                    <div
                      key={agent.id}
                      className={`agent-item${currentAgentId === agent.id ? " active" : ""}${isNewArrival ? " new-arrival" : ""}`}
                      onClick={() => handleSelectAgent(agent.id)}
                    >
                      <span className="relative inline-flex">
                        <Monogram agent={displayAgent} size={28} radius={8} />
                        {publishedAgentIds.has(agent.id) && (
                          <span
                            className="absolute top-0 right-0 w-2 h-2 rounded-full bg-[#c2a378] border-2 border-[#faf7f2]"
                            aria-label="Published to marketplace"
                            title="Published to marketplace"
                          />
                        )}
                      </span>
                      <div className="agent-info">
                        <div className="agent-name">{agentDisplayName(agent)}</div>
                        {(() => {
                          // `agent.model` can be a string OR a
                          // `{primary, fallbacks}` object (OpenClaw 4.5
                          // returns the structured shape from agents.list).
                          // Direct `.split` calls here used to crash the
                          // chat UI with `TypeError: e.model.split is not a
                          // function` after sign-in.
                          const modelStr = getAgentModelString(agent);
                          const display = displayModelName(modelStr, modelNames);
                          if (!display) return null;
                          return <div className="agent-model">{display}</div>;
                        })()}
                      </div>
                      <div className="agent-row-actions">
                        {/* "See her work →": deep-link into the inline Issues
                            panel filtered to this agent. */}
                        <button
                          type="button"
                          className="agent-row-action"
                          aria-label={`See ${agentDisplayName(agent)}'s work`}
                          title={`See ${agentDisplayName(agent)}'s work`}
                          onClick={(e) => { e.stopPropagation(); openAgentIssues?.(agent.id); }}
                        >
                          <ListTodo size={13} />
                        </button>
                        {/* "Inspect agent": open the per-agent inspector shell
                            (Overview / Tools / Channels). */}
                        <button
                          type="button"
                          className="agent-row-action"
                          aria-label={`Inspect ${agentDisplayName(agent)}`}
                          title={`Inspect ${agentDisplayName(agent)}`}
                          onClick={(e) => { e.stopPropagation(); openAgentInspector?.(agent.id); }}
                        >
                          <SlidersHorizontal size={13} />
                        </button>
                        <button
                          type="button"
                          className="agent-row-action"
                          aria-label={`Rename ${agentDisplayName(agent)}`}
                          onClick={(e) => { e.stopPropagation(); setRenameTarget(agent); }}
                        >
                          <Pencil size={13} />
                        </button>
                        {!isDefault && (
                          <button
                            type="button"
                            className="agent-row-action agent-row-action--danger"
                            aria-label={`Delete ${agentDisplayName(agent)}`}
                            onClick={(e) => { e.stopPropagation(); setDeleteTarget(agent); }}
                          >
                            <Trash2 size={13} />
                          </button>
                        )}
                      </div>
                      <StatusDot status="active" />
                    </div>
                  );
                })}
              </div>
              {/* CompanyNav (Dashboard / Inbox / Work / Company) plus the
                  marketplace block (GallerySection) — Browse + Publish buttons
                  that open the MarketplaceDrawer. The drawer (mounted on the
                  chat page) owns the install + auto-select flow now. */}
              <CompanyNav activePanel={activePanel} onPanelChange={onPanelChange} />
              <GallerySection />
          </div>

          {/* Footer */}
          <div className="sidebar-footer">
            <div className="user-row">
              <div className="user-avatar">{userInitials}</div>
              <div className="user-info">
                <div className="user-name">{userName}</div>
              </div>
              {subscriptionStatus && <span className="plan-badge">{subscriptionStatus}</span>}
            </div>
            <DesktopUpdater version="0.1" />
          </div>
        </div>

        <div className="main-area" style={{ gridArea: "main" }}>
          <div className="main-header">
            <button className="mobile-hamburger" onClick={() => setSidebarOpen(true)} aria-label="Open menu">
              <Menu size={22} />
            </button>
            {onOpenFile && (
              <button
                onClick={() => { capture("file_browser_opened"); onOpenFile?.(""); }}
                className="flex items-center justify-center text-[#8a8578] hover:text-[#1a1a1a] transition-colors p-1"
                title="Browse workspace files"
              >
                <FolderOpen size={18} />
              </button>
            )}
            {/* Active-agent quick actions — inspector / work / pause-resume —
                so the user can act on the agent they're chatting with without
                opening the full inspector. Gated on an active agent (matches
                the file-tree button's simple presence gate). Destructive /
                structural actions (Delete, Reports-to) stay in the inspector. */}
            {currentAgentId && (
              <div className="flex items-center gap-1" aria-label="Agent actions">
                <button
                  type="button"
                  onClick={() => openAgentInspector?.(currentAgentId)}
                  className="flex items-center justify-center text-[#8a8578] hover:text-[#1a1a1a] transition-colors p-1 rounded focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--forest)]"
                  title="Open agent inspector"
                  aria-label="Open agent inspector"
                >
                  <SlidersHorizontal size={18} />
                </button>
                <button
                  type="button"
                  onClick={() => openAgentIssues?.(currentAgentId)}
                  className="flex items-center justify-center text-[#8a8578] hover:text-[#1a1a1a] transition-colors p-1 rounded focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--forest)]"
                  title="See this agent's work"
                  aria-label="See this agent's work"
                >
                  <ListTodo size={18} />
                </button>
                <button
                  type="button"
                  onClick={() => { void handleTogglePauseResume(); }}
                  disabled={agentActionBusy}
                  className="flex items-center justify-center text-[#8a8578] hover:text-[#1a1a1a] transition-colors p-1 rounded focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--forest)] disabled:opacity-50"
                  title={currentAgentPaused ? "Resume agent" : "Pause agent"}
                  aria-label={currentAgentPaused ? "Resume agent" : "Pause agent"}
                >
                  {currentAgentPaused ? <Play size={18} /> : <Pause size={18} />}
                </button>
              </div>
            )}
            <div style={{ flex: 1 }} />
            <button
              onClick={() => openSupport()}
              className="flex items-center justify-center text-[#8a8578] hover:text-[#1a1a1a] transition-colors p-1 mr-1"
              title="Get help / report a problem"
              aria-label="Get help"
            >
              <HelpCircle size={18} />
            </button>
            <UserButton
              appearance={{
                elements: {
                  avatarBox: "h-8 w-8",
                },
              }}
            >
              <UserButton.MenuItems>
                <UserButton.Link label="Settings" labelIcon={<CreditCard className="h-4 w-4" />} href="/settings" />
              </UserButton.MenuItems>
            </UserButton>
          </div>

          <div className="main-content">
            <TrialBanner />
            <OutOfCreditsBanner />
            {showSubscriptionSuccess && (
              <div className="subscription-banner">
                <CheckCircle size={16} />
                <p>Subscription confirmed! Your agent is being upgraded.</p>
              </div>
            )}
            {recoveryTriggered ? (
              <ProvisioningStepper trigger="recovery">{children}</ProvisioningStepper>
            ) : (
              <ProvisioningStepper>{children}</ProvisioningStepper>
            )}
          </div>

          {chatReady && (
            <GetStarted
              derived={getStartedDerived}
              actions={{
                chat: () => {
                  // Don't select/act on an empty id: if no real agent exists
                  // yet, bail rather than passing "" to handleSelectAgent.
                  const targetId = defaultId ?? agents[0]?.id;
                  if (!targetId) return;
                  handleSelectAgent(targetId);
                  window.dispatchEvent(new CustomEvent("getstarted:focus-composer"));
                },
                task: () => {
                  // ONE navigation to the inbox panel with the New-task
                  // intent. Mirror what `setActivePanel` (page.tsx) would
                  // build — set panel + view=control, drop stale agent/detailId
                  // filters — and add `newIssue=1` so the inbox auto-opens its
                  // New-task dialog (read by InboxPage). Calling `onPanelChange`
                  // and then a second relative `?panel=…` push grew two history
                  // entries (Back needed two presses) and the relative form
                  // dropped the other query params setActivePanel preserves.
                  const params = new URLSearchParams(searchParams.toString());
                  params.set("panel", `${TEAMS_PANEL_PREFIX}inbox`);
                  params.set("view", "control");
                  params.delete("agent");
                  params.delete("detailId");
                  params.set("newIssue", "1");
                  router.push(`/chat?${params.toString()}`, { scroll: false });
                },
                hire: () => openMarketplace({ view: "browse" }),
                channel: () => {
                  // Route to a surface where a fresh user can actually CREATE
                  // their first bot: the default agent's inspector Channels tab
                  // (AgentChannelsSection). The old /settings?panel=channels
                  // (MyChannelsSection) is link-only and dead-ends a zero-bot
                  // user. ?inspectorTab=channels is read by AgentInspectorPanel
                  // to open straight on that tab.
                  // Bail without navigating if there's no real agent to target
                  // rather than pushing ?agent= with an empty id.
                  const agentId = defaultId ?? agents[0]?.id;
                  if (!agentId) return;
                  const params = new URLSearchParams(searchParams.toString());
                  params.set("panel", "agent-inspector");
                  params.set("agent", agentId);
                  params.set("view", "control");
                  params.set("inspectorTab", "channels");
                  router.push(`/chat?${params.toString()}`, { scroll: false });
                },
              }}
            />
          )}
        </div>

        {fileViewerOpen && (
          <FileViewer
            agentId={currentAgentId}
            initialFilePath={activeFilePath}
            onClose={() => onCloseFileViewer?.()}
          />
        )}

        {/* Each dialog is keyed on its open/target so it remounts (and
            re-initializes its internal state) on each open, instead of
            using useEffect to reset on close — see AgentDialogs.tsx. */}
        <AgentCreateDialog
          key={createFormOpen ? "create-open" : "create-closed"}
          open={createFormOpen}
          // Names only — the dup check is a name-space concern. The UUID id
          // is the identity (used for workspace/RPC targeting), never a name,
          // so an unnamed agent contributes nothing to collide against.
          existingNames={agents
            .map((a) => a.identity?.name || a.name)
            .filter((n): n is string => !!n)}
          onCancel={() => setCreateFormOpen(false)}
          onCreate={async (name) => {
            // OpenClaw's agents.create requires a non-empty `workspace` —
            // see useAgents.createAgent, which fills in the default path
            // (.openclaw/workspaces/{id}) when omitted.
            await createAgent({ name });
            setCreateFormOpen(false);
          }}
        />

        <AgentRenameDialog
          key={`rename-${renameTarget?.id ?? "closed"}`}
          agent={renameTarget}
          onCancel={() => setRenameTarget(null)}
          onRename={async (name) => {
            if (!renameTarget) return;
            await updateAgent(renameTarget.id, { name });
            setRenameTarget(null);
          }}
        />

        <AgentDeleteDialog
          key={`delete-${deleteTarget?.id ?? "closed"}`}
          agent={deleteTarget}
          onCancel={() => setDeleteTarget(null)}
          onDelete={async () => {
            if (!deleteTarget) return;
            await deleteAgent(deleteTarget.id);
            if (userSelectedId === deleteTarget.id) setUserSelectedId(null);
            setDeleteTarget(null);
          }}
        />
      </div>
    </ExecApprovalsProvider>
  );
}
