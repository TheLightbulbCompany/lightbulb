import { useAuth } from "@clerk/clerk-expo";
import { useRooms as useSharedRooms, type RoomsPlatform } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { useMemo } from "react";
import { AppState } from "react-native";

import { useLiveEvents } from "../providers/live-events";

// Mobile's half of RoomsPlatform, the mirror of apps/frontend/src/hooks/useRooms.ts.
// Both callbacks are module-level constants so the shared hook's visibility
// effect subscribes once, not once per render — the contract on `RoomsPlatform`
// turns an unstable field into a render storm, not an error.
const isVisible = () => AppState.currentState === "active";
const onVisibilityChange = (listener: () => void) => {
  const subscription = AppState.addEventListener("change", listener);
  return () => subscription.remove();
};

/**
 * The agent chat screen opens exactly one DM, so `autoSelect` is off: a room
 * that cannot be resolved must stay empty rather than silently fall back to
 * rooms[0], which on mobile would be some other agent's conversation.
 */
export function useRooms(initialAgentId: string | null) {
  const { userId, orgId } = useAuth();
  const live = useLiveEvents();
  const platform = useMemo<RoomsPlatform>(() => ({
    userId: userId ?? null,
    orgId: orgId ?? null,
    live,
    isVisible,
    onVisibilityChange,
    uuid: randomUUID,
  }), [userId, orgId, live]);
  return useSharedRooms(platform, { initialAgentId, autoSelect: false });
}
