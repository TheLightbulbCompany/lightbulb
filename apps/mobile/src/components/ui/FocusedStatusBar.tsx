import { useIsFocused } from "expo-router";
import { StatusBar } from "expo-status-bar";

/**
 * `<StatusBar style="auto" />`, but only while this screen is focused.
 *
 * The app root pins the status bar to `dark` (`app/_layout.tsx`) because most
 * screens still read the light-only `colors` palette — a global `auto` would
 * make the bar light-on-white on every one of them whenever the phone is in
 * Dark Mode. A redesigned, dark-capable screen (`useChatPalette`) mounts this
 * instead: expo-router keeps sibling tab screens mounted, so without the
 * focus check an unfocused screen's own `<StatusBar>` would keep fighting
 * whichever tab is actually on screen.
 */
export function FocusedStatusBar() {
  const focused = useIsFocused();
  if (!focused) return null;
  return <StatusBar style="auto" />;
}
