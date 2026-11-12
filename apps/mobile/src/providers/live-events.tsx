import { useAuth, useOrganization } from "@clerk/expo";
import {
  ApiError,
  createApiClient,
  createMessagingClient,
  createRuntimeClient,
  refreshOnboardingStatus,
  useLiveEvents as useSharedLiveEvents,
  createTasksClient,
  useAgentDirectory,
  type AgentNames,
  useTaskChangedRefresh,
  type TasksClient,
  type ApiMethods,
  type ChatIncomingMessage,
  type LiveConfig,
  type LiveEvent,
  type LiveEventOf,
  type RuntimeRpcSource,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
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
import { AppState } from "react-native";

import { makeAdapter } from "../lib/adapter";

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
  liveError: string | null;
  messaging: ReturnType<typeof createMessagingClient>;
  /** The super-agent task routes. */
  tasks: TasksClient;
  /** Agent id → name (task specialists are agent ids); empty until it loads. */
  agentNames: AgentNames;
  /** The front agent (displays as "Lightbulb"); undefined until the agents list loads. */
  frontAgentId: string | undefined;
  /** The signed-in API client (lane A's automation routes read through it). */
  api: ApiMethods;
  claimActiveRoomSubscription(room: ActiveRoomSubscription): () => void;
  onChatMessage(
    handler: (
      frame: ChatIncomingMessage,
      event: LiveEventOf<"room.run.frame">,
    ) => void,
  ): () => void;
}

const LiveEventsContext = createContext<LiveEventsContextValue | null>(null);

