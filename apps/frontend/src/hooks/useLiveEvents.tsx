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
import useSWR, { mutate, preload } from "swr";
import {
  ApiError,
  createMessagingClient,
  createRuntimeClient,
  agentDirectory,
  createTasksClient,
  roomsListKey,
  type DirectoryAgent,
  type AgentNames,
  framesChannelFor,
  useTaskChangedRefresh,
  useLiveEvents as useSharedLiveEvents,
  type ChatIncomingMessage,
  type LiveEvent,
  type LiveEventOf,
  type RuntimeRpcSource,
  type TasksClient,
} from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { readDesktopPermissionGrant } from "@/lib/desktopPermissions";
import { subscribeDesktopPermissions } from "@/lib/desktopPermissionsSubscribe";
import { readLastKnown, writeLastKnown } from "@/lib/lastKnown";

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
  /** Why the node is in its current status, when the desktop said (e.g. a pair refusal). */
  nodeStatusReason: string;
  messaging: ReturnType<typeof createMessagingClient>;
  /** The workspace's super-agent switch, from the live config already fetched (contracts-v2 §1). */
  superAgent: boolean;
  /** The live config answered (or failed), so `superAgent` is final: a redirect that depends on it waits for this. */
  featuresLoaded?: boolean;
  /** The super-agent task routes; only called while `superAgent` is on. */
  tasks: TasksClient;
  /** Agent id → name while the switch is on (task specialists are agent ids); empty until it loads. */
  agentNames: AgentNames;
  /** The front agent (shows as "Lightbulb" while the switch is on); undefined until the agents list loads. */
  frontAgentId: string | undefined;
  claimActiveRoomSubscription(room: ActiveRoomSubscription): () => void;
  /** Subscribe the active room's frames sibling channel (a Mac tab's view-tier frames) while
   * any claim is held. Ref-counted; the returned function releases this claim once. */
  claimFrames(): () => void;
  /** A shared session's frames channel in this workspace, `/ws/<ws>/session-frames/<sid>`; null until live config loads. */
  sessionFramesChannel(sessionId: string): string | null;
  /** Subscribe a shared session's frames channel while the claim is held. `onRefused`: AppSync's
   * authorizer refused the subscribe (not a member, or the session is not shared). */
  claimSessionFrames(channel: string, onRefused: () => void): () => void;
  onChatMessage(handler: (frame: ChatIncomingMessage, event: LiveEventOf<"room.run.frame">) => void): () => void;
}

const LiveEventsContext = createContext<LiveEventsContextValue | null>(null);

