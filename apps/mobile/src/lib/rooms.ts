import { useAuth, useOrganization } from "@clerk/expo";
import { useRooms, type RoomsPlatform, type UseRoomsOptions, type UseRoomsResult } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { useCallback, useEffect, useMemo, useRef } from "react";
import { AppState } from "react-native";

import { useLiveEvents } from "../providers/live-events";
import { roomsCache } from "./roomsCache";

/**
 * The phone's half of RoomsPlatform.
 *
 * `visible` is the SCREEN's own focus (expo-router blurs a pushed screen
 * without backgrounding the app), ANDed with AppState — the shared hook's
 * read-marking rule is "on screen and in the foreground", and on a phone
 * those are two different facts.
 */
export function useRoomsPlatform(visible = true): RoomsPlatform {
  const { userId } = useAuth();
  const { organization } = useOrganization();
  const live = useLiveEvents();
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const isVisible = useCallback(
    () => visibleRef.current && AppState.currentState === "active",
    [],
  );
  const listeners = useRef(new Set<() => void>());
  const onVisibilityChange = useCallback((listener: () => void) => {
    listeners.current.add(listener);
    const subscription = AppState.addEventListener("change", () => listener());
    return () => {
      listeners.current.delete(listener);
      subscription.remove();
    };
  }, []);
  // The screen's own focus is the other half of isVisible(), so a change in it
  // has to notify too — AppState alone leaves a room that was blurred by a
  // pushed thread screen never re-marking read on the way back. Firing on
  // mount is a no-op: the shared hook subscribes in a later effect and calls
  // markVisibleRead() itself there, so the set is still empty here.
  useEffect(() => {
    for (const listener of listeners.current) listener();
  }, [visible]);
  return useMemo(() => ({
    userId: userId ?? null,
    orgId: organization?.id ?? null,
    live,
    isVisible,
    onVisibilityChange,
    uuid: () => randomUUID(),
  }), [userId, organization?.id, live, isVisible, onVisibilityChange]);
}

/** The room machine, wired to this phone. */
export function useMobileRooms(
  options: UseRoomsOptions & { visible?: boolean } = {},
): UseRoomsResult {
  const { visible = true, ...roomOptions } = options;
  // Persisted per user + workspace, so a cold start paints the last-known list.
  return useRooms(useRoomsPlatform(visible), { persistence: roomsCache, ...roomOptions });
}
