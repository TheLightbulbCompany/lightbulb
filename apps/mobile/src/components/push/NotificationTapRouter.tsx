import * as Notifications from "expo-notifications";
import { router, type Href } from "expo-router";
import { useEffect } from "react";

type TapData = {
  url?: string; source?: string; roomId?: string; agentId?: string;
  approvalId?: string; interactionId?: string; issueId?: string; sessionId?: string; taskId?: string;
};

const HOME = "/(app)/(tabs)/chats" as Href;
const NEEDS_YOU = "/(app)/(tabs)/activity" as Href;

/**
 * Which tab a tapped notification opens. Anything that asks the person to decide
 * (an approval, an agent's question, a task's question) opens Needs you; any other
 * conversation push (a room, a session, a legacy agent link) opens Home. `source` is
 * required for the Teams kinds so an unrelated payload carrying an `issueId` cannot
 * claim the route.
 */
export function notificationHref(data: TapData | undefined): Href | null {
  if (data?.source === "teams" && (data.approvalId || (data.interactionId && data.issueId))) return NEEDS_YOU;
  if (data?.source === "task_question" && data.taskId) return NEEDS_YOU;
  if (data?.roomId || data?.agentId || (data?.source === "session" && data.sessionId) || data?.url) return HOME;
  return null;
}

/** The last tap routed, by notification id. A tap landing while this mounts
 * is read from the stored response AND delivered by the native event after the
 * clear below — it must route once. */
let lastRouted: string | undefined;

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
    const id = response.notification.request.identifier;
    Notifications.clearLastNotificationResponse();
    if (id === lastRouted) return;
    lastRouted = id;

    const href = notificationHref(response.notification.request.content.data as TapData | undefined);
    if (href) router.navigate(href);
  }, [response]);

  return null;
}
