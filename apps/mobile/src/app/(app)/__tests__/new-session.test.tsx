import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";

const mockReplace = jest.fn();
const mockDismissTo = jest.fn();
const mockBack = jest.fn();
let mockOrgId: string | undefined = "org-a";
jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { replace: (...args: unknown[]) => mockReplace(...args), dismissTo: (...args: unknown[]) => mockDismissTo(...args), back: () => mockBack() },
    Stack: { Screen: () => null },
    useFocusEffect: (callback: () => void | (() => void)) => useEffect(callback, [callback]),
  };
});
jest.mock("@clerk/clerk-expo", () => ({ useAuth: () => ({ userId: "user-a", orgId: mockOrgId, isLoaded: true }) }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "33333333-3333-4333-8333-333333333333" }));
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }) }));
jest.mock("../../../lib/keyboardOffset", () => ({ usePushedScreenKeyboardOffset: () => 0 }));
let mockSavedStart: Record<string, unknown> = {};
jest.mock("../../../lib/localStore", () => ({ LocalStore: class {
  read() { return mockSavedStart; }
  write(next: Record<string, unknown>) { mockSavedStart = next; }
  flush() {}
} }));
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
type MockEvent = { type: string; payload: { operationId: string; state: string; error?: { code: string; message: string; retryable: boolean } } };
let mockEventHandler: ((event: MockEvent) => void) | null = null;
const mockOnEvent = jest.fn((handler: (event: MockEvent) => void) => {
  mockEventHandler = handler;
  return () => { mockEventHandler = null; };
});
const mockLive = { onEvent: (handler: (event: MockEvent) => void) => mockOnEvent(handler) };
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import NewSessionScreen from "../session/new";

const targets = { enabled: true, devices: [{ deviceId: "mac-1", generation: "gen-1", name: "Your Mac" }] };
const options = { name: "Alice's Mac", sharing: true, folders: ["/Users/alice/code"], tools: [
  { id: "claude", label: "Claude Code", models: [{ id: "sonnet", label: "Sonnet" }],
    modes: [{ id: "auto", label: "Auto review" }, { id: "plan", label: "Plan" }],
    modeLabel: "Permission mode", efforts: [{ id: "high", label: "High" }], defaultMode: "auto", defaultEffort: null },
  { id: "codex", label: "Codex", models: [{ id: "gpt-6", label: "GPT-6" }],
    modes: [{ id: "auto-review", label: "Auto review" }, { id: "on-request", label: "On request" }], modeLabel: "Approval policy",
    efforts: [{ id: "high", label: "High" }], defaultMode: "auto-review", defaultEffort: null },
] };

beforeEach(() => {
  mockSavedStart = {};
  mockOrgId = "org-a";
  mockEventHandler = null;
  mockReplace.mockClear();
  mockDismissTo.mockClear();
  mockBack.mockClear();
  mockGet.mockReset();
  mockPost.mockReset();
  mockOnEvent.mockClear();
  mockGet.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? targets : options);
  mockPost.mockResolvedValue({ startId: "33333333-3333-4333-8333-333333333333" });
});

it("starts an approved Mac session and waits for its exact start receipt", async () => {
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByLabelText("Session options")).toBeTruthy());
  expect(screen.queryByText("Approved folder")).toBeNull();
  await fireEvent.changeText(screen.getByLabelText("First message"), "Fix the build safely.");
  await waitFor(() => expect(screen.getByLabelText("Start session").props.accessibilityState.disabled).toBe(false));
  await fireEvent.press(screen.getByLabelText("Start session"));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/desktop-sessions/start?source=ios", {
    startId: "33333333-3333-4333-8333-333333333333",
    deviceId: "mac-1", generation: "gen-1", tool: "claude", folder: "/Users/alice/code",
    model: null, mode: "auto", effort: null, prompt: "Fix the build safely.",
  }));
  expect(mockReplace).not.toHaveBeenCalled();
  await waitFor(() => expect(mockEventHandler).not.toBeNull());
  await act(async () => { mockEventHandler?.({ type: "runtime.operation.changed", payload: { operationId: "someone-else", state: "succeeded" } }); });
  expect(screen.queryByLabelText("View sessions")).toBeNull();
  expect(mockReplace).not.toHaveBeenCalled();
  await act(async () => { mockEventHandler?.({ type: "runtime.operation.changed", payload: { operationId: "33333333-3333-4333-8333-333333333333", state: "succeeded" } }); });
  await waitFor(() => expect(screen.getByLabelText("View sessions")).toBeTruthy());
  expect(mockReplace).not.toHaveBeenCalled();
  await fireEvent.press(screen.getByLabelText("View sessions"));
  expect(mockDismissTo).toHaveBeenCalledWith("/(app)/(tabs)/chats?filter=sessions");
});