export function LiveEventsProvider({ children }: { children: ReactNode }) {
  const { getToken, isLoaded, userId } = useAuth();
  const { organization } = useOrganization();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );
  const messaging = useMemo(() => createMessagingClient(api), [api]);
  const runtime = useMemo(
    () => createRuntimeClient(api, () => randomUUID()),
    [api],
  );
  const ownerScope = organization?.id ?? userId;
  // Keyed by the signed-in workspace and person: task reads are one viewer's (the server keeps only what they may view),
  // so an org switch or another person signing in never reuses the last one's cached tasks.
  const taskScope = [organization?.id, userId].filter(Boolean).join(":");
  const tasks = useMemo(() => createTasksClient(api, taskScope), [api, taskScope]);
  const refreshTask = useTaskChangedRefresh(tasks);
  const httpReady = !!isLoaded && !!userId;
  const [config, setConfig] = useState<LiveConfig | null>(null);
  const [configError, setConfigError] = useState<Error | null>(null);
  const [foreground, setForeground] = useState(AppState.currentState === "active");
  const [activeRoom, setActiveRoom] = useState<ActiveRoomSubscription | null>(null);
  const activeRoomClaim = useRef<symbol | null>(null);
  const eventHandlers = useRef(new Set<(event: LiveEvent) => void>());
  const chatHandlers = useRef(
    new Set<
      (
        frame: ChatIncomingMessage,
        event: LiveEventOf<"room.run.frame">,
      ) => void
    >(),
  );

  useEffect(() => {
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    setConfig(null);
    setConfigError(null);
    if (!httpReady) return;

    const load = () => {
      void messaging.getLiveConfig().then((next) => {
        if (cancelled) return;
        setConfig(next);
        setConfigError(null);
      }).catch((error: unknown) => {
        if (cancelled) return;
        setConfigError(error instanceof Error ? error : new Error("Live configuration unavailable"));
        retry = setTimeout(load, 3_000);
      });
    };
    load();
    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
    };
  }, [httpReady, messaging, ownerScope, userId]);

  useEffect(() => {
    setActiveRoom(null);
  }, [userId, ownerScope]);

  // Swap in a fresh live-config without dropping the socket (the loader above nulls it first):
  // membership may have changed while the app was backgrounded or the socket was down.
  // An answer for the workspace this was asked in only: the user may have switched meanwhile.
  // A 403 (no longer a member) drops the config, which closes the socket and every subscription.
  const scopeRef = useRef(ownerScope);
  scopeRef.current = ownerScope;
  const refreshConfig = useCallback(() => {
    if (!httpReady) return;
    const asked = scopeRef.current;
    void messaging.getLiveConfig().then(
      (next) => { if (scopeRef.current === asked) setConfig(next); },
      (error: unknown) => { if (scopeRef.current === asked && error instanceof ApiError && error.status === 403) setConfig(null); },
    );
  }, [httpReady, messaging]);

  // Presence for the away email (T15): every two minutes while the app is in the foreground.
  useEffect(() => {
    if (!httpReady || !foreground) return;
    const ping = () => void Promise.resolve().then(() => api.post("/users/me/active", {})).catch(() => undefined);
    ping();
    const timer = setInterval(ping, 120_000);
    return () => clearInterval(timer);
  }, [httpReady, foreground, api, userId]);

  const refreshConfigRef = useRef(refreshConfig);
  refreshConfigRef.current = refreshConfig;
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active") {
        setForeground(true);
        refreshConfigRef.current();
      }
      else if (state === "background") setForeground(false);
    });
    return () => subscription.remove();
  }, []);

  const dispatchEvent = useCallback((event: LiveEvent) => {
    // The web twin's rule (hooks/useLiveEvents.tsx): a status nudge refetches the one
    // /container/status key useOnboardingStatus reads — the event is how the stepper advances.
    if (event.type === "runtime.resource.invalidated" && (event.payload.resource === "onboarding" || event.payload.resource === "health")) {
      refreshOnboardingStatus();
    }
    if (event.type === "task.changed") refreshTask(event.payload);
    for (const handler of eventHandlers.current) handler(event);
  }, [refreshTask]);
  const dispatchChat = useCallback(
    (
      frame: ChatIncomingMessage,
      event: LiveEventOf<"room.run.frame">,
    ) => {
      for (const handler of chatHandlers.current) handler(frame, event);
    },
    [],
  );

  // Specialist names for task cards ("Finance", never the agent id): one read of the agents list,
  // shared by every card through this context.
  const { names: agentNames, frontAgentId } = useAgentDirectory(httpReady ? tasks.cacheScope : null,
    async () => ((await api.get("/teams/agents")) as { agents?: { id: string; name?: string | null; role?: string | null; reportsTo?: string | null; origin?: string | null }[] } | null)?.agents ?? []);
  const userChannel = config ? `/ws/${config.workspaceId}/users/${config.userChannelKey}` : null;
  const roomChannel = config && activeRoom
    ? `/ws/${activeRoom.workspaceId ?? config.workspaceId}/rooms/${activeRoom.roomId}/${activeRoom.deliveryKey}`
    : null;
  const live = useSharedLiveEvents({
    config: config ?? null,
    getToken: () => getTokenRef.current({ template: "lightbulb-appsync" }),
    userChannel,
    roomChannel,
    uuid: () => randomUUID(),
    onEvent: dispatchEvent,
    onChatMessage: dispatchChat,
    onConfigStale: refreshConfig,
    tasksChannel: true,
    enabled: foreground && httpReady,
  });

  const claimActiveRoomSubscription = useCallback(
    (room: ActiveRoomSubscription) => {
      const claim = Symbol();
      activeRoomClaim.current = claim;
      setActiveRoom((current) =>
        current?.roomId === room.roomId &&
        current.deliveryKey === room.deliveryKey
          ? current
          : room,
      );
      return () => {
        if (activeRoomClaim.current !== claim) return;
        activeRoomClaim.current = null;
        setActiveRoom(null);
      };
    },
    [],
  );
  const onEvent = useCallback((handler: (event: LiveEvent) => void) => {
    eventHandlers.current.add(handler);
    return () => eventHandlers.current.delete(handler);
  }, []);
  const onChatMessage = useCallback(
    (
      handler: (
        frame: ChatIncomingMessage,
        event: LiveEventOf<"room.run.frame">,
      ) => void,
    ) => {
      chatHandlers.current.add(handler);
      return () => chatHandlers.current.delete(handler);
    },
    [],
  );

  const value = useMemo<LiveEventsContextValue>(
    () => ({
      httpReady,
      liveConnected: live.liveConnected,
      liveChannels: live.liveChannels,
      liveError: configError ? "Live updates are reconnecting." : live.lastError,
      messaging,
      tasks,
      agentNames,
      frontAgentId,
      api,
      claimActiveRoomSubscription,
      onChatMessage,
      rpc: runtime.rpc,
      getOperation: runtime.getOperation,
      onEvent,
      connectionRevision: live.connectionRevision,
    }),
    [
      httpReady,
      live.liveConnected,
      live.liveChannels,
      live.lastError,
      live.connectionRevision,
      configError,
      messaging,
      tasks,
      agentNames,
      frontAgentId,
      api,
      runtime,
      claimActiveRoomSubscription,
      onChatMessage,
      onEvent,
    ],
  );

  return (
    <LiveEventsContext.Provider value={value}>
      {children}
    </LiveEventsContext.Provider>
  );
}

export function useLiveEvents(): LiveEventsContextValue {
  const value = useContext(LiveEventsContext);
  if (!value) {
    throw new Error("useLiveEvents must be used within a LiveEventsProvider");
  }
  return value;
}
