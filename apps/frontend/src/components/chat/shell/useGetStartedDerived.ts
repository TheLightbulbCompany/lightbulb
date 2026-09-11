"use client";

import { useMemo } from "react";
import useSWR from "swr";

import { useApi } from "@/lib/api";
import { useRuntimeRpc } from "@/hooks/useRuntimeRpc";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useChannelLinks } from "@/hooks/useChannelLinks";
import { useProvisioningState } from "@/hooks/useProvisioningState";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";

export interface GetStartedDerivedSignals {
  chat: boolean;
  hire: boolean;
  channel: boolean;
  task: boolean;
}

export function useGetStartedDerived(): GetStartedDerivedSignals & { chatReady: boolean } {
  const api = useApi();
  const { read: readTeams } = useTeamsApi();

  // "Get started" guide readiness gate. Gate the floating guide — AND its
  // completion-signal fetches below — on the signal that the chat surface is
  // actually live and interactive, NOT on `useProvisioningState().phase ===
  // "normal"`. That phase is "normal" for ANY 200 from /container/status,
  // which includes the entire cold-start window (status="provisioning",
  // phase="provisioning" / "starting"). Gating on it would mount the
  // launcher + the auto-opened first-run card ON TOP of the
  // ProvisioningStepper's "Provisioning your container…" loader — handing
  // off to surfaces that don't exist yet (empty agents[], hidden composer)
  // and burning the one-time `seen: true` flag before the user can act.
  //
  // The canonical "chat is live" signal is the backend-reported
  // `runtime_phase` reaching "ready" — the exact gate the sibling
  // ProvisioningStepper uses to stop its loader and render the chat
  // `children` (it shares the `/container/status` SWR cache via the same
  // `useProvisioningState` -> `useOnboardingStatus` key, so
  // `container.runtime_phase` here flips to "ready" in the same render the
  // stepper does). It is strictly stronger than `phase === "normal"`: a
  // non-null `container` already implies a 200 ("normal"), and
  // `.runtime_phase === "ready"` additionally requires the gateway pool to
  // have connected.
  const { container } = useProvisioningState();
  // Ready-equivalent for the derived UI state: a parked box is a healthy,
  // completed onboarding — the "chat" step must not un-complete when it
  // sleeps. The sessions.list RPC below can't fire against a parked socket,
  // but useRuntimeRpc nulls its own key while parked (freezing the last
  // cached value), so chatStarted stays correct across a park.
  const chatReady = container?.runtime_phase === "ready" || container?.lifecycle === "parked";

  // `channel` complete = the user has actually connected a channel: ANY
  // provider has at least one bot a member has paired their identity to
  // (`linked: true`), for ANY agent. Channels are created per-agent (keyed by
  // the agent's UUID), so the old `agent_id === "main"` check matched no real
  // bot and the step never ticked once channels moved to the per-agent tab.
  // `linked` (not bare bot-exists) is the truest "connected" signal and mirrors
  // the `emitStep("channel")` producer in BotSetupWizard, which fires on the
  // same pairing success. The hoisted `useChannelLinks` hook owns the
  // `/channels/links/me` read (null-keyed until `chatReady`) and the
  // `.some(linked)` derivation — see its own doc comment for the SWR options.
  const { channelLinked } = useChannelLinks(chatReady);
  // `task` complete = the team already has >= 1 task. We only consume the
  // task-exists boolean, so read just the `all` inbox tab directly (via the
  // same bearer-only `useTeamsApi` hook — safe to call without a Teams
  // provider context) rather than `useInboxData`, which fans out to three
  // reads (mine/recent/all) for surfaces that render the full inbox — two of
  // those are pure waste here.
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
  // `useChannelLinks` options above) so it never fires during the cold-start
  // window.
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
  // `chatReady` via a null method (the gateway pool is connected by then;
  // useRuntimeRpc additionally self-nulls while parked) and
  // low-frequency (dedupingInterval bumped to 5min, overriding the hook's 10s)
  // so it is not an always-on poll; the hook's event-driven revalidation stays
  // throttled. The `emitStep("chat")` event still completes real in-app chats
  // instantly (OR'd in `useGetStarted`), so this read only backfills history.
  const { data: sessionsData } = useRuntimeRpc<
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
    () => ({
      chat: chatStarted,
      hire: hireDone,
      channel: channelLinked,
      task: taskExists,
      chatReady,
    }),
    [chatStarted, hireDone, channelLinked, taskExists, chatReady],
  );

  return getStartedDerived;
}
