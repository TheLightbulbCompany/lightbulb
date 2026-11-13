import { fireEvent, render, screen } from "@testing-library/react-native";

let mockParams: Record<string, string> = { roomId: "chat-1" };
const mockTitles: string[] = [];
jest.mock("expo-router", () => ({
  Stack: { Screen: ({ options }: { options: { title: string } }) => { mockTitles.push(options.title); return null; } },
  useFocusEffect: (cb: () => void) => require("react").useEffect(cb, [cb]),
  useLocalSearchParams: () => mockParams,
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "msg-1" }));
jest.mock("react-native-keyboard-controller", () => ({ KeyboardAvoidingView: require("react-native").View }));
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0 }) }));
jest.mock("react-native-markdown-display", () => ({ __esModule: true, default: ({ children }: { children: string }) => require("react").createElement(require("react-native").Text, null, children) }));
const mockSelect = jest.fn();
const mockSend = jest.fn();
let mockRuns: { roomId: string; state: string }[] = [];
const CHAT = { roomId: "chat-1", kind: "group-dm", name: null, members: [{ kind: "agent", id: "front" }, { kind: "human", id: "me" }] };
const CALL = { roomId: "chat-1", kind: "channel", name: "Call with Ada", members: [{ kind: "agent", id: "front" }] };
let mockRoom: unknown = CHAT;
let mockMessages: unknown[] = [];
let mockStates: Record<string, string> = {};
let mockHistory = { historyError: false, historyLoading: false };
const mockLoadHistory = jest.fn();
const mockReconcile = jest.fn(async () => {});
jest.mock("../../../../lib/rooms", () => ({
  useMobileRooms: () => ({
    activeRoomId: "chat-1", activeRoom: mockRoom, rooms: [], selectRoom: mockSelect, sendMessage: mockSend, runs: mockRuns,
    optimisticMessages: [], messages: mockMessages, ...mockHistory, loadHistory: mockLoadHistory,
    sendState: (id: string) => mockStates[id] ?? "idle", reconcileSend: mockReconcile,
  }),
}));

import ChatScreen, { lineText } from "../[roomId]/index";

beforeEach(() => {
  jest.clearAllMocks();
  mockParams = { roomId: "chat-1" };
  mockStates = {};
  mockHistory = { historyError: false, historyLoading: false };
  mockTitles.length = 0;
  mockRuns = [];
  mockRoom = CHAT;
  mockMessages = [
    { messageId: "m1", text: "What time do you open?", author: { kind: "human", displayName: "Ann" } },
    { messageId: "m2", text: "We open at **9**.", author: { kind: "agent", displayName: "Lightbulb" } },
  ];
});

it("opens the room and shows its messages", async () => {
  await render(<ChatScreen />);
  expect(mockSelect).toHaveBeenCalledWith("chat-1");
  expect(screen.getByText("What time do you open?")).toBeTruthy();
  expect(screen.getByText("We open at **9**.")).toBeTruthy();
});

it("keeps a failed message as its own retry row while the next one is typed and sent", async () => {
  let n = 0;
  jest.requireMock("expo-crypto").randomUUID = () => `msg-${++n}`;
  mockSend.mockImplementationOnce(async () => { mockStates["msg-1"] = "rejected"; throw new Error("offline"); })
    .mockImplementation(async (r: { clientMessageId: string }) => { mockStates[r.clientMessageId] = "persisted"; });
  const { rerender } = await render(<ChatScreen />);
  await fireEvent.changeText(screen.getByLabelText("Message"), "Book me in");
  await fireEvent.press(screen.getByRole("button", { name: "Send" }));
  // The next message goes out on its own id; the failed one stays.
  await fireEvent.changeText(screen.getByLabelText("Message"), "And a trim");
  await fireEvent.press(screen.getByRole("button", { name: "Send" }));
  await rerender(<ChatScreen />);
  const retry = screen.getByLabelText("Not sent: Book me in. Tap to retry");
  expect(screen.queryByLabelText("Not sent: And a trim. Tap to retry")).toBeNull();
  await fireEvent.press(retry);
  expect(mockReconcile).toHaveBeenCalledWith("msg-1");
  expect(mockSend.mock.calls.map((c) => [c[0].clientMessageId, c[0].text])).toEqual([["msg-1", "Book me in"], ["msg-2", "And a trim"]]);
  expect(mockSend.mock.calls[0][0]).toMatchObject({ mentions: [], replyToMessageId: null, threadRootMessageId: null });
});

it("shows no Not sent row for a send that threw after the message was confirmed", async () => {
  jest.requireMock("expo-crypto").randomUUID = () => "msg-x";
  mockSend.mockImplementationOnce(async () => { mockStates["msg-x"] = "persisted"; throw new Error("late failure"); });
  const { rerender } = await render(<ChatScreen />);
  await fireEvent.changeText(screen.getByLabelText("Message"), "Book me in");
  await fireEvent.press(screen.getByRole("button", { name: "Send" }));
  await rerender(<ChatScreen />);
  expect(screen.queryByLabelText(/Not sent/)).toBeNull();
});

it("says when the history failed to load and retries it, instead of an empty chat", async () => {
  mockMessages = [];
  mockHistory = { historyError: true, historyLoading: false };
  await render(<ChatScreen />);
  expect(screen.queryByText("Ask Lightbulb anything.")).toBeNull();
  await fireEvent.press(screen.getByText("Couldn’t load this conversation. Retry"));
  expect(mockLoadHistory).toHaveBeenCalled();
});

it("says when Lightbulb is replying", async () => {
  mockRuns = [{ roomId: "chat-1", state: "running" }];
  await render(<ChatScreen />);
  expect(screen.getByText("Lightbulb is replying…")).toBeTruthy();
});

it("opens a call or meeting room as a read-only record", async () => {
  mockRoom = CALL;
  mockMessages = [{ messageId: "c1", text: "Can I move my booking?", author: { kind: "human", displayName: "Ada" } }];
  await render(<ChatScreen />);
  expect(screen.getByText("Can I move my booking?")).toBeTruthy();
  expect(screen.getByText("Ada")).toBeTruthy();
  expect(screen.queryByLabelText("Message")).toBeNull();
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  expect(screen.queryByText("Ask Lightbulb anything.")).toBeNull();
});

it("is read-only until the room is known, and for a chat shared with another person", async () => {
  mockRoom = undefined;
  mockMessages = [];
  await render(<ChatScreen />);
  expect(screen.queryByLabelText("Message")).toBeNull();
  mockRoom = { ...CHAT, members: [...CHAT.members, { kind: "human", id: "dana" }] };
  await render(<ChatScreen />);
  expect(screen.queryByLabelText("Message")).toBeNull();
  expect(screen.getByText("Nothing was recorded here.")).toBeTruthy();
});

it("says what a line without text carries", async () => {
  mockRoom = CALL;
  mockMessages = [{ messageId: "a1", text: "", attachments: [{ fileName: "quote.pdf" }], author: { kind: "agent", displayName: "Lightbulb" } }];
  await render(<ChatScreen />);
  expect(screen.getByText("Attached: quote.pdf")).toBeTruthy();
  expect(lineText({ text: "", taskRefs: [{ taskId: "t", role: "started" }] })).toBe("Started a task");
  expect(lineText({ text: "  " })).toBe("(No text)");
});

it("titles a record with the row's own words, not the room's internal name", async () => {
  mockRoom = CALL;
  mockParams = { roomId: "chat-1", title: "Weekly sync" };
  await render(<ChatScreen />);
  expect(mockTitles.at(-1)).toBe("Weekly sync");
});
