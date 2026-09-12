import * as Notifications from "expo-notifications";

import { sessionIdFromKey } from "@lightbulb/shared";

export interface NotificationTarget {
  agentId: string | null;
  sessionId: string | null;
  roomId: string | null;
  threadId: string | null;
}

export interface NotificationData {
  agentId?: string;
  sessionKey?: string;
  roomId?: string;
  threadRootMessageId?: string | null;
}

/**
 * Suppress the in-app banner only when the exact thing the push is about is
 * already on screen. Room pushes (the projector's) match on roomId AND the
 * thread the reader is in — a thread reply seen from the parent conversation
 * is not on screen, so it still banners. Agent/session pushes keep the
 * pre-rooms rule.
 */
export function shouldSuppressBanner(
  current: NotificationTarget,
  notif: NotificationData,
): boolean {
  if (notif.roomId) {
    return (
      current.roomId === notif.roomId &&
      (current.threadId ?? null) === (notif.threadRootMessageId ?? null)
    );
  }
  if (!notif.agentId || current.agentId !== notif.agentId) return false;
  if (notif.sessionKey === undefined) return true;
  return sessionIdFromKey(notif.sessionKey) === current.sessionId;
}

/**
 * Install the notification handler. `getTarget` returns the conversation (or
 * legacy agent chat) currently on screen, so the handler can suppress the
 * banner for it while showing it everywhere else.
 */
export function configureNotificationHandler(
  getTarget: () => NotificationTarget,
): void {
  Notifications.setNotificationHandler({
    handleNotification: async (notification) => {
      const data = notification.request.content.data as NotificationData | undefined;
      const suppress = shouldSuppressBanner(getTarget(), data ?? {});
      return {
        shouldShowBanner: !suppress,
        shouldShowList: true,
        shouldPlaySound: !suppress,
        shouldSetBadge: false,
      };
    },
  });
}
