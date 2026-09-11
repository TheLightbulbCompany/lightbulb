import { useEffect, useRef, useState } from "react";

import { AppSyncEventsClient, type LiveErrorKind, type LiveStatus } from "./appsyncEvents";
import type { ChatIncomingMessage, LiveEvent, LiveEventOf } from "./types";
import type { LiveConfig } from "../messaging/types";

export interface UseLiveEventsOptions {
  config: LiveConfig | null;
  getToken: () => Promise<string | null>;
  userChannel: string | null;
  roomChannel: string | null;
  uuid: () => string;
  onEvent: (event: LiveEvent) => void;
  onChatMessage?: (frame: ChatIncomingMessage, event: LiveEventOf<"room.run.frame">) => void;
  onReconcile?: () => void;
  enabled?: boolean;
}

export interface UseLiveEventsResult {
  liveConnected: boolean;
  lastEventAt: number | null;
  connectionRevision: number;
  liveChannels: readonly string[];
  lastError: LiveErrorKind | null;
}

export function useLiveEvents(options: UseLiveEventsOptions): UseLiveEventsResult {
  const {
    config,
    userChannel,
    roomChannel,
    enabled = true,
  } = options;
  const tokenRef = useRef(options.getToken);
  const uuidRef = useRef(options.uuid);
  const eventRef = useRef(options.onEvent);
  const chatRef = useRef(options.onChatMessage);
  const reconcileRef = useRef(options.onReconcile);
  tokenRef.current = options.getToken;
  uuidRef.current = options.uuid;
  eventRef.current = options.onEvent;
  chatRef.current = options.onChatMessage;
  reconcileRef.current = options.onReconcile;

  const desired = [userChannel, roomChannel].filter((channel): channel is string => !!channel);
  const desiredRef = useRef(desired);
  desiredRef.current = desired;
  const clientRef = useRef<AppSyncEventsClient | null>(null);
  const reconciledRef = useRef("");
  const [status, setStatus] = useState<LiveStatus>({ connected: false, liveChannels: [], ackCount: 0 });
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const [lastError, setLastError] = useState<LiveErrorKind | null>(null);

  useEffect(() => {
    if (!enabled || !config) {
      clientRef.current?.close();
      clientRef.current = null;
      reconciledRef.current = "";
      setStatus({ connected: false, liveChannels: [], ackCount: 0 });
      return;
    }

    const client = new AppSyncEventsClient({
      httpEndpoint: config.httpEndpoint,
      realtimeEndpoint: config.realtimeEndpoint,
      getToken: () => tokenRef.current(),
      uuid: () => uuidRef.current(),
      onEvent: (event) => {
        setLastEventAt(Date.now());
        eventRef.current(event);
        if (event.type === "room.run.frame") chatRef.current?.(event.payload.frame, event);
      },
      onStatus: (next) => {
        setStatus(next);
        const wanted = desiredRef.current;
        const allLive = next.connected && wanted.length > 0 && wanted.every((channel) => next.liveChannels.includes(channel));
        if (!allLive) return;
        const reconciliationKey = `${next.ackCount}|${wanted.join("|")}`;
        if (reconciliationKey === reconciledRef.current) return;
        reconciledRef.current = reconciliationKey;
        reconcileRef.current?.();
      },
      onError: (kind) => setLastError(kind),
    });
    clientRef.current = client;
    client.setChannels(desiredRef.current);
    return () => {
      client.close();
      if (clientRef.current === client) clientRef.current = null;
    };
  }, [enabled, config?.httpEndpoint, config?.realtimeEndpoint]);

  useEffect(() => {
    clientRef.current?.setChannels(desired);
  }, [userChannel, roomChannel]);

  const liveConnected = enabled && !!config && status.connected && desired.every((channel) => status.liveChannels.includes(channel));
  return {
    liveConnected,
    lastEventAt,
    connectionRevision: status.ackCount,
    liveChannels: status.liveChannels,
    lastError,
  };
}
