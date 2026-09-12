import { useAuth, useOrganization } from "@clerk/clerk-expo";
import { useRooms, type RoomsPlatform, type UseRoomsOptions, type UseRoomsResult } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { useCallback, useMemo, useRef } from "react";
import { AppState } from "react-native";

import { useLiveEvents } from "../providers/live-events";

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
  const onVisibilityChange = useCallback((listener: () => void) => {
    const subscription = AppState.addEventListener("change", () => listener());
    return () => subscription.remove();
  }, []);
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
  return useRooms(useRoomsPlatform(visible), roomOptions);
}

/**
 * Authenticated image bytes → a data URI RN's <Image> can render. Same
 * FileReader rail teamsData.fetchAttachmentDataUri already uses.
 * ponytail: no cache; add an LRU keyed by attachmentId if scrolling a
 * photo-heavy room re-downloads noticeably.
 */
export function blobToDataUri(blob: Blob): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(typeof reader.result === "string" ? reader.result : null);
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(blob);
  });
}
