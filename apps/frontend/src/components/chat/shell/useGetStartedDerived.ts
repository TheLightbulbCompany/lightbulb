"use client";

import { useMemo } from "react";
import useSWR from "swr";

import { useApi } from "@/lib/api";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";

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

export interface GetStartedDerivedSignals {
  chat: boolean;
  hire: boolean;
  channel: boolean;
  task: boolean;
}

export function useGetStartedDerived(chatReady: boolean): GetStartedDerivedSignals {
  const api = useApi();
  const { read: readTeams } = useTeamsApi();

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

  return getStartedDerived;
}
