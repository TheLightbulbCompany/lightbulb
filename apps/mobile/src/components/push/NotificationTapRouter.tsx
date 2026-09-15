import * as Notifications from "expo-notifications";
import { router, type Href } from "expo-router";
import { useEffect } from "react";

import { getCurrentTarget } from "../../lib/activeAgent";
import { parseAgentDeepLink } from "../../lib/deepLink";
import type { NotificationData } from "../../lib/notifications";

type TapData = NotificationData & { url?: string; source?: string; issueId?: string };

/**
 * Where a tapped notification lands, as the screens to push over the tabs.
 *
 * Teams is checked first: its pushes deliberately carry no `agentId` (that id
 * is a Paperclip uuid — see `push_data` in routers/paperclip_events.py), and
 * `source` is required so an unrelated payload carrying an `issueId` cannot
 * claim the route. A thread reply opens its room underneath the thread, the
 * same hierarchy an in-app "Reply in thread" builds, so back goes to the room.
 */
export function notificationHrefs(data: TapData | undefined): Href[] | null {
  if (data?.source === "teams" && data.issueId) {
    return [{ pathname: "/(app)/teams/issue/[id]", params: { id: data.issueId } }];
  }
  if (data?.roomId) {
    const room = `/(app)/room/${data.roomId}` as Href;
    return data.threadRootMessageId
      ? [room, `/(app)/room/${data.roomId}/thread/${data.threadRootMessageId}` as Href]
      : [room];
  }
  const agentId = data?.agentId ?? (data?.url ? parseAgentDeepLink(data.url)?.agentId : undefined);
  return agentId ? [`/agent/${agentId}` as Href] : null;
}

/**
 * Routes a tapped notification — the one that cold-started the app, or one
 * tapped while it was backgrounded.
 *
 * Mounted beside the signed-in Stack, NOT above the gates. A push dispatched
 * before that Stack exists makes expo-router push a second copy of the whole
 * `(app)` group onto the ROOT stack; that copy mounts its own router, which
 * re-read the (never cleared) last response and pushed again — a loop of app
 * copies that showed as a blank launch, or as one extra back press per copy.
 *
 * `useLastNotificationResponse` is the single source for both paths, and the
 * response is cleared once handled: the native module otherwise keeps
 * returning the last tap for the life of the process.
 */
export function NotificationTapRouter() {
  const response = Notifications.useLastNotificationResponse();

  useEffect(() => {
    if (!response) return;
    Notifications.clearLastNotificationResponse();
    const data = response.notification.request.content.data as TapData | undefined;
    const hrefs = notificationHrefs(data);
    if (!hrefs) return;

    // Already reading it: the message arrives live, and pushing would stack a
    // second copy of the screen under the reader's back button.
    const current = getCurrentTarget();
    if (
      data?.roomId &&
      current.roomId === data.roomId &&
      current.threadId === (data.threadRootMessageId ?? null)
    ) {
      return;
    }

    // Land on exactly [tabs, ...target], whatever was open when the app
    // backgrounded, so the back button always leads home from the target.
    if (router.canDismiss()) router.dismissAll();
    for (const href of hrefs) router.push(href);
  }, [response]);

  return null;
}
