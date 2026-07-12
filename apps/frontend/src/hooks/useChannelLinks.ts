"use client";

import useSWR, { type KeyedMutator } from "swr";

import { useApi } from "@/lib/api";

/** One bot entry from `/channels/links/me` — a bot is keyed by its agent's
 *  UUID (channels are created per-agent, not on a fixed "main" agent).
 *  `bot_username` is populated by the backend via a `channels.status` probe;
 *  `linked` flips true once a member has paired their identity to the bot. */
export interface ChannelBotEntry {
  agent_id: string;
  bot_username: string;
  linked: boolean;
}

/** Response shape of `/channels/links/me`. */
export interface ChannelLinksResponse {
  telegram: ChannelBotEntry[];
  discord: ChannelBotEntry[];
  slack: ChannelBotEntry[];
  can_create_bots: boolean;
}

export interface UseChannelLinksResult {
  channelLinks: ChannelLinksResponse | undefined;
  /** True when ANY provider has at least one bot a member has paired their
   *  identity to (`linked: true`), for ANY agent. Mirrors the
   *  `emitStep("channel")` producer in BotSetupWizard, which fires on the
   *  same pairing success. */
  channelLinked: boolean;
  error: Error | undefined;
  isLoading: boolean;
  /** SWR's `mutate`, renamed for callers that just want "refetch". */
  refresh: KeyedMutator<ChannelLinksResponse>;
}

/**
 * The ONE `/channels/links/me` fetcher — hoisted out of three private
 * copies that each carried their own response type + SWR call against the
 * same endpoint: `useGetStartedDerived`'s channel-complete derivation,
 * `MyChannelsSection`, and `AgentChannelsSection`.
 *
 * `enabled` null-gates the SWR key (SWR's canonical conditional-fetch gate).
 * `useGetStartedDerived` passes its `chatReady` signal so this read never
 * fires during the cold-start window; the two channel-management surfaces
 * (which render only once mounted and always need the list) pass `true`.
 *
 * Options are fixed at `revalidateOnFocus: false, dedupingInterval:
 * 300_000` for every caller: channel-link state changes rarely (only on a
 * bot pairing), so there's no case where a caller needs tighter
 * revalidation than that.
 */
export function useChannelLinks(enabled: boolean): UseChannelLinksResult {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<ChannelLinksResponse>(
    enabled ? "/channels/links/me" : null,
    () => api.get("/channels/links/me") as Promise<ChannelLinksResponse>,
    { revalidateOnFocus: false, dedupingInterval: 300_000 },
  );
  const channelLinked =
    !!data &&
    (["telegram", "discord", "slack"] as const).some((p) => data[p]?.some((b) => b.linked));
  return { channelLinks: data, channelLinked, error, isLoading, refresh: mutate };
}
