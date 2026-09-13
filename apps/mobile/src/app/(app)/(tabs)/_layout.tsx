import * as Notifications from "expo-notifications";
import { NativeTabs } from "expo-router/unstable-native-tabs";
import { useEffect, useState } from "react";

import { colors } from "../../../config/theme";
import { useMobileRooms } from "../../../lib/rooms";
import { badgeCount, useChannelMentions } from "../../../lib/unreadBadge";

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
  const mentions = useChannelMentions(rooms);
  const total = badgeCount(rooms, mentions);
  useEffect(() => {
    onCount(total);
    void Notifications.setBadgeCountAsync(total).catch(() => {});
  }, [onCount, total]);
  return null;
}

/**
 * Slack's four: Home · DMs · Activity · More.
 *
 * `NativeTabs` (react-native-screens' UITabBarController) rather than the JS
 * tab bar: on iOS 26 that is the real glass tab bar, with SF-font labels, SF
 * Symbol glyphs and the system's own minimize-on-scroll behaviour — none of
 * which a JS bar drawn in Source Sans 3 with a Feather icon font can imitate.
 * It carries everything the old bar did: per-tab icons, and Home's unread badge.
 *
 * There is no header here. This group is ONE screen of the (app) stack, and
 * that stack draws the header for all four roots (see `TAB_TITLES`), which is
 * also what keeps the tab content below the status bar.
 *
 * Teams, Control, Settings and the legacy agent route used to be registered
 * here as hidden tabs. They are pushed screens on the (app) stack now — `more`
 * is the door to the first three.
 */
export default function TabsLayout() {
  const [unread, setUnread] = useState(0);
  return (
    <>
      <TabBadge onCount={setUnread} />
      <NativeTabs tintColor={colors.forest}>
        <NativeTabs.Trigger name="home">
          <NativeTabs.Trigger.Icon sf={{ default: "house", selected: "house.fill" }} />
          <NativeTabs.Trigger.Label>Home</NativeTabs.Trigger.Label>
          <NativeTabs.Trigger.Badge hidden={unread === 0}>
            {String(unread)}
          </NativeTabs.Trigger.Badge>
        </NativeTabs.Trigger>
        <NativeTabs.Trigger name="dms">
          <NativeTabs.Trigger.Icon sf={{ default: "message", selected: "message.fill" }} />
          <NativeTabs.Trigger.Label>DMs</NativeTabs.Trigger.Label>
        </NativeTabs.Trigger>
        <NativeTabs.Trigger name="activity">
          <NativeTabs.Trigger.Icon sf="at" />
          <NativeTabs.Trigger.Label>Activity</NativeTabs.Trigger.Label>
        </NativeTabs.Trigger>
        <NativeTabs.Trigger name="more">
          <NativeTabs.Trigger.Icon
            sf={{ default: "ellipsis.circle", selected: "ellipsis.circle.fill" }}
          />
          <NativeTabs.Trigger.Label>More</NativeTabs.Trigger.Label>
        </NativeTabs.Trigger>
      </NativeTabs>
    </>
  );
}
