import { approvalActivityId, interactionActivityId } from "@lightbulb/shared";
import * as Notifications from "expo-notifications";
import { router, type Href } from "expo-router";
import { useEffect } from "react";

import { getCurrentTarget } from "../../lib/activeAgent";
import { parseAgentDeepLink } from "../../lib/deepLink";
import { shouldSuppressBanner, type NotificationData } from "../../lib/notifications";

type TapData = NotificationData & {
  url?: string; source?: string; eventType?: string; issueId?: string;
  approvalId?: string; interactionId?: string; sessionId?: string; taskId?: string;
};

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
  // An approval opens Activity on it: pinned under "Needs you", decided inline.
  if (data?.source === "teams" && data.approvalId) {
    return [{ pathname: "/(app)/(tabs)/activity", params: { focus: approvalActivityId(data.approvalId) } }];
  }
  // So does an agent's question (a thread interaction), answered inline there.
  if (data?.source === "teams" && data.interactionId && data.issueId) {
    return [{ pathname: "/(app)/(tabs)/activity", params: { focus: interactionActivityId(data.issueId, data.interactionId) } }];
  }
  // A super-agent thread's question (task_questions.py push data): its task screen, ahead of the room fallback.
  if (data?.source === "task_question" && data.taskId) {
    return [{ pathname: "/(app)/task/[taskId]", params: { taskId: data.taskId } }];
  }
  // One of your shared sessions is waiting for you or finished.
  if (data?.source === "session" && data.sessionId) {
    return [{ pathname: "/(app)/session/[sessionId]", params: { sessionId: data.sessionId } }];
  }
  if (data?.roomId) {
    // Threads are gone: a reply's notification opens its room.
    return [`/(app)/room/${data.roomId}` as Href];
  }
  const agentId = data?.agentId ?? (data?.url ? parseAgentDeepLink(data.url)?.agentId : undefined);
  return agentId ? [`/agent/${agentId}` as Href] : null;
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
 *
 * It pushes over whatever is open rather than unwinding to the tabs: every
 * pushed screen keeps its composer draft in its own state, and unwinding would
 * throw an unsent message away.
 */
export function NotificationTapRouter() {
  const response = Notifications.useLastNotificationResponse();

  useEffect(() => {
    if (!response) return;
    const id = response.notification.request.identifier;
    Notifications.clearLastNotificationResponse();
    if (id === lastRouted) return;
    lastRouted = id;

    const data = response.notification.request.content.data as TapData | undefined;
    const hrefs = notificationHrefs(data);
    if (!hrefs) return;
    const current = getCurrentTarget();
    // Already on screen: the message arrives live, and a push would stack a
    // second copy of the screen under the reader's back button.
    if (shouldSuppressBanner(current, data ?? {})) return;

    if (data?.roomId && current.roomId === data.roomId) {
      // Inside that room already: go back down to the room.
      router.dismissTo(hrefs[0]);
      return;
    }
    for (const href of hrefs) router.push(href);
  }, [response]);

  return null;
}
