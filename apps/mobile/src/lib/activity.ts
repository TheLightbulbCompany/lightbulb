import { useActivity, type ActivityAction, type ActivityItem, type UseActivityResult } from "@lightbulb/shared";
import type { Href } from "expo-router";

import { notificationHrefs } from "../components/push/NotificationTapRouter";
import { useRoomsPlatform } from "./rooms";

/**
 * The Activity feed, wired to this phone. `visible` is the screen's own focus
 * (ANDed with AppState by the platform): gaining it refreshes the feed. The
 * tab badge mounts one with the default `true`; both share SWR's cache.
 */
export function useMobileActivity(visible = true): UseActivityResult {
  const platform = useRoomsPlatform(visible);
  return useActivity(platform.live.messaging, platform);
}

type Navigate = NonNullable<ActivityAction["navigate"]>;

/**
 * The screens to push for a `navigate` target, reusing the push-tap routes: a
 * thread opens over its room, so back lands in the conversation. Approvals
 * live in the Teams inbox (there is no approval screen on the phone).
 */
export function activityHrefs(target: Navigate): Href[] {
  switch (target.kind) {
    case "room":
      return notificationHrefs({ roomId: target.roomId ?? target.id }) ?? [];
    case "thread":
      return target.roomId
        ? notificationHrefs({ roomId: target.roomId, threadRootMessageId: target.id }) ?? []
        : [];
    case "issue":
      return [{ pathname: "/(app)/teams/issue/[id]", params: { id: target.id } }];
    case "run":
      return [{ pathname: "/(app)/teams/run/[runId]", params: { runId: target.id } }];
    case "approval":
      return ["/(app)/(tabs)/teams"];
    case "session":
      return notificationHrefs({ source: "session", sessionId: target.id }) ?? [];
    case "task":
      return notificationHrefs({ source: "task_question", taskId: target.id }) ?? [];
    default:
      // A link kind a newer server sends that this build doesn't know: stay put.
      return [];
  }
}

/** Where tapping the row goes: the item's own `navigate` action, else its message. */
export function itemTarget(item: ActivityItem): Navigate | null {
  const action = item.actions.find((entry) => entry.navigate)?.navigate;
  if (action) return action;
  if (!item.roomId) return null;
  return item.threadRootMessageId
    ? { kind: "thread", id: item.threadRootMessageId, roomId: item.roomId }
    : { kind: "room", id: item.roomId, roomId: item.roomId };
}
