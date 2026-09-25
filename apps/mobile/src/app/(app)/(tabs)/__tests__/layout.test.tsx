import { render, screen } from "@testing-library/react-native";
import type { ReactNode } from "react";

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed
// and read LAZILY — babel lowers these to `var` and the factories run while
// the layout's own imports are still resolving.
let mockUnread = 0;

// The native tab bar is a UITabBarController; there is nothing to mount in
// jest. This stands in for its SHAPE — one node per trigger, in order — which
// is the only thing this suite asserts about it.
jest.mock("expo-router/unstable-native-tabs", () => {
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
    return <View>{children}</View>;
  }
  NativeTabs.Trigger = Trigger;
  return { NativeTabs };
});
jest.mock("../../../../lib/rooms", () => ({ useMobileRooms: () => ({ rooms: [] }) }));
let mockUnseen = 0;
jest.mock("../../../../lib/activity", () => ({ useMobileActivity: () => ({ attentionCount: mockUnseen }) }));
jest.mock("../../../../lib/unreadBadge", () => ({
  badgeCount: () => mockUnread,
}));
const mockSetBadgeCountAsync = jest.fn(async (_total: number) => true);
jest.mock("expo-notifications", () => ({
  setBadgeCountAsync: (n: number) => mockSetBadgeCountAsync(n),
}));

import TabsLayout from "../_layout";

beforeEach(() => {
  mockUnread = 0;
  mockUnseen = 0;
  mockSetBadgeCountAsync.mockClear();
});

// An iOS tab bar fits four. Teams, Control, Settings and the legacy agent
// route used to be registered here as hidden `href: null` tabs, which is what
// made opening one a TAB SWITCH; they are pushed screens on the (app) stack
// now and must not come back as triggers.
it("registers exactly the four real tabs, in Slack's order", async () => {
  await render(<TabsLayout />);
  expect(screen.getAllByTestId(/^tab-/).map((node) => node.props.testID)).toEqual([
    "tab-home",
    "tab-dms",
    "tab-activity",
    "tab-more",
  ]);
});

// The one thing NativeTabs had to be able to express beyond labels and icons.
it("hides Home's badge at zero and shows the count above it", async () => {
  await render(<TabsLayout />);
  expect(screen.queryByTestId("tab-badge")).toBeNull();

  mockUnread = 7;
  await render(<TabsLayout />);
  expect(screen.getByTestId("tab-badge")).toHaveTextContent("7");
});

// The same number belongs on the app icon, because the projector sends no
// `badge` in its push payload.
it("mirrors the count onto the app icon", async () => {
  mockUnread = 3;
  await render(<TabsLayout />);
  expect(mockSetBadgeCountAsync).toHaveBeenCalledWith(3);
});

it("badges Activity with its unseen count, and nothing at zero", async () => {
  await render(<TabsLayout />);
  expect(screen.queryByTestId("tab-badge")).toBeNull();

  mockUnseen = 4;
  await render(<TabsLayout />);
  expect(screen.getByTestId("tab-activity")).toHaveTextContent("Activity4");
  expect(screen.getByTestId("tab-home")).not.toHaveTextContent(/\d/);
});
