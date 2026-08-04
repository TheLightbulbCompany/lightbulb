import * as Notifications from "expo-notifications";

import { sessionIdFromKey } from "@isol8/shared";

/**
 * Suppress the in-app banner only when the app is foregrounded on the exact
 * agent chat AND session the notification is about (the reply already streamed
 * live). A reply on a different session of the same agent is nowhere on
 * screen, so it still banners. Payloads without a sessionKey (legacy) fall
 * back to agent-scoped suppression.
 */
export function shouldSuppressBanner(
  currentAgentId: string | null,
  currentSessionId: string | null,
  notifAgentId: string | undefined,
  notifSessionKey: string | undefined,
): boolean {
  if (!notifAgentId || currentAgentId !== notifAgentId) return false;
  if (notifSessionKey === undefined) return true;
  return sessionIdFromKey(notifSessionKey) === currentSessionId;
}

/**
 * Install the notification handler. `getCurrentChat` returns the agent+session
 * of the chat currently on screen (agentId null when none), so the handler can
 * suppress the banner for the active conversation while showing it everywhere
 * else.
 */
export function configureNotificationHandler(
  getCurrentChat: () => { agentId: string | null; sessionId: string | null },
): void {
  Notifications.setNotificationHandler({
    handleNotification: async (notification) => {
      const data = notification.request.content.data as
        | { agentId?: string; sessionKey?: string }
        | undefined;
      const { agentId, sessionId } = getCurrentChat();
      const suppress = shouldSuppressBanner(agentId, sessionId, data?.agentId, data?.sessionKey);
      return {
        shouldShowBanner: !suppress,
        shouldShowList: true,
        shouldPlaySound: !suppress,
        shouldSetBadge: false,
      };
    },
  });
}
