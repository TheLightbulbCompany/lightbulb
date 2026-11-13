import { render, screen, within } from "@testing-library/react-native";
import type { ReactNode } from "react";

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed
// and read LAZILY — babel lowers these to `var` and the factories run while
// the layout's own imports are still resolving.
let mockOrgId: string | null = "org-a";
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ orgId: mockOrgId }) }));

// The native tab bar is a UITabBarController; there is nothing to mount in
// jest. This stands in for its SHAPE — one node per trigger, in order — which
// is the only thing this suite asserts about it.
let mockNativeTabsMounts = 0;
jest.mock("expo-router/unstable-native-tabs", () => {
  const { useEffect } = require("react");
  const { Text, View } = require("react-native");
  function Trigger({ name, children }: { name: string; children?: ReactNode }) {
    return <View testID={`tab-${name}`}>{children}</View>;
  }
  function TriggerIcon() {
    return null;
  }
  function TriggerLabel({ children }: { children?: ReactNode }) {
    return <Text>{children}</Text>;
  }
  // Mirrors expo-router's `appendBadgeOptions`: a truthy child ALWAYS draws
  // (`badgeValue = String(children)`, `hidden` not consulted); only a
  // childless badge honours `hidden`. A `"0"` child is a "0" badge on device.
  function TriggerBadge({ hidden, children }: { hidden?: boolean; children?: ReactNode }) {
    if (children) return <Text testID="tab-badge">{String(children)}</Text>;
    return hidden ? null : <Text testID="tab-badge"> </Text>;
  }
  Trigger.Icon = TriggerIcon;
  Trigger.Label = TriggerLabel;
  Trigger.Badge = TriggerBadge;
  function NativeTabs({ children }: { children?: ReactNode }) {
    // Counts mounts, not renders: proves whether a `key` change actually threw
    // the previous instance (and every tab's state under it) away.
    useEffect(() => {
      mockNativeTabsMounts += 1;
    }, []);
    return <View>{children}</View>;
  }
  NativeTabs.Trigger = Trigger;
  return { NativeTabs };
});
let mockUnseen = 0;
jest.mock("../../../../components/tasks/NeedsYouCard", () => ({
  useNeedsYou: () => ({ items: Array.from({ length: mockUnseen }, (_, i) => ({ id: `q${i}` })) }),
}));
const mockSetBadgeCountAsync = jest.fn(async (_total: number) => true);
jest.mock("expo-notifications", () => ({
  setBadgeCountAsync: (n: number) => mockSetBadgeCountAsync(n),
}));

import TabsLayout from "../_layout";

beforeEach(() => {
  mockUnseen = 0;
  mockOrgId = "org-a";
  mockNativeTabsMounts = 0;
  mockSetBadgeCountAsync.mockClear();
});

it("registers Home · Agent · Needs you · Settings", async () => {
  await render(<TabsLayout />);
  expect(screen.getAllByTestId(/^tab-/).map((node) => node.props.testID)).toEqual([
    "tab-chats",
    "tab-teams",
    "tab-activity",
    "tab-settings",
  ]);
});

it("labels the assistant configuration tab Agent", async () => {
  await render(<TabsLayout />);
  expect(within(screen.getByTestId("tab-teams")).getByText("Agent")).toBeTruthy();
});

// The same number belongs on the app icon, because the projector sends no
// `badge` in its push payload.
it("badges Needs you with its open Questions, mirrors it onto the app icon, and draws nothing at zero", async () => {
  await render(<TabsLayout />);
  expect(screen.queryByTestId("tab-badge")).toBeNull();

  mockUnseen = 4;
  await render(<TabsLayout />);
  expect(screen.getByTestId("tab-activity")).toHaveTextContent("Needs you4");
  expect(screen.getByTestId("tab-chats")).not.toHaveTextContent(/\d/);
  expect(mockSetBadgeCountAsync).toHaveBeenCalledWith(4);
});

// A workspace switch must throw away every tab's mounted state rather than leave the
// old workspace's rows on screen until each screen's own refetch lands.
// Keying the navigator on the active org is how: this proves the key
// actually changes (and only then) by counting NativeTabs mounts.
it("remounts the tab navigator when the active org changes, but not on an unrelated rerender", async () => {
  const { rerender } = await render(<TabsLayout />);
  expect(mockNativeTabsMounts).toBe(1);

  // Same org: an unrelated rerender must not throw every tab's state away.
  await rerender(<TabsLayout />);
  expect(mockNativeTabsMounts).toBe(1);

  // Different org: the key changes, forcing a fresh mount.
  mockOrgId = "org-b";
  await rerender(<TabsLayout />);
  expect(mockNativeTabsMounts).toBe(2);
});
