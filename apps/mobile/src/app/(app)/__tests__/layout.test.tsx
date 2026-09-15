import { render } from "@testing-library/react-native";
import type { ReactNode } from "react";

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed
// and read LAZILY — babel lowers these to `var` and the factories run while
// the layout's own imports are still resolving.
type ScreenOptions = { title?: string };
type ScreenProps = {
  name: string;
  options?: ScreenOptions | ((arg: { route: unknown }) => ScreenOptions);
};
const mockScreens: ScreenProps[] = [];
jest.mock("expo-router", () => {
  const { View } = require("react-native");
  function Stack({ children }: { children?: ReactNode }) {
    return <View>{children}</View>;
  }
  function StackScreen(props: ScreenProps) {
    mockScreens.push(props);
    return null;
  }
  function Redirect() {
    return null;
  }
  Stack.Screen = StackScreen;
  return { Stack, Redirect };
});
// The focused tab name, as the real helper would report it off the tab
// navigator's state (undefined before it has any).
let mockFocusedTab: string | undefined;
jest.mock("expo-router/react-navigation", () => ({
  getFocusedRouteNameFromRoute: () => mockFocusedTab,
}));
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ isSignedIn: true, isLoaded: true }),
}));
// Consent is still being read from the keychain until a test says otherwise.
let mockConsentGranted = true;
jest.mock("../../../components/consent/AiConsentGate", () => ({
  AiConsentGate: ({ children }: { children?: ReactNode }) => (mockConsentGranted ? children : null),
}));
jest.mock("../../../components/provision/ProvisionGate", () => ({
  ProvisionGate: ({ children }: { children?: ReactNode }) => children,
}));
let mockPushRegistrations = 0;
let mockTapRouters = 0;
jest.mock("../../../components/push/PushRegistration", () => ({
  PushRegistration: () => {
    mockPushRegistrations += 1;
    return null;
  },
}));
jest.mock("../../../components/push/NotificationTapRouter", () => ({
  NotificationTapRouter: () => {
    mockTapRouters += 1;
    return null;
  },
}));
jest.mock("../../../providers/live-events", () => ({
  LiveEventsProvider: ({ children }: { children?: ReactNode }) => children,
}));

import AppLayout, { unstable_settings } from "../_layout";

const names = () => mockScreens.map((screen) => screen.name);
const titleOf = (name: string) => {
  const found = mockScreens.find((screen) => screen.name === name);
  return typeof found?.options === "function"
    ? found.options({ route: {} }).title
    : found?.options?.title;
};

beforeEach(() => {
  mockScreens.length = 0;
  mockFocusedTab = undefined;
  mockConsentGranted = true;
  mockPushRegistrations = 0;
  mockTapRouters = 0;
});

// The defect this restructure fixes: a conversation used to be a HIDDEN TAB
// containing its own Stack, so opening one was a tab switch — expo-router's JS
// tabs render those with `animation: 'none'`, which is why there was no
// slide-in, no edge-swipe back and no native back button. Both room screens
// being siblings of `(tabs)` on this Stack is what makes `router.push` a real
// push OVER the tab bar. (home.test.tsx asserts the path it pushes.)
it("owns both room screens as pushed Stack screens, not tabs", async () => {
  await render(<AppLayout />);
  expect(names()).toEqual(
    expect.arrayContaining(["room/[roomId]/index", "room/[roomId]/thread/[messageId]"]),
  );
  expect(names()).toContain("(tabs)");
  // The four tabs belong to the (tabs) group's own layout. A `room` screen here
  // would mean the old hidden-tab shape had come back.
  for (const tab of ["home", "dms", "activity", "more", "room"]) {
    expect(names()).not.toContain(tab);
  }
});

// Teams and Control were nested Stacks inside hidden tabs. Folded in here, a
// push from More lands on a normally pushed screen with a native back button.
it("owns the Teams, Control, Settings and legacy agent routes", async () => {
  await render(<AppLayout />);
  expect(names()).toEqual(
    expect.arrayContaining([
      "teams/index",
      "teams/issue/[id]",
      "control/index",
      "control/listing/[id]",
      "settings",
      "agent/[agentId]",
    ]),
  );
  expect(titleOf("teams/index")).toBe("Teams");
  expect(titleOf("settings")).toBe("Settings");
});

// A cold-start push deep link (`/(app)/room/<id>`, or its thread) has to build
// the stack as [(tabs), room] so back has somewhere to go. That is the whole
// reason the custom HeaderBack chevron could be deleted.
it("anchors the stack on the tabs so a cold-start deep link can go back", () => {
  expect(unstable_settings.anchor).toBe("(tabs)");
});

// The four tab roots share this Stack's header — a nested navigator cannot set
// its parent's options — so the title has to follow the focused tab, and fall
// back to Home for the first render, before the tab navigator has state.
it("titles the tabs header from the focused tab", async () => {
  await render(<AppLayout />);
  expect(titleOf("(tabs)")).toBe("Home");

  mockFocusedTab = "dms";
  mockScreens.length = 0;
  await render(<AppLayout />);
  expect(titleOf("(tabs)")).toBe("DMs");
});

// The cold-start loop: a tap routed while the gates still hid the Stack made
// expo-router push a second copy of the whole (app) group onto the root stack,
// whose own router pushed again. Token registration may run behind the gates;
// tap routing must not.
it("routes notification taps only once the gated Stack is mounted", async () => {
  mockConsentGranted = false;
  await render(<AppLayout />);
  expect(mockPushRegistrations).toBeGreaterThan(0);
  expect(mockTapRouters).toBe(0);

  mockConsentGranted = true;
  await render(<AppLayout />);
  expect(mockTapRouters).toBeGreaterThan(0);
});
