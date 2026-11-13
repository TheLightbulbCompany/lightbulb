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
let mockSignedIn = true;
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ isSignedIn: mockSignedIn, isLoaded: true }),
}));
const mockClearRoomsCache = jest.fn();
jest.mock("../../../lib/roomsCache", () => ({
  clearRoomsCache: () => mockClearRoomsCache(),
}));
// Consent is still being read from the keychain until a test says otherwise.
let mockConsentGranted = true;
jest.mock("../../../components/consent/AiConsentGate", () => ({
  AiConsentGate: ({ children }: { children?: ReactNode }) => (mockConsentGranted ? children : null),
}));
jest.mock("../../../components/billing/PurchaseListener", () => ({
  PurchaseListener: () => null,
}));
jest.mock("../../../components/billing/IapProvider", () => ({
  IapProvider: ({ children }: { children?: ReactNode }) => children,
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
let mockLiveEventsMounts = 0;
jest.mock("../../../providers/live-events", () => ({
  LiveEventsProvider: ({ children }: { children?: ReactNode }) => {
    mockLiveEventsMounts += 1;
    return children;
  },
}));
jest.mock("../../../providers/live", () => ({ LiveProvider: ({ children }: { children?: ReactNode }) => children }));
jest.mock("../../../components/voice/LiveSheet", () => ({ LiveSheet: () => null }));
// null = the session has no active workspace yet (the gate shows its chooser).
let mockWorkspaceReady = true;
jest.mock("../../../components/workspace/NameGate", () => ({
  NameGate: ({ children }: { children?: ReactNode }) => children,
}));
let mockWorkspaceGateRenders = 0;
jest.mock("../../../components/workspace/WorkspaceGate", () => ({
  WorkspaceGate: ({ children }: { children?: ReactNode }) => {
    mockWorkspaceGateRenders += 1;
    return mockWorkspaceReady ? children : null;
  },
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
  mockSignedIn = true;
  mockClearRoomsCache.mockClear();
  mockScreens.length = 0;
  mockFocusedTab = undefined;
  mockConsentGranted = true;
  mockPushRegistrations = 0;
  mockTapRouters = 0;
  mockLiveEventsMounts = 0;
  mockWorkspaceReady = true;
  mockWorkspaceGateRenders = 0;
});

// The four tabs belong to the (tabs) group's own layout; the one pushed screen
// it names is the text chat. The old chat app's screens (threads, sessions,
// tasks, Control) are gone.
it("registers the tab group and the chat screen, nothing else", async () => {
  await render(<AppLayout />);
  expect(names()).toEqual(["(tabs)", "room/[roomId]/index"]);
});

// A cold-start deep link (`/(app)/room/<id>`) has to build the stack as
// [(tabs), room] so back has somewhere to go.
it("anchors the stack on the tabs so a cold-start deep link can go back", () => {
  expect(unstable_settings.anchor).toBe("(tabs)");
});

// The four tab roots share this Stack's header — a nested navigator cannot set
// its parent's options — so the title has to follow the focused tab, and fall
// back to Chats for the first render, before the tab navigator has state.
it("titles the tabs header from the focused tab", async () => {
  await render(<AppLayout />);
  expect(titleOf("(tabs)")).toBe("Home");

  mockFocusedTab = "teams";
  mockScreens.length = 0;
  await render(<AppLayout />);
  expect(titleOf("(tabs)")).toBe("Agent");
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

// F4: every product API 409s without an active workspace, so nothing that calls
// one — the live-events config read, the gates, the tabs — may mount before the
// workspace gate passes. Consent still comes first (App Store 5.1.1(i)).
it("mounts nothing that calls the API until a workspace is active, and asks for consent first", async () => {
  mockWorkspaceReady = false;
  await render(<AppLayout />);
  expect(mockWorkspaceGateRenders).toBeGreaterThan(0);
  expect(mockLiveEventsMounts).toBe(0);
  expect(names()).toEqual([]);
  // Push tokens are per user, not per workspace: registration is not held back.
  expect(mockPushRegistrations).toBeGreaterThan(0);

  mockConsentGranted = false;
  mockWorkspaceGateRenders = 0;
  await render(<AppLayout />);
  expect(mockWorkspaceGateRenders).toBe(0);
});

// Every sign-out — Settings, the consent decline, the gates' escape hatches, an
// expired session — lands here signed out, so this one place deletes the
// persisted rooms and messages before anyone else can sign in on the phone.
it("deletes the persisted rooms cache whenever the session is signed out", async () => {
  await render(<AppLayout />);
  expect(mockClearRoomsCache).not.toHaveBeenCalled();

  mockSignedIn = false;
  await render(<AppLayout />);
  expect(mockClearRoomsCache).toHaveBeenCalledTimes(1);
});