it("keeps tool and permission choices in optional session settings", async () => {
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByLabelText("Session options")).toBeTruthy());
  expect(screen.queryByText("Permission mode")).toBeNull();
  await fireEvent.press(screen.getByLabelText("Session options"));
  await waitFor(() => expect(screen.getByText("Permission mode")).toBeTruthy());
  await fireEvent.press(screen.getByText("Plan"));
  await fireEvent.press(screen.getByText("Sonnet"));
  await fireEvent.press(screen.getByLabelText("Done with session options"));
  await fireEvent.changeText(screen.getByLabelText("First message"), "Review this project.");
  await fireEvent.press(screen.getByLabelText("Start session"));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/desktop-sessions/start?source=ios", expect.objectContaining({
    model: "sonnet", mode: "plan", effort: null,
  })));
});

it("uses the selected Codex model, reasoning effort and approval policy", async () => {
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByLabelText("Session options")).toBeTruthy());
  await fireEvent.press(screen.getByLabelText("Session options"));
  await fireEvent.press(screen.getByText("Codex"));
  await fireEvent.press(screen.getByText("GPT-6"));
  await fireEvent.press(screen.getByText("High"));
  await fireEvent.press(screen.getByLabelText("Done with session options"));
  await fireEvent.changeText(screen.getByLabelText("First message"), "Review this project.");
  await fireEvent.press(screen.getByLabelText("Start session"));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/desktop-sessions/start?source=ios", expect.objectContaining({
    tool: "codex", model: "gpt-6", mode: "auto-review", effort: "high",
  })));
});

it("keeps an uncertain POST result pending so a retry cannot launch a duplicate CLI", async () => {
  mockPost.mockRejectedValue(new Error("connection dropped"));
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByLabelText("Session options")).toBeTruthy());
  await fireEvent.changeText(screen.getByLabelText("First message"), "Fix the build safely.");
  await waitFor(() => expect(screen.getByLabelText("Start session").props.accessibilityState.disabled).toBe(false));
  await fireEvent.press(screen.getByLabelText("Start session"));
  await waitFor(() => expect(screen.getByText(/Could not confirm whether/)).toBeTruthy());
  expect(screen.queryByLabelText("Start session")).toBeNull();
  expect(screen.getByLabelText("View sessions")).toBeTruthy();
});

it("keeps a retryable Mac outcome pending because Terminal may already have opened", async () => {
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByLabelText("Session options")).toBeTruthy());
  await fireEvent.changeText(screen.getByLabelText("First message"), "Fix the build safely.");
  await waitFor(() => expect(screen.getByLabelText("Start session").props.accessibilityState.disabled).toBe(false));
  await fireEvent.press(screen.getByLabelText("Start session"));
  await act(async () => { mockEventHandler?.({ type: "runtime.operation.changed", payload: {
    operationId: "33333333-3333-4333-8333-333333333333", state: "failed",
    error: { code: "desktop_timeout", message: "Mac reply was lost", retryable: true },
  } }); });
  expect(await screen.findByText(/Could not confirm whether/)).toBeTruthy();
  expect(screen.queryByLabelText("Start session")).toBeNull();
  expect(mockPost).toHaveBeenCalledTimes(1);
});

