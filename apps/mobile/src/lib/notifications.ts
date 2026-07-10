import * as Notifications from "expo-notifications";

/**
 * Suppress the in-app banner only when the app is foregrounded on the exact
 * agent chat the notification is about (the reply already streamed live).
 */
export function shouldSuppressBanner(
  currentAgentId: string | null,
  notifAgentId: string | undefined,
): boolean {
  return !!notifAgentId && currentAgentId === notifAgentId;
}

/**
 * Install the notification handler. `getCurrentAgentId` returns the agentId of
 * the chat currently on screen (or null), so the handler can suppress the
 * banner for the active session while showing it everywhere else.
 */
export function configureNotificationHandler(getCurrentAgentId: () => string | null): void {
  Notifications.setNotificationHandler({
    handleNotification: async (notification) => {
      const notifAgentId = notification.request.content.data?.agentId as string | undefined;
      const suppress = shouldSuppressBanner(getCurrentAgentId(), notifAgentId);
      return {
        shouldShowBanner: !suppress,
        shouldShowList: true,
        shouldPlaySound: !suppress,
        shouldSetBadge: false,
      };
    },
  });
}
