import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

const mockReplace = jest.fn();
jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { replace: (...args: unknown[]) => mockReplace(...args) },
    Stack: { Screen: () => null },
    useFocusEffect: (callback: () => void | (() => void)) => useEffect(callback, [callback]),
  };
});
jest.mock("@clerk/clerk-expo", () => ({ useAuth: () => ({ userId: "user-a" }) }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "33333333-3333-4333-8333-333333333333" }));
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }) }));
jest.mock("../../../lib/keyboardOffset", () => ({ usePushedScreenKeyboardOffset: () => 0 }));
jest.mock("../../../components/ui/SegmentedControl", () => ({
  SegmentedControl: ({ segments, onChange }: { segments: { key: string; label: string }[]; onChange: (key: string) => void }) => {
    const { Pressable, Text, View } = require("react-native");
    return <View>{segments.map((item) => <Pressable key={item.key} onPress={() => onChange(item.key)}><Text>{item.label}</Text></Pressable>)}</View>;
  },
}));

const mockGet = jest.fn();
const mockPost = jest.fn();
const mockApi = { get: (...args: unknown[]) => mockGet(...args), post: (...args: unknown[]) => mockPost(...args) };
jest.mock("../../../lib/desktopSessions", () => ({
  useDesktopSessionApi: () => mockApi,
  sessionStartError: () => "Could not reach your Mac.",
  sessionOutcomeError: () => "The Mac did not start the session.",
}));
jest.mock("../../../lib/rooms", () => ({ useMobileRooms: () => ({ resourceSource: {} }) }));
let mockSessions: { sessionId: string; deviceId: string; humanUserId: string; provider: string }[] = [];
const mockRefresh = jest.fn();
jest.mock("@lightbulb/shared", () => ({
  useWorkspaceSessions: () => ({ sessions: mockSessions, loading: false, error: null, refresh: mockRefresh }),
}));
const mockOnEvent = jest.fn((_handler: unknown) => () => {});
const mockLive = { onEvent: (handler: unknown) => mockOnEvent(handler) };
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import NewSessionScreen from "../session/new";

const targets = { enabled: true, devices: [{ deviceId: "mac-1", generation: "gen-1", name: "Your Mac" }] };
const options = { name: "Alice's Mac", sharing: true, folders: ["/Users/alice/code"], tools: [{ id: "claude", label: "Claude Code" }, { id: "codex", label: "Codex" }] };

beforeEach(() => {
  mockSessions = [{ sessionId: "old", deviceId: "mac-1", humanUserId: "user-a", provider: "claude" }];
  mockReplace.mockClear();
  mockGet.mockReset();
  mockPost.mockReset();
  mockOnEvent.mockClear();
  mockGet.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? targets : options);
  mockPost.mockResolvedValue({ startId: "33333333-3333-4333-8333-333333333333" });
});

it("starts an approved Mac session and opens only its newly registered session", async () => {
  const view = await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByText("Claude Code")).toBeTruthy());
  await fireEvent.changeText(screen.getByLabelText("First message"), "Fix the build safely.");
  await waitFor(() => expect(screen.getByLabelText("Start session").props.accessibilityState.disabled).toBe(false));
  await fireEvent.press(screen.getByLabelText("Start session"));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/desktop-sessions/start", {
    startId: "33333333-3333-4333-8333-333333333333",
    deviceId: "mac-1", generation: "gen-1", tool: "claude", folder: "/Users/alice/code",
    model: null, mode: null, prompt: "Fix the build safely.",
  }));
  expect(mockReplace).not.toHaveBeenCalled();
  mockSessions = [...mockSessions,
    { sessionId: "theirs", deviceId: "mac-1", humanUserId: "user-b", provider: "claude" },
    { sessionId: "other-mac", deviceId: "mac-2", humanUserId: "user-a", provider: "claude" },
  ];
  await view.rerender(<NewSessionScreen />);
  expect(mockReplace).not.toHaveBeenCalled();
  mockSessions = [...mockSessions, { sessionId: "sess-new", deviceId: "mac-1", humanUserId: "user-a", provider: "claude" }];
  await view.rerender(<NewSessionScreen />);
  await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/(app)/session/sess-new"));
});

it("explains disabled sharing without offering a start button", async () => {
  mockGet.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? targets : { ...options, sharing: false });
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByText(/Session sharing is off/)).toBeTruthy());
  expect(screen.queryByLabelText("Start session")).toBeNull();
  expect(mockPost).not.toHaveBeenCalled();
});
