import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import * as DocumentPicker from "expo-document-picker";
import * as ImagePicker from "expo-image-picker";
import { ActionSheetIOS } from "react-native";

// expo-router isn't in the jest transform allowlist; stub what AgentChat uses.
jest.mock("expo-router", () => ({
  router: {
    push: jest.fn(),
    back: jest.fn(),
    canGoBack: () => false,
    replace: jest.fn(),
    setParams: jest.fn(),
  },
  Stack: { Screen: () => null },
  useFocusEffect: jest.fn(),
  useLocalSearchParams: () => ({ agentId: "agent_a", name: "Friday" }),
}));

// user: null keeps history hydration + first-run kickoff inert.
jest.mock("@clerk/clerk-expo", () => ({
  useUser: () => ({ user: null }),
  useAuth: () => ({ getToken: jest.fn(async () => "token") }),
}));

// Incrementing uuids: chips, run ids, and error items all mint via
// randomUUID, and a constant would collide as React keys.
let mockUuidN = 0;
jest.mock("expo-crypto", () => ({
  randomUUID: () =>
    `00000000-0000-4000-8000-${String(++mockUuidN).padStart(12, "0")}`,
}));

const mockSendMessage = jest.fn(async (_roomId: string, request: { clientMessageId: string; text: string }) => ({
  messageId: request.clientMessageId,
  roomId: "room_a",
  messageSeq: 1,
  roomRevision: 1,
  author: { kind: "human", id: "user_a", displayName: "You" },
  text: request.text,
  mentions: [],
  replyToMessageId: null,
  threadRootMessageId: null,
  createdAt: "2026-09-09T00:00:00.000Z",
  runId: null,
}));
const mockRoom = {
  roomId: "room_a",
  name: null,
  kind: "dm",
  roomRevision: 1,
  lastMessageSeq: 0,
  lastMessageAt: null,
  unreadCount: 0,
  updatedAt: "2026-09-09T00:00:00.000Z",
  deliveryKey: "delivery_a",
  members: [{
    kind: "agent",
    id: "agent_a",
    displayName: "Friday",
    mentionHandle: "friday",
    role: "member",
    listenMode: "active",
    joinedAt: "2026-09-09T00:00:00.000Z",
    joinedSeq: 0,
  }],
  lastReadSeq: 0,
  createdAt: "2026-09-09T00:00:00.000Z",
};
const mockMessaging = {
  listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
  getRoom: jest.fn(async () => mockRoom),
  listMessages: jest.fn(async () => ({ messages: [], nextAfterSeq: null })),
  markRead: jest.fn(async (_roomId: string, request: { lastReadSeq: number }) => request),
  sendMessage: mockSendMessage,
  abortRun: jest.fn(),
};
const mockLiveEvents = {
  httpReady: true,
  liveConnected: true,
  liveChannels: ["/rooms/room_a/delivery_a"],
  liveError: null,
  connectionRevision: 1,
  messaging: mockMessaging,
  runtime: { rpc: jest.fn() },
  setActiveRoomSubscription: jest.fn(),
  onChatMessage: () => () => {},
  onEvent: () => () => {},
};
jest.mock("../../../../providers/live-events", () => ({
  useLiveEvents: () => mockLiveEvents,
}));

// createApiClient override intercepts the upload rail (attachments.ts builds
// a one-shot client per upload); everything else stays real.
const mockUpload = jest.fn();
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useRuntimeRpc: () => ({ data: null, error: null, isLoading: false }),
  createApiClient: () => ({ upload: mockUpload }),
}));

import AgentChat from "../[agentId]";

const mockLaunchLibrary = ImagePicker.launchImageLibraryAsync as jest.Mock;
const mockGetDocument = DocumentPicker.getDocumentAsync as jest.Mock;

/** Auto-select a sheet row: 0 = Photo Library, 1 = Files, 2 = Cancel. */
function sheetSelects(index: number) {
  return jest
    .spyOn(ActionSheetIOS, "showActionSheetWithOptions")
    .mockImplementation((_opts, cb) => cb(index));
}

async function addLibraryChip(
  asset: Partial<{
    uri: string;
    fileName: string | null;
    fileSize: number;
    mimeType: string;
  }> = {},
) {
  sheetSelects(0);
  mockLaunchLibrary.mockResolvedValueOnce({
    canceled: false,
    assets: [
      {
        uri: "file:///tmp/photo.png",
        fileName: "photo.png",
        fileSize: 1234,
        mimeType: "image/png",
        ...asset,
      },
    ],
  });
  fireEvent.press(screen.getByLabelText("Attach"));
  return screen.findByText(asset.fileName ?? "photo.png");
}

