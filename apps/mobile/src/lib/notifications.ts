import * as Notifications from "expo-notifications";

/**
 * Install the notification handler: every push shows as a banner. No screen
 * shows a live conversation any more, so there is nothing on screen to
 * suppress a banner for.
 */
export function configureNotificationHandler(): void {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
    }),
  });
}
