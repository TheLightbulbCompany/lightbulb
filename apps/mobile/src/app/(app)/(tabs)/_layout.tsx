import { useAuth } from "@clerk/expo";
import * as Notifications from "expo-notifications";
import { NativeTabs } from "expo-router/unstable-native-tabs";
import { useEffect, useState } from "react";
import { useColorScheme } from "react-native";

import { chatPalette } from "../../../config/theme";
import { useMobileActivity } from "../../../lib/activity";
import { useMobileRooms } from "../../../lib/rooms";
import { badgeCount } from "../../../lib/unreadBadge";

/**
 * The unread number, Slack's way: DMs and mentions only, muted rooms excluded.
 * Renders nothing — it lifts the count out to the layout, which is the
 * component that owns the tab's badge.
 *
 * It also owns the app-icon badge, because the same number belongs on both and
 * the projector sends no `badge` in its push payload.
 */
function TabBadge({ onCount }: { onCount: (total: number) => void }) {
  const { rooms } = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const total = badgeCount(rooms);
  useEffect(() => {
    onCount(total);
    void Notifications.setBadgeCountAsync(total).catch(() => {});
  }, [onCount, total]);
  return null;
}

/** Activity's attention count (the server's, capped at 99: open approvals and failed
 *  runs always, the rest while unseen), lifted to the layout like TabBadge's. */
function ActivityBadge({ onCount }: { onCount: (total: number) => void }) {
  const { attentionCount } = useMobileActivity();
  useEffect(() => onCount(attentionCount), [onCount, attentionCount]);
  return null;
}

/**
 * Home · Agent · Needs you · Settings. Home keeps typed conversations alongside Live;
 * the route names remain stable for notification links.
 *
 * `NativeTabs` (react-native-screens' UITabBarController) rather than the JS
 * tab bar: on iOS 26 that is the real glass tab bar, with SF-font labels, SF
 * Symbol glyphs and the system's own minimize-on-scroll behaviour — none of
 * which a JS bar drawn in Satoshi with a Feather icon font can imitate.
 * It carries everything the old bar did: per-tab icons, and Chats' unread badge.
 *
 * There is no header here. This group is ONE screen of the (app) stack, and
 * that stack draws the header for all four roots (see `TAB_TITLES`), which is
 * also what keeps the tab content below the status bar.
 *
 * Control and the legacy agent route stay pushed screens on the (app) stack.
 */
export default function TabsLayout() {
  const [unread, setUnread] = useState(0);
  const [unseen, setUnseen] = useState(0);
  // Chats' session/presence state and Teams' and Settings' data hooks all
  // hold workspace-scoped local state with no scope of their own; the org
  // switch itself doesn't remount this group (see (app)/_layout.tsx). Keying
  // the whole navigator on the active org forces a fresh mount of every tab
  // screen — and everything they hold — on a switch, instead of showing the
  // old workspace's rows/dashboard until each screen's own refetch lands.
  const { orgId } = useAuth();
  // The selected tab reads in both schemes (forest green vanished on dark).
  const tint = chatPalette[useColorScheme() === "dark" ? "dark" : "light"].fg;
  return (
    <>
      <TabBadge onCount={setUnread} />
      <ActivityBadge onCount={setUnseen} />
      <NativeTabs key={orgId ?? "none"} tintColor={tint}>
        <NativeTabs.Trigger name="chats">
          <NativeTabs.Trigger.Icon sf={{ default: "house", selected: "house.fill" }} md="home" />
          <NativeTabs.Trigger.Label>Home</NativeTabs.Trigger.Label>
          {/* No Badge element at all at zero, not a "0" child and not
              `hidden`: expo-router sets `badgeValue = String(children)` for
              any truthy child and only consults `hidden` when there is none
              (`appendBadgeOptions`), so "0" drew a "0" badge. */}
          {unread > 0 && (
            <NativeTabs.Trigger.Badge>{String(unread)}</NativeTabs.Trigger.Badge>
          )}
        </NativeTabs.Trigger>
        <NativeTabs.Trigger name="teams">
          <NativeTabs.Trigger.Icon sf={{ default: "sparkles", selected: "sparkles" }} md="auto_awesome" />
          <NativeTabs.Trigger.Label>Agent</NativeTabs.Trigger.Label>
        </NativeTabs.Trigger>
        <NativeTabs.Trigger name="activity">
          <NativeTabs.Trigger.Icon sf="tray" md="inbox" />
          <NativeTabs.Trigger.Label>Needs you</NativeTabs.Trigger.Label>
          {/* Same zero rule as Home's badge above. */}
          {unseen > 0 && (
            <NativeTabs.Trigger.Badge>{String(unseen)}</NativeTabs.Trigger.Badge>
          )}
        </NativeTabs.Trigger>
        <NativeTabs.Trigger name="settings">
          <NativeTabs.Trigger.Icon sf={{ default: "gearshape", selected: "gearshape.fill" }} md="settings" />
          <NativeTabs.Trigger.Label>Settings</NativeTabs.Trigger.Label>
        </NativeTabs.Trigger>
      </NativeTabs>
    </>
  );
}
