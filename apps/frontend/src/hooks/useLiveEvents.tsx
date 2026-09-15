"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useAuth, useUser } from "@clerk/nextjs";
import useSWR, { mutate } from "swr";
import {
  createMessagingClient,
  createRuntimeClient,
  useLiveEvents as useSharedLiveEvents,
  type ChatIncomingMessage,
  type LiveEvent,
  type LiveEventOf,
  type RuntimeRpcSource,
} from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { readDesktopPermissionGrant } from "@/lib/desktopPermissions";
import { subscribeDesktopPermissions } from "@/lib/desktopPermissionsSubscribe";

export interface ActiveRoomSubscription {
  roomId: string;
  deliveryKey: string;
  /** The room's workspace segment; falls back to the caller's for rows without one. */
  workspaceId?: string | null;
}

export interface LiveEventsContextValue extends RuntimeRpcSource {
  httpReady: boolean;
  liveConnected: boolean;
  liveChannels: readonly string[];
  nodeConnected: boolean;
  nodeStatus: string;
  messaging: ReturnType<typeof createMessagingClient>;
  claimActiveRoomSubscription(room: ActiveRoomSubscription): () => void;
  onChatMessage(handler: (frame: ChatIncomingMessage, event: LiveEventOf<"room.run.frame">) => void): () => void;
}

const LiveEventsContext = createContext<LiveEventsContextValue | null>(null);

export function LiveEventsProvider({ children }: { children: ReactNode }) {
  const { getToken, isLoaded, userId, orgId } = useAuth();
  const { user } = useUser();
  const api = useApi();
  const messaging = useMemo(() => createMessagingClient(api), [api]);
  const runtimeClient = useMemo(() => createRuntimeClient(api, () => crypto.randomUUID()), [api]);
  const ownerScope = orgId ?? userId ?? null;
  const httpReady = !!isLoaded && !!userId;
  const { data: config } = useSWR(
    httpReady ? ["messaging-live-config", userId, ownerScope] : null,
    () => messaging.getLiveConfig(),
    { revalidateOnFocus: false },
  );
  const [activeRoomState, setActiveRoomState] = useState<{
    ownerScope: string | null;
    room: ActiveRoomSubscription | null;
  } | null>(null);
  const activeRoomClaim = useRef<symbol | null>(null);
  const activeRoom = ownerScope && activeRoomState?.ownerScope === ownerScope
    ? activeRoomState.room
    : null;
  const [nodeStatus, setNodeStatus] = useState("disconnected");
  const [desktopPermissionRevision, setDesktopPermissionRevision] = useState(0);
  const eventHandlers = useRef(new Set<(event: LiveEvent) => void>());
  const chatHandlers = useRef(new Set<(frame: ChatIncomingMessage, event: LiveEventOf<"room.run.frame">) => void>());

  const dispatchEvent = useCallback((event: LiveEvent) => {
    if (event.type === "runtime.resource.invalidated" && (event.payload.resource === "onboarding" || event.payload.resource === "health")) {
      void mutate(["/container/status", `${userId}:${orgId}`]);
    }
    for (const handler of eventHandlers.current) handler(event);
  }, [userId, orgId]);
  const dispatchChat = useCallback((frame: ChatIncomingMessage, event: LiveEventOf<"room.run.frame">) => {
    for (const handler of chatHandlers.current) handler(frame, event);
  }, []);

  const userChannel = config ? `/ws/${config.workspaceId}/users/${config.userChannelKey}` : null;
  const roomChannel = config && activeRoom ? `/ws/${activeRoom.workspaceId ?? config.workspaceId}/rooms/${activeRoom.roomId}/${activeRoom.deliveryKey}` : null;
  const live = useSharedLiveEvents({
    config: config ?? null,
    getToken: () => getToken({ template: "lightbulb-appsync" }),
    userChannel,
    roomChannel,
    uuid: () => crypto.randomUUID(),
    onEvent: dispatchEvent,
    onChatMessage: dispatchChat,
  });

  const onEvent = useCallback((handler: (event: LiveEvent) => void) => {
    eventHandlers.current.add(handler);
    return () => eventHandlers.current.delete(handler);
  }, []);
  const onChatMessage = useCallback((handler: (frame: ChatIncomingMessage, event: LiveEventOf<"room.run.frame">) => void) => {
    chatHandlers.current.add(handler);
    return () => chatHandlers.current.delete(handler);
  }, []);
  const claimActiveRoomSubscription = useCallback((room: ActiveRoomSubscription) => {
    const claim = Symbol();
    activeRoomClaim.current = claim;
    setActiveRoomState((current) =>
      current?.ownerScope === ownerScope &&
      current.room?.roomId === room.roomId &&
      current.room.deliveryKey === room.deliveryKey
        ? current
        : { ownerScope, room },
    );
    return () => {
      if (activeRoomClaim.current !== claim) return;
      activeRoomClaim.current = null;
      setActiveRoomState({ ownerScope, room: null });
    };
  }, [ownerScope]);

  useEffect(() => subscribeDesktopPermissions(() => {
    setDesktopPermissionRevision((revision) => revision + 1);
  }), []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tauri = (window as any).__TAURI__;
    if (!tauri?.event?.listen) return;
    let unlisten: (() => void) | null = null;
    void tauri.event.listen("node:status", (event: { payload: string }) => {
      setNodeStatus(event.payload);
    }).then((stop: () => void) => {
      unlisten = stop;
    });
    return () => unlisten?.();
  }, []);

  useEffect(() => {
    if (typeof window === "undefined" || !user?.id) return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tauri = (window as any).__TAURI__;
    const grant = readDesktopPermissionGrant();
    if (!tauri?.core?.invoke || !grant) return;
    let cancelled = false;
    const sendAuth = async () => {
      const token = await getToken().catch(() => null);
      if (cancelled || !token) return;
      await tauri.core.invoke("send_auth_token", {
        token,
        displayName: user.fullName || user.firstName || "User",
        userId: user.id,
        permissions: grant.permissions,
      }).catch(() => {});
    };
    void sendAuth();
    const refresh = window.setInterval(() => void sendAuth(), 45_000);
    return () => {
      cancelled = true;
      window.clearInterval(refresh);
    };
  }, [user?.id, user?.fullName, user?.firstName, getToken, desktopPermissionRevision, nodeStatus]);

  const value = useMemo<LiveEventsContextValue>(() => ({
    httpReady,
    liveConnected: live.liveConnected,
    liveChannels: live.liveChannels,
    nodeConnected: nodeStatus === "connected",
    nodeStatus,
    messaging,
    claimActiveRoomSubscription,
    onChatMessage,
    rpc: runtimeClient.rpc,
    getOperation: runtimeClient.getOperation,
    onEvent,
    connectionRevision: live.connectionRevision,
  }), [
    httpReady,
    live.liveConnected,
    live.liveChannels,
    live.connectionRevision,
    nodeStatus,
    messaging,
    claimActiveRoomSubscription,
    onChatMessage,
    runtimeClient,
    onEvent,
  ]);

  return <LiveEventsContext.Provider value={value}>{children}</LiveEventsContext.Provider>;
}

export function useLiveEvents(): LiveEventsContextValue {
  const value = useContext(LiveEventsContext);
  if (!value) throw new Error("useLiveEvents must be used within a LiveEventsProvider");
  return value;
}
