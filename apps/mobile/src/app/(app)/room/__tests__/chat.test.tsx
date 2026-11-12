import { fireEvent, render, screen } from "@testing-library/react-native";

jest.mock("expo-router", () => ({
  Stack: { Screen: () => null },
  useFocusEffect: (cb: () => void) => require("react").useEffect(cb, [cb]),
  useLocalSearchParams: () => ({ roomId: "chat-1" }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "msg-1" }));
jest.mock("react-native-keyboard-controller", () => ({ KeyboardAvoidingView: require("react-native").View }));
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0 }) }));
jest.mock("react-native-markdown-display", () => ({ __esModule: true, default: ({ children }: { children: string }) => require("react").createElement(require("react-native").Text, null, children) }));
const mockSelect = jest.fn();
const mockSend = jest.fn();
let mockRuns: { roomId: string; state: string }[] = [];
jest.mock("../../../../lib/rooms", () => ({
  useMobileRooms: () => ({
    activeRoomId: "chat-1", selectRoom: mockSelect, sendMessage: mockSend, runs: mockRuns, optimisticMessages: [],
    messages: [
      { messageId: "m1", text: "What time do you open?", author: { kind: "human", displayName: "Ann" } },
      { messageId: "m2", text: "We open at **9**.", author: { kind: "agent", displayName: "Lightbulb" } },
    ],
  }),
}));

import ChatScreen from "../[roomId]/index";

beforeEach(() => { jest.clearAllMocks(); mockRuns = []; });

it("opens the room and shows its messages", async () => {
  await render(<ChatScreen />);
  expect(mockSelect).toHaveBeenCalledWith("chat-1");
  expect(screen.getByText("What time do you open?")).toBeTruthy();
  expect(screen.getByText("We open at **9**.")).toBeTruthy();
});

it("sends the typed text and reuses the id when a failed send is retried", async () => {
  mockSend.mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined);
  await render(<ChatScreen />);
  await fireEvent.changeText(screen.getByLabelText("Message"), "Book me in");
  await fireEvent.press(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByText("Not sent. Try again.")).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Send" }));
  expect(mockSend.mock.calls.map((c) => c[0].clientMessageId)).toEqual(["msg-1", "msg-1"]);
  expect(mockSend.mock.calls[0][0]).toMatchObject({ text: "Book me in", mentions: [], replyToMessageId: null, threadRootMessageId: null });
});

it("says when Lightbulb is replying", async () => {
  mockRuns = [{ roomId: "chat-1", state: "running" }];
  await render(<ChatScreen />);
  expect(screen.getByText("Lightbulb is replying…")).toBeTruthy();
});