/** `preloadRooms`: start the room list here (the chat surface only; Settings has no room list). */
export function LiveEventsProvider({ children, preloadRooms = false }: { children: ReactNode; preloadRooms?: boolean }) {
  const { getToken, isLoaded, userId, orgId } = useAuth();
  const { user } = useUser();
  const api = useApi();
  const messaging = useMemo(() => createMessagingClient(api), [api]);
  const runtimeClient = useMemo(() => createRuntimeClient(api, () => crypto.randomUUID()), [api]);
  const ownerScope = orgId ?? userId ?? null;
  // Keyed by the signed-in workspace and person: a task list is one viewer's (the server keeps only the tasks they may
  // view), so an org switch or another person signing in here never reuses the last one's lists (dev walk 12).
  const taskScope = [orgId, userId].filter(Boolean).join(":");
  const tasks = useMemo(() => createTasksClient(api, taskScope), [api, taskScope]);
  const refreshTask = useTaskChangedRefresh(tasks);
  const httpReady = !!isLoaded && !!userId;
  // The room list gates "Loading chats…". Start it with this provider's first reads instead of
  // when the workspace mounts; useRooms picks up the in-flight request under the same key.
  useEffect(() => {
    if (preloadRooms && httpReady && userId) void preload(roomsListKey(userId, orgId), () => messaging.listRooms()).catch(() => undefined);
  }, [preloadRooms, httpReady, userId, orgId, messaging]);
  const { data: liveConfig, error: configError, mutate: refreshConfig } = useSWR(
    httpReady ? ["messaging-live-config", userId, ownerScope] : null,
    () => messaging.getLiveConfig(),
    // The refresh is the backend TeamsEventBroker's keep-alive: it releases a user's
    // Paperclip socket after 10 min without a live-config fetch. SWR keeps the same
    // object when the response is unchanged, so this does not resubscribe AppSync.
    // On focus too: it is how a removed member's open tab learns it (the 403 below).
    { revalidateOnFocus: true, refreshInterval: 5 * 60_000, refreshWhenHidden: true },
  );
  // No longer a member (403): SWR would keep the last config, and with it the socket and every
  // subscription. Dropping it closes them all.
  const config = configError instanceof ApiError && configError.status === 403 ? undefined : liveConfig;
  const featuresLoaded = config !== undefined || configError !== undefined;
  // Until the live config answers (1-3 s on a cold load), the switch reads what this browser last saw for this person
  // and workspace, so a super-agent reload draws its own shell at once instead of the old one first (W12-18).
  const switchScope = `${userId}:${ownerScope}`;
  // (Read once Clerk has loaded: never during hydration, so the server's render and the first client render agree.)
  const superAgent = featuresLoaded ? config?.features?.superAgent === true : httpReady && readLastKnown("superAgent", switchScope) === true;
  useEffect(() => {
    if (config && userId) writeLastKnown("superAgent", switchScope, config.features?.superAgent === true);
  }, [config, userId, switchScope]);
  // Specialist names for task cards ("Finance", never the agent id): one read of the agents list while
  // the switch is on, shared by every card through this context.
  // The same `/teams/agents` SWR key the roster and the panels read, so a cold load makes one request, not two.
  const { data: teamAgents } = useTeamsApi().read<{ agents?: DirectoryAgent[] }>(superAgent && httpReady ? "/agents" : null);
  const { names: agentNames, frontAgentId } = useMemo(() => agentDirectory(teamAgents?.agents ?? []), [teamAgents]);
  const [activeRoomState, setActiveRoomState] = useState<{
    ownerScope: string | null;
    room: ActiveRoomSubscription | null;
  } | null>(null);
  const activeRoomClaim = useRef<symbol | null>(null);
  const activeRoom = ownerScope && activeRoomState?.ownerScope === ownerScope
    ? activeRoomState.room
    : null;
  const [frameClaims, setFrameClaims] = useState(0);
  const sessionFrameClaims = useRef(new Set<{ channel: string; onRefused: () => void }>());
  const [sessionFramesChannels, setSessionFramesChannels] = useState<string[]>([]);
  const [nodeStatus, setNodeStatus] = useState("disconnected");
  const [nodeStatusReason, setNodeStatusReason] = useState("");
  const [desktopPermissionRevision, setDesktopPermissionRevision] = useState(0);
  const eventHandlers = useRef(new Set<(event: LiveEvent) => void>());
  const chatHandlers = useRef(new Set<(frame: ChatIncomingMessage, event: LiveEventOf<"room.run.frame">) => void>());

  const dispatchEvent = useCallback((event: LiveEvent) => {
    if (event.type === "runtime.resource.invalidated" && (event.payload.resource === "onboarding" || event.payload.resource === "health")) {
      void mutate(["/container/status", `${userId}:${orgId}`]);
    }
    if (event.type === "task.changed") refreshTask(event.payload);
    for (const handler of eventHandlers.current) handler(event);
  }, [userId, orgId, refreshTask]);
  const dispatchChat = useCallback((frame: ChatIncomingMessage, event: LiveEventOf<"room.run.frame">) => {
    for (const handler of chatHandlers.current) handler(frame, event);
  }, []);

  const userChannel = config ? `/ws/${config.workspaceId}/users/${config.userChannelKey}` : null;
  const roomChannel = config && activeRoom ? `/ws/${activeRoom.workspaceId ?? config.workspaceId}/rooms/${activeRoom.roomId}/${activeRoom.deliveryKey}` : null;
  // Derived from the room channel in the same render, so the pair can never disagree.
  const framesChannel = frameClaims > 0 && roomChannel ? framesChannelFor(roomChannel) : null;
  const live = useSharedLiveEvents({
    config: config ?? null,
    getToken: () => getToken({ template: "lightbulb-appsync" }),
    userChannel,
    roomChannel,
    framesChannel,
    sessionFramesChannels,
    tasksChannel: superAgent,
    onSubscribeError: (channel) => {
      for (const claim of sessionFrameClaims.current) if (claim.channel === channel) claim.onRefused();
    },
    uuid: () => crypto.randomUUID(),
    onEvent: dispatchEvent,
    onChatMessage: dispatchChat,
    onConfigStale: () => void refreshConfig(),
  });

  // Presence for the away email (T15): every two minutes while this tab is VISIBLE, and on
  // becoming visible. A hidden tab says nothing, so a person who walked away gets the email.
  useEffect(() => {
    if (!httpReady) return;
    const ping = () => {
      if (document.visibilityState === "visible") void Promise.resolve().then(() => api.post("/users/me/active", {})).catch(() => undefined);
    };
    ping();
    const timer = window.setInterval(ping, 120_000);
    document.addEventListener("visibilitychange", ping);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", ping);
    };
    // `api` identity churns every render; the ping only needs the signed-in user.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [httpReady, userId]);

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

  const claimFrames = useCallback(() => {
    setFrameClaims((count) => count + 1);
    let held = true;
    return () => {
      if (!held) return;
      held = false;
      setFrameClaims((count) => count - 1);
    };
  }, []);

  const claimSessionFrames = useCallback((channel: string, onRefused: () => void) => {
    const claims = sessionFrameClaims.current;
    const sync = () => setSessionFramesChannels([...new Set([...claims].map((c) => c.channel))]);
    const claim = { channel, onRefused };
    claims.add(claim);
    sync();
    return () => { claims.delete(claim); sync(); };
  }, []);
  const workspaceId = config?.workspaceId ?? null;
  const sessionFramesChannel = useCallback(
    (sessionId: string) => (workspaceId ? `/ws/${workspaceId}/session-frames/${sessionId}` : null),
    [workspaceId],
  );

  useEffect(() => subscribeDesktopPermissions(() => {
    setDesktopPermissionRevision((revision) => revision + 1);
  }), []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tauri = (window as any).__TAURI__;
    if (!tauri?.event?.listen) return;
    let unlisten: (() => void) | null = null;
    let unlistenReason: (() => void) | null = null;
    void tauri.event.listen("node:status", (event: { payload: string }) => {
      setNodeStatus(event.payload);
      setNodeStatusReason("");
    }).then((stop: () => void) => {
      unlisten = stop;
    });
    // Emitted right after a status it explains (e.g. why pairing was refused).
    void tauri.event.listen("node:status_reason", (event: { payload: string }) => {
      setNodeStatusReason(event.payload);
    }).then((stop: () => void) => {
      unlistenReason = stop;
    });
    return () => {
      unlisten?.();
      unlistenReason?.();
    };
  }, []);

  // For the desktop's `lightbulb claude` line: "Shared with <workspace>".
  const workspaceName = user?.organizationMemberships?.find((m) => m.organization.id === orgId)?.organization.name ?? null;
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
        // The id lets the app name this workspace only when it is the one this Mac shares to.
        workspaceId: orgId ?? null,
        workspaceName,
      }).catch(() => {});
    };
    void sendAuth();
    const refresh = window.setInterval(() => void sendAuth(), 45_000);
    return () => {
      cancelled = true;
      window.clearInterval(refresh);
    };
  }, [user?.id, user?.fullName, user?.firstName, orgId, workspaceName, getToken, desktopPermissionRevision, nodeStatus]);

  const value = useMemo<LiveEventsContextValue>(() => ({
    httpReady,
    liveConnected: live.liveConnected,
    liveChannels: live.liveChannels,
    nodeConnected: nodeStatus === "connected",
    nodeStatus,
    nodeStatusReason,
    messaging,
    superAgent,
    featuresLoaded,
    tasks,
    agentNames,
    frontAgentId,
    claimActiveRoomSubscription,
    claimFrames,
    claimSessionFrames,
    sessionFramesChannel,
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
    nodeStatusReason,
    messaging,
    superAgent,
    featuresLoaded,
    tasks,
    agentNames,
    frontAgentId,
    claimActiveRoomSubscription,
    claimFrames,
    claimSessionFrames,
    sessionFramesChannel,
    onChatMessage,
    runtimeClient,
    onEvent,
  ]);

  return <LiveEventsContext.Provider value={value}>{children}</LiveEventsContext.Provider>;
}

/** The provider's value, or null outside one (a component rendered on its own, as in tests). */
export function useOptionalLiveEvents(): LiveEventsContextValue | null {
  return useContext(LiveEventsContext);
}

export function useLiveEvents(): LiveEventsContextValue {
  const value = useContext(LiveEventsContext);
  if (!value) throw new Error("useLiveEvents must be used within a LiveEventsProvider");
  return value;
}

/** Super-agent UI switch: the live config's `features.superAgent` (until it answers, what this browser last saw for this
 *  person and workspace); false outside a provider. */
export function useSuperAgent(): boolean {
  return useContext(LiveEventsContext)?.superAgent ?? false;
}