it("accepts a Mac receipt that arrives before the start POST returns", async () => {
  mockPost.mockImplementation(async () => {
    mockEventHandler?.({ type: "runtime.operation.changed", payload: {
      operationId: "33333333-3333-4333-8333-333333333333", state: "succeeded",
    } });
    return { startId: "33333333-3333-4333-8333-333333333333" };
  });
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByLabelText("Session options")).toBeTruthy());
  await fireEvent.changeText(screen.getByLabelText("First message"), "Fix the build safely.");
  await waitFor(() => expect(screen.getByLabelText("Start session").props.accessibilityState.disabled).toBe(false));
  await fireEvent.press(screen.getByLabelText("Start session"));
  await waitFor(() => expect(screen.getByText(/Terminal opened on/)).toBeTruthy());
});

it("opens only the native session linked to this start, even when another session appears first", async () => {
  mockGet.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? targets
    : path === "/messaging/sessions" ? { sessions: [
      { startId: "someone-else", sessionId: "wrong", humanUserId: "user-a" },
      { startId: "33333333-3333-4333-8333-333333333333", sessionId: "exact", humanUserId: "user-a" },
    ] } : options);
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByLabelText("First message")).toBeTruthy());
  await fireEvent.changeText(screen.getByLabelText("First message"), "Fix the build safely.");
  await waitFor(() => expect(screen.getByLabelText("Start session").props.accessibilityState.disabled).toBe(false));
  await fireEvent.press(screen.getByLabelText("Start session"));
  await waitFor(() => expect(mockReplace).toHaveBeenCalledWith({
    pathname: "/(app)/session/[sessionId]", params: { sessionId: "exact" },
  }));
  expect(mockReplace).not.toHaveBeenCalledWith(expect.objectContaining({ params: { sessionId: "wrong" } }));
});

it("recovers an unresolved start after the phone app restarts", async () => {
  mockSavedStart = { userId: "user-a", ownerScope: "org-a", startId: "33333333-3333-4333-8333-333333333333", startedAt: Date.now() };
  mockGet.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? targets
    : path === "/messaging/sessions" ? { sessions: [
      { startId: "33333333-3333-4333-8333-333333333333", sessionId: "recovered", humanUserId: "user-a" },
    ] } : options);
  await render(<NewSessionScreen />);
  await waitFor(() => expect(mockReplace).toHaveBeenCalledWith({
    pathname: "/(app)/session/[sessionId]", params: { sessionId: "recovered" },
  }));
  expect(mockPost).not.toHaveBeenCalled();
});

it("does not restore a start from another workspace", async () => {
  mockSavedStart = { userId: "user-a", ownerScope: "org-old", startId: "33333333-3333-4333-8333-333333333333", startedAt: Date.now() };
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByLabelText("First message")).toBeTruthy());
  expect(mockSavedStart).toEqual({});
  expect(screen.getByLabelText("Start session")).toBeTruthy();
});

it("explains disabled sharing without offering a start button", async () => {
  mockGet.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? targets : { ...options, sharing: false });
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByText(/Session sharing is off/)).toBeTruthy());
  expect(screen.queryByLabelText("Start session")).toBeNull();
  expect(mockPost).not.toHaveBeenCalled();
});

it("lets the owner choose another Mac when the first has sharing off", async () => {
  mockGet.mockImplementation(async (path: string) => path === "/desktop-sessions/targets"
    ? { enabled: true, devices: [...targets.devices, { deviceId: "mac-2", generation: "gen-2", name: "Second Mac" }] }
    : path.includes("mac-2") ? options : { ...options, sharing: false });
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByLabelText("Choose another Mac")).toBeTruthy());
  await fireEvent.press(screen.getByLabelText("Choose another Mac"));
  await fireEvent.press(screen.getByText("Second Mac"));
  await waitFor(() => expect(screen.getByLabelText("First message")).toBeTruthy());
});

it("blocks an older Mac that cannot guarantee the selected permissions", async () => {
  mockGet.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? targets
    : { ...options, tools: options.tools.map((tool) => ({
      id: tool.id, label: tool.label, models: tool.models, modes: tool.modes, modeLabel: tool.modeLabel,
    })) });
  await render(<NewSessionScreen />);
  await waitFor(() => expect(screen.getByText(/Update Lightbulb on Alice's Mac to choose safe session settings/)).toBeTruthy());
  expect(screen.getByLabelText("Start session").props.accessibilityState.disabled).toBe(true);
  expect(mockPost).not.toHaveBeenCalled();
});
