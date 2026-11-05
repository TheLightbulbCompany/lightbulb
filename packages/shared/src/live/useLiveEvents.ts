import { useEffect, useRef, useState } from "react";

import { AppSyncEventsClient, type LiveErrorKind, type LiveStatus } from "./appsyncEvents";
import type { ChatIncomingMessage, LiveEvent, LiveEventOf } from "./types";
import type { LiveConfig } from "../messaging/types";

export interface UseLiveEventsOptions {
  config: LiveConfig | null;
  getToken: () => Promise<string | null>;
  userChannel: string | null;
  roomChannel: string | null;
  /** The room's `/frames` sibling channel (see `appsyncEvents.framesChannelFor`) —
   *  desired only alongside its matching `roomChannel`; anything else is refused
   *  by the client as a `protocol_error`. */
  framesChannel?: string | null;
  /** Shared sessions' frames channels, one per open terminal view (`/ws/<ws>/session-frames/<sid>`). */
  sessionFramesChannels?: readonly string[];
  /** A `/session-frames/` channel the authorizer refused: dropped from the socket, not retried.
   *  A transient subscribe error is not reported here; it takes the reconnect path. */
  onSubscribeError?: (channel: string) => void;
  /** Subscribe the workspace's `/ws/<ws>/tasks` channel (`task.changed`); only while the super-agent switch is on. */
  tasksChannel?: boolean;
  uuid: () => string;
  onEvent: (event: LiveEvent) => void;
  onChatMessage?: (frame: ChatIncomingMessage, event: LiveEventOf<"room.run.frame">) => void;
  /** Re-fetch live-config: membership may have changed while no notice could arrive (a socket
   *  reconnect, a refused sessions subscription, or a `workspace.access.removed`). */
  onConfigStale?: () => void;
  enabled?: boolean;
}

export interface UseLiveEventsResult {
  liveConnected: boolean;
  /** The socket is up but the active room's own subscription has not been acked yet. */
  roomSubscriptionPending: boolean;
  connectionRevision: number;
  liveChannels: readonly string[];
  lastError: LiveErrorKind | null;
}

const NO_CHANNELS: readonly string[] = [];

export function useLiveEvents(options: UseLiveEventsOptions): UseLiveEventsResult {
  const {
    config,
    userChannel,
    roomChannel,
    framesChannel = null,
    sessionFramesChannels = NO_CHANNELS,
    tasksChannel: tasksOn = false,
    enabled = true,
  } = options;
  const tokenRef = useRef(options.getToken);
  const uuidRef = useRef(options.uuid);
  const eventRef = useRef(options.onEvent);
  const chatRef = useRef(options.onChatMessage);
  const refusedRef = useRef(options.onSubscribeError);
  refusedRef.current = options.onSubscribeError;
  tokenRef.current = options.getToken;
  uuidRef.current = options.uuid;
  eventRef.current = options.onEvent;
  chatRef.current = options.onChatMessage;
  const staleRef = useRef(options.onConfigStale);
  staleRef.current = options.onConfigStale;

  // One sessions channel per workspace; AppSync admits current members only. A removal can't
  // close an open subscription server-side, so `workspace.access.removed` closes the socket here
  // and keeps it closed for that workspace (the next live-config is refused).
  const workspaceRef = useRef(config?.workspaceId);
  workspaceRef.current = config?.workspaceId;
  const [removedFrom, setRemovedFrom] = useState<string | null>(null);
  const live = enabled && !!config && removedFrom !== config.workspaceId;
  const sessionsChannel = config ? `/ws/${config.workspaceId}/sessions` : null;

  const tasksChannel = config && tasksOn ? `/ws/${config.workspaceId}/tasks` : null;
  const desired = [userChannel, sessionsChannel, tasksChannel, roomChannel, framesChannel, ...sessionFramesChannels]
    .filter((channel): channel is string => !!channel);
  const sessionFramesKey = sessionFramesChannels.join(" ");
  const desiredRef = useRef(desired);
  desiredRef.current = desired;
  const clientRef = useRef<AppSyncEventsClient | null>(null);
  const [status, setStatus] = useState<LiveStatus>({ connected: false, liveChannels: [], ackCount: 0 });
  const [lastError, setLastError] = useState<LiveErrorKind | null>(null);

  useEffect(() => {
    if (!live || !config) {
      clientRef.current?.close();
      clientRef.current = null;
      setStatus({ connected: false, liveChannels: [], ackCount: 0 });
      return;
    }

    let acks = 0;
    const client = new AppSyncEventsClient({
      httpEndpoint: config.httpEndpoint,
      realtimeEndpoint: config.realtimeEndpoint,
      getToken: () => tokenRef.current(),
      uuid: () => uuidRef.current(),
      onEvent: (event) => {
        eventRef.current(event);
        if (event.type === "workspace.access.removed" && event.payload.workspaceId === workspaceRef.current) {
          setRemovedFrom(event.payload.workspaceId);
          staleRef.current?.();
          return;
        }
        if (event.type === "room.run.frame") chatRef.current?.(event.payload.frame, event);
      },
      onStatus: (next) => {
        // A second ack on one client is a reconnect: a removal notice may have been missed.
        if (next.ackCount > 1 && next.ackCount !== acks) staleRef.current?.();
        acks = next.ackCount;
        setStatus(next);
      },
      onError: (kind, channel) => {
        setLastError(kind);
        if (kind === "subscribe_error" && channel?.endsWith("/sessions")) staleRef.current?.();
        if (kind === "subscribe_refused" && channel) refusedRef.current?.(channel);
      },
    });
    clientRef.current = client;
    client.setChannels(desiredRef.current);
    return () => {
      client.close();
      if (clientRef.current === client) clientRef.current = null;
    };
  }, [live, config?.httpEndpoint, config?.realtimeEndpoint]);

  useEffect(() => {
    clientRef.current?.setChannels(desired);
  }, [userChannel, sessionsChannel, tasksChannel, roomChannel, framesChannel, sessionFramesKey]);

  // "Connected" is the socket being up with the user's own channel live. It is
  // NOT "every channel I currently want is acked": a freshly claimed room
  // spends a few frames between subscribe and subscribe_success, and treating
  // that window as a disconnect made every room open flash "Reconnecting live
  // updates" — the socket had never dropped. That pending window is exposed
  // separately as `roomSubscriptionPending` for callers that care.
  const socketLive = live && status.connected;
  const liveConnected = socketLive && (!userChannel || status.liveChannels.includes(userChannel));
  const roomSubscriptionPending = socketLive && !!roomChannel && !status.liveChannels.includes(roomChannel);
  return {
    liveConnected,
    roomSubscriptionPending,
    connectionRevision: status.ackCount,
    liveChannels: status.liveChannels,
    lastError,
  };
}