describe("AgentChat attachments", () => {
  beforeEach(() => {
    jest.restoreAllMocks();
    mockSendMessage.mockClear();
    mockUpload.mockReset();
    mockLaunchLibrary.mockReset();
    mockGetDocument.mockReset();
  });

  it("offers Photo Library / Files / Cancel from the attach button", async () => {
    const sheet = sheetSelects(2);
    await render(<AgentChat />);
    fireEvent.press(screen.getByLabelText("Attach"));
    expect(sheet).toHaveBeenCalledWith(
      expect.objectContaining({
        options: ["Photo Library", "Files", "Cancel"],
        cancelButtonIndex: 2,
      }),
      expect.any(Function),
    );
  });

  it("adds a pending chip from the photo library and removes it", async () => {
    await render(<AgentChat />);
    await addLibraryChip();
    expect(screen.getByText("photo.png")).toBeTruthy();
    fireEvent.press(screen.getByLabelText("Remove photo.png"));
    await waitFor(() => expect(screen.queryByText("photo.png")).toBeNull());
  });

  it("rejects an oversize file with the web's message and no chip", async () => {
    await render(<AgentChat />);
    sheetSelects(1);
    mockGetDocument.mockResolvedValueOnce({
      canceled: false,
      assets: [
        {
          uri: "file:///tmp/big.zip",
          name: "big.zip",
          size: 11 * 1024 * 1024,
          mimeType: "application/zip",
        },
      ],
    });
    fireEvent.press(screen.getByLabelText("Attach"));
    expect(
      await screen.findByText("big.zip exceeds the 10MB limit"),
    ).toBeTruthy();
    expect(screen.queryByLabelText("Remove big.zip")).toBeNull();
  });

  it("uploads first, then sends the message with the path notice prepended", async () => {
    await render(<AgentChat />);
    await addLibraryChip();
    mockUpload.mockResolvedValueOnce({
      uploaded: [
        {
          filename: "photo.png",
          path: ".openclaw/workspaces/agent_a/uploads/photo.png",
          size: 1234,
        },
      ],
    });
    fireEvent.changeText(
      screen.getByPlaceholderText("Ask anything"),
      "look at this",
    );
    await screen.findByDisplayValue("look at this");
    fireEvent.press(screen.getByLabelText("Send"));
    // The user bubble renders only after the awaited upload resolves.
    await screen.findByText(/look at this/);
    expect(mockUpload).toHaveBeenCalledWith(
      "/container/files?agent_id=agent_a",
      expect.anything(),
    );
    expect(mockSendMessage).toHaveBeenCalledTimes(1);
    expect(mockUpload.mock.invocationCallOrder[0]).toBeLessThan(
      mockSendMessage.mock.invocationCallOrder[0],
    );
    // Message-reference contract, verbatim from web AgentChatWindow.
    expect(mockSendMessage.mock.calls[0][1].text).toBe(
      "[The user uploaded files to your workspace. You can read them at these paths:\n" +
        "- photo.png → .openclaw/workspaces/agent_a/uploads/photo.png]\n\n" +
        "look at this",
    );
    // Draft cleared: chip gone, input empty.
    expect(screen.queryByLabelText("Remove photo.png")).toBeNull();
    expect(screen.queryByDisplayValue("look at this")).toBeNull();
  });

  it("does not send when the upload fails; surfaces an inline error and keeps the draft", async () => {
    await render(<AgentChat />);
    await addLibraryChip();
    mockUpload.mockRejectedValueOnce(new Error("EFS unavailable"));
    fireEvent.changeText(
      screen.getByPlaceholderText("Ask anything"),
      "look at this",
    );
    await screen.findByDisplayValue("look at this");
    fireEvent.press(screen.getByLabelText("Send"));
    expect(
      await screen.findByText("File upload failed: EFS unavailable"),
    ).toBeTruthy();
    expect(mockSendMessage).not.toHaveBeenCalled();
    // Draft survives for a retry: chip + text both still staged.
    expect(screen.getByLabelText("Remove photo.png")).toBeTruthy();
    expect(screen.getByDisplayValue("look at this")).toBeTruthy();
  });
});
