"use client";

import useSWR, { type KeyedMutator } from "swr";

import { useApi } from "@/lib/api";

export interface ChannelPairingRequest {
  requestId: string;
  channel: string;
  channelLabel: string;
  accountId: string;
  accountLabel?: string;
  senderId: string;
  senderLabel: string;
  metadata?: Record<string, string>;
  createdAt: string;
  lastSeenAt: string;
  expiresAt: string;
  notifySupported: boolean;
}

export interface ChannelPairingSnapshot {
  requests: ChannelPairingRequest[];
}

export function useChannelPairingRequests(): {
  data: ChannelPairingSnapshot | undefined;
  error: Error | undefined;
  isLoading: boolean;
  refresh: KeyedMutator<ChannelPairingSnapshot>;
} {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<ChannelPairingSnapshot>(
    "/channels/pairing/requests",
    () =>
      api.get("/channels/pairing/requests") as Promise<ChannelPairingSnapshot>,
    { refreshInterval: 15_000 },
  );

  return { data, error, isLoading, refresh: mutate };
}
