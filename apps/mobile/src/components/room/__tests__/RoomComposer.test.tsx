import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ActionSheetIOS, StyleSheet } from "react-native";
import * as Reanimated from "react-native-reanimated";
import { chatPalette } from "../../../config/theme";
import { ApiError, type LocalSession, type RoomMember } from "@lightbulb/shared";
import { saveDraft } from "../../../lib/composerDraft";
import { RoomComposer, type RoomComposerSessionOption } from "../RoomComposer";

jest.mock("expo-crypto", () => ({ randomUUID: () => "77777777-7777-4777-8777-777777777777" }));

// Mutable so each test can drive a different device's insets, the same
// pattern ProvisionGate.test.tsx uses for its own useSafeAreaInsets mock.
let mockInsets = { top: 0, bottom: 0, left: 0, right: 0 };
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => mockInsets,
}));

const members: RoomMember[] = [
  {
    kind: "agent",
    id: "agent-1",
    displayName: "Friday",
    mentionHandle: "friday",
    role: "member",
    listenMode: "active",
    joinedAt: "x",
    joinedSeq: 0,
  },
  {
    kind: "human",
    id: "user-b",
    displayName: "Aden Kim",
    mentionHandle: null,
    role: "member",
    listenMode: null,
    joinedAt: "x",
    joinedSeq: 0,
  },
];

const sessionTargets: RoomComposerSessionOption[] = [{
  candidate: {
    session: {
      sessionId: "session-1", deviceId: "mac", humanUserId: "user-b", provider: "claude",
      generation: "g1", status: "idle", providerVersion: "1", controlUnavailableReason: null,
      activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false },
      capabilitiesVerified: true, latestEventSeq: 0, cwdName: "lightbulb", title: "Launch check",
    } satisfies LocalSession,
    handle: "claude-launch-check",
    collided: false,
  },
  ownerName: "Aden Kim",
}];

// A fresh draft key per case, so one case's kept draft never leaks into the
// next (the store is a module-level singleton, as it is on the device).
let keySeq = 0;
const draftKey = () => `test-owner|room-${keySeq++}|`;

// RNTL 14's render/fireEvent are async — every call here is awaited, the
// convention the other room suites already follow.
const setup = async (
  onSend = jest.fn(async () => {}),
  onTypingChange?: (typing: boolean) => void,
  targets: RoomComposerSessionOption[] = [],
) => {
  const onUploadAttachment = jest.fn(async () => ({ id: "att-1" }));
  await render(
    <RoomComposer
      members={members}
      sessionTargets={targets}
      placeholder="Message"
      disabled={false}
      reconnecting={false}
      onSend={onSend}
      onUploadAttachment={onUploadAttachment}
      draftKey={draftKey()}
      onTypingChange={onTypingChange}
    />,
  );
  return { onSend, onUploadAttachment };
};

const input = () => screen.getByPlaceholderText("Message");
const caretAt = (position: number) =>
  fireEvent(input(), "selectionChange", {
    nativeEvent: { selection: { start: position, end: position } },
  });

function choosePhotoSource(index: 0 | 1) {
  return jest
    .spyOn(ActionSheetIOS, "showActionSheetWithOptions")
    .mockImplementation((_options, callback) => callback(index));
}

describe("RoomComposer", () => {
  beforeEach(() => {
    jest.restoreAllMocks();
    jest.clearAllMocks();
    mockInsets = { top: 0, bottom: 0, left: 0, right: 0 };
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.requestMediaLibraryPermissionsAsync.mockResolvedValue({ granted: true });
    ImagePicker.requestCameraPermissionsAsync.mockResolvedValue({ granted: true });
    ImagePicker.launchImageLibraryAsync.mockResolvedValue({ canceled: true, assets: null });
    ImagePicker.launchCameraAsync.mockResolvedValue({ canceled: true, assets: null });
  });

  it("offers the photo library and camera from the attachment action", async () => {
    const sheet = choosePhotoSource(0);
    await setup();
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    expect(sheet).toHaveBeenCalledWith(
      expect.objectContaining({ options: ["Photo Library", "Take Photo", "Cancel"] }),
      expect.any(Function),
    );
  });

  it("sends the structured mentions the typed text resolves to", async () => {
    const onSend = jest.fn(async () => {});
    await setup(onSend);
    await fireEvent.changeText(input(), "ping @friday");
    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(
        expect.objectContaining({
          text: "ping @friday",
          mentions: [{ kind: "agent", id: "agent-1" }],
        }),
      ),
    );
  });

  // The @ picker is gone; a typed session handle still resolves at send.
  it("offers no @ picker and sends a typed session handle's id", async () => {
    const onSend = jest.fn(async () => {});
    await setup(onSend, undefined, sessionTargets);
    await fireEvent.changeText(input(), "@cl");
    await caretAt(3);
    expect(screen.queryByLabelText(/^Mention /)).toBeNull();
    await fireEvent.changeText(input(), "@claude-launch-check ");
    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith(expect.objectContaining({
      sessionMentions: ["session-1"],
    })));
  });

  it("holds a stale session handle until the sender chooses to send it as text", async () => {
    const onSend = jest.fn(async () => {});
    await setup(onSend, undefined, sessionTargets);
    await fireEvent.changeText(input(), "@claude-gone ping");
    await fireEvent.press(screen.getByLabelText("Send"));
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByText("@claude-gone no longer matches a shared session. Remove it or send anyway.")).toBeTruthy();
    await fireEvent.press(screen.getByText("Send anyway"));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith(expect.not.objectContaining({ sessionMentions: expect.anything() })));
  });

  it("opens the emoji typeahead on : and inserts the glyph", async () => {
    await setup();
    await fireEvent.changeText(input(), "ship :tar");
    await caretAt(9);
    await waitFor(() => expect(screen.getByLabelText("Insert 🎯")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Insert 🎯"));
    expect(input().props.value).toBe("ship 🎯 ");
  });

  it("refuses to send an empty draft", async () => {
    const onSend = jest.fn(async () => {});
    await setup(onSend);
    await fireEvent.press(screen.getByLabelText("Send"));
    expect(onSend).not.toHaveBeenCalled();
  });

  // A photo uploads when it is PICKED, not when the message is sent, so
  // sending is instant and the draft can hold an id that survives leaving.
  it("uploads a picked photo straight away and sends its attachment id", async () => {
    choosePhotoSource(0);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [{ uri: "file:///a.png", fileName: "a.png", mimeType: "image/png", fileSize: 10 }],
    });
    const onSend = jest.fn(async () => {});
    const { onUploadAttachment } = await setup(onSend);
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    expect(await screen.findByText("a.png")).toBeTruthy();
    // A part that carries its own bytes: the `{uri}` descriptor is refused by
    // Expo's fetch, which failed every upload from the phone.
    await waitFor(() =>
      expect(onUploadAttachment).toHaveBeenCalledWith({
        name: "a.png",
        type: "image/png",
        bytes: expect.any(Function),
      }),
    );
    // The photo as it is on the phone: nothing here is compressed twice.
    expect(ImagePicker.launchImageLibraryAsync).toHaveBeenCalledWith(
      expect.objectContaining({ quality: 1 }),
    );
    expect(jest.requireMock("expo-image-manipulator").ImageManipulator.manipulate).not.toHaveBeenCalled();
    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(expect.objectContaining({ attachmentIds: ["att-1"] })),
    );
    // Sending did not re-upload what was already uploaded.
    expect(onUploadAttachment).toHaveBeenCalledTimes(1);
  });

  // The composer reads the conversation's stored draft on mount — which is
  // what makes backing out of a room and returning keep the message.
  it("opens showing the draft left in this conversation", async () => {
    const key = draftKey();
    saveDraft(key, { text: "half a thought", attachments: [] });
    await render(
      <RoomComposer
        members={members}
        placeholder="Message"
        disabled={false}
        reconnecting={false}
        onSend={jest.fn(async () => {})}
        onUploadAttachment={jest.fn(async () => ({ id: "att-1" }))}
        draftKey={key}
      />,
    );
    expect(screen.getByPlaceholderText("Message").props.value).toBe("half a thought");
  });

  // Formatting stays hidden until asked for — always on, it took a full row
  // from the message, which on a phone with the keyboard up left one line.
  it("keeps formatting out of the way until the reader asks for it", async () => {
    await setup();
    expect(screen.queryByLabelText("Bold")).toBeNull();

    await fireEvent.press(screen.getByLabelText("Formatting"));
    expect(screen.getByLabelText("Bold")).toBeTruthy();
    expect(screen.getByLabelText("Formatting").props.accessibilityState).toMatchObject({
      expanded: true,
    });

    await fireEvent.press(screen.getByLabelText("Formatting"));
    expect(screen.queryByLabelText("Bold")).toBeNull();
  });

  it("puts formatting away once the message is sent", async () => {
    const onSend = jest.fn(async () => {});
    await setup(onSend);
    await fireEvent.changeText(screen.getByPlaceholderText("Message"), "hello");
    await fireEvent.press(screen.getByLabelText("Formatting"));
    expect(screen.getByLabelText("Bold")).toBeTruthy();

    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(screen.queryByLabelText("Bold")).toBeNull();
  });

  // Two toolbar taps fit inside one native round trip easily. If the second
  // read the offsets the first was working from, it would slice a string that
  // had already changed under it — and since this is the persisted draft, the
  // corruption would be written to disk and restored next time.
  it("formats correctly when a second tap lands before iOS reports the first", async () => {
    await setup();
    const input = screen.getByPlaceholderText("Message");
    await fireEvent.changeText(input, "hello world");
    await fireEvent(input, "selectionChange", {
      nativeEvent: { selection: { start: 6, end: 11 } },
    });

    await fireEvent.press(screen.getByLabelText("Formatting"));
    // Bold, then italic, with NO selectionChange echo in between.
    await fireEvent.press(screen.getByLabelText("Bold"));
    await fireEvent.press(screen.getByLabelText("Italic"));

    expect(screen.getByPlaceholderText("Message").props.value).toBe("hello **_world_**");
  });

  // The controlled selection used to release only on an exact match, so a
  // reader who moved the caret before the echo arrived was never released —
  // and every later render snapped it back to where the toolbar left it.
  it("stops driving the caret as soon as the reader moves it", async () => {
    await setup();
    const input = screen.getByPlaceholderText("Message");
    await fireEvent.changeText(input, "hello");
    await fireEvent.press(screen.getByLabelText("Formatting"));
    await fireEvent.press(screen.getByLabelText("Bold"));
    expect(screen.getByPlaceholderText("Message").props.selection).toBeTruthy();

    // The reader taps somewhere else entirely; not the range we asked for.
    await fireEvent(input, "selectionChange", {
      nativeEvent: { selection: { start: 1, end: 1 } },
    });
    expect(screen.getByPlaceholderText("Message").props.selection).toBeUndefined();
  });

  // A remembered range can outlive the text it referred to (the draft was
  // restored, a send cleared the field). Slicing past the end must not happen.
  it("keeps a stale selection inside the text it is applied to", async () => {
    await setup();
    const input = screen.getByPlaceholderText("Message");
    await fireEvent.changeText(input, "a longer draft");
    await fireEvent(input, "selectionChange", {
      nativeEvent: { selection: { start: 5, end: 14 } },
    });
    await fireEvent.changeText(input, "short");
    await fireEvent.press(screen.getByLabelText("Formatting"));
    await fireEvent.press(screen.getByLabelText("Bold"));
    expect(screen.getByPlaceholderText("Message").props.value).toContain("short");
  });

  it("refuses to send past the limits the server enforces", async () => {
    const onSend = jest.fn(async () => {});
    await setup(onSend);
    await fireEvent.changeText(screen.getByPlaceholderText("Message"), "x".repeat(100_001));
    expect(screen.getByText("Keep your message within 100,000 UTF-8 bytes.")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Send"));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("drops a removed photo from the message", async () => {
    choosePhotoSource(0);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [{ uri: "file:///a.png", fileName: "a.png", mimeType: "image/png", fileSize: 10 }],
    });
    await setup();
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    expect(await screen.findByLabelText("Preview a.png")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Remove a.png"));
    expect(screen.queryByLabelText("Preview a.png")).toBeNull();
  });

  it("reports typing as the draft goes non-empty and empty again", async () => {
    const onTypingChange = jest.fn();
    await setup(jest.fn(async () => {}), onTypingChange);
    await fireEvent.changeText(input(), "h");
    expect(onTypingChange).toHaveBeenLastCalledWith(true);
    await fireEvent.changeText(input(), "");
    expect(onTypingChange).toHaveBeenLastCalledWith(false);
  });

  it("reports typing stopped on blur", async () => {
    const onTypingChange = jest.fn();
    await setup(jest.fn(async () => {}), onTypingChange);
    await fireEvent.changeText(input(), "hi");
    await fireEvent(input(), "blur");
    expect(onTypingChange).toHaveBeenLastCalledWith(false);
  });

  it("reports typing stopped the moment a message sends", async () => {
    const onTypingChange = jest.fn();
    await setup(jest.fn(async () => {}), onTypingChange);
    await fireEvent.changeText(input(), "ping");
    await fireEvent.press(screen.getByLabelText("Send"));
    expect(onTypingChange).toHaveBeenLastCalledWith(false);
  });

  // The messaging endpoint caps an image at 5MB and takes no HEIC, which is
  // what an iPhone's own camera saves. Both used to be dead ends for the
  // person; now the phone scales the photo down and sends a JPEG.
  it.each([
    ["a photo over the 5MB cap", "big.png", "image/png", 6e6, 1600, 3000, { height: 2048 }],
    ["an iPhone's HEIC photo", "IMG_1.heic", "image/heic", 3e6, 5712, 4284, { width: 2048 }],
  ])("sends %s as a scaled-down JPEG", async (_what, fileName, mimeType, fileSize, width, height, scaled) => {
    choosePhotoSource(0);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [{ uri: `file:///${fileName}`, fileName, mimeType, fileSize, width, height }],
    });
    const { onUploadAttachment } = await setup();
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    const sent = fileName.replace(/\.\w+$/, ".jpg");
    await waitFor(() =>
      expect(onUploadAttachment).toHaveBeenCalledWith(
        expect.objectContaining({ name: sent, type: "image/jpeg" }),
      ),
    );
    const { manipulate } = jest.requireMock("expo-image-manipulator").ImageManipulator;
    expect(manipulate).toHaveBeenCalledWith(`file:///${fileName}`);
    expect(manipulate.mock.results[0].value.resize).toHaveBeenCalledWith(scaled);
  });

  it("does not scale up a small photo it has to re-encode", async () => {
    choosePhotoSource(0);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [
        { uri: "file:///s.heic", fileName: "s.heic", mimeType: "image/heic", fileSize: 9, width: 800, height: 600 },
      ],
    });
    const { onUploadAttachment } = await setup();
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    await waitFor(() => expect(onUploadAttachment).toHaveBeenCalled());
    const { manipulate } = jest.requireMock("expo-image-manipulator").ImageManipulator;
    expect(manipulate.mock.results[0].value.resize).not.toHaveBeenCalled();
  });

  // A GIF or a WebP can be animated, and re-encoding would keep only the
  // first frame, so one over the cap is still refused, in words.
  it.each([
    ["GIF", "big.gif", "image/gif"],
    ["WebP", "big.webp", "image/webp"],
  ])("refuses a %s over the cap rather than flattening it", async (_kind, fileName, mimeType) => {
    choosePhotoSource(0);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [{ uri: `file:///${fileName}`, fileName, mimeType, fileSize: 6e6, width: 400, height: 400 }],
    });
    const { onUploadAttachment } = await setup();
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    await waitFor(() => expect(screen.getByText(/The limit is 5MB/)).toBeTruthy());
    expect(onUploadAttachment).not.toHaveBeenCalled();
    expect(jest.requireMock("expo-image-manipulator").ImageManipulator.manipulate).not.toHaveBeenCalled();
  });

  // The photo is not in the draft until it has been re-encoded. Sending in
  // that gap would send the text alone and strand the photo in the next draft.
  it("holds Send while a photo is being converted", async () => {
    choosePhotoSource(0);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [
        { uri: "file:///IMG_2.heic", fileName: "IMG_2.heic", mimeType: "image/heic", fileSize: 3e6, width: 4000, height: 3000 },
      ],
    });
    let finish: () => void = () => {};
    const { manipulate } = jest.requireMock("expo-image-manipulator").ImageManipulator;
    manipulate.mockImplementationOnce((uri: string) => ({
      resize: jest.fn(),
      renderAsync: () =>
        new Promise((resolve) => {
          finish = () => resolve({ saveAsync: async () => ({ uri: `${uri}.jpg`, width: 1, height: 1 }) });
        }),
    }));
    const onSend = jest.fn(async () => {});
    await setup(onSend);
    await fireEvent.changeText(input(), "with a photo");
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    await waitFor(() => expect(manipulate).toHaveBeenCalled());
    await fireEvent.press(screen.getByLabelText("Send"));
    expect(onSend).not.toHaveBeenCalled();
    finish();
    expect(await screen.findByText("IMG_2.jpg")).toBeTruthy();
    await waitFor(() => expect(screen.queryByLabelText("Uploading")).toBeNull());
    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(
        expect.objectContaining({ text: "with a photo", attachmentIds: ["att-1"] }),
      ),
    );
  });

  it("takes a new photo after camera permission is granted", async () => {
    choosePhotoSource(1);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchCameraAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [
        {
          uri: "file:///camera.jpg",
          fileName: "camera.jpg",
          mimeType: "image/jpeg",
          fileSize: 20,
        },
      ],
    });
    await setup();
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    expect(await screen.findByLabelText("Preview camera.jpg")).toBeTruthy();
  });

  it("shows a recoverable camera permission error without opening the camera", async () => {
    choosePhotoSource(1);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.requestCameraPermissionsAsync.mockResolvedValueOnce({ granted: false });
    await setup();
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    expect(
      await screen.findByText("Camera access is needed. Allow it in Settings, then try again."),
    ).toBeTruthy();
    expect(ImagePicker.launchCameraAsync).not.toHaveBeenCalled();
  });

  it("rejects an unsupported picker result and leaves the composer usable", async () => {
    choosePhotoSource(0);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [
        {
          uri: "file:///not-photo.pdf",
          fileName: "not-photo.pdf",
          mimeType: "application/pdf",
          fileSize: 10,
        },
      ],
    });
    const { onUploadAttachment } = await setup();
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    expect(await screen.findByText(/isn't a photo we can send/)).toBeTruthy();
    expect(onUploadAttachment).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Attach photo")).toBeTruthy();
  });

  it("pastes a copied photo from the attachment action", async () => {
    const Clipboard = jest.requireMock("expo-clipboard");
    Clipboard.hasImageAsync.mockResolvedValueOnce(true);
    Clipboard.getImageAsync.mockResolvedValueOnce({
      data: "data:image/jpeg;base64,anBlZw==",
      size: { width: 4, height: 3 },
    });
    const sheet = choosePhotoSource(0);
    const { onUploadAttachment } = await setup();
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    expect(await screen.findByText("Pasted photo.jpg")).toBeTruthy();
    expect(sheet).toHaveBeenCalledWith(
      expect.objectContaining({
        options: ["Paste Photo", "Photo Library", "Take Photo", "Cancel"],
        cancelButtonIndex: 3,
      }),
      expect.any(Function),
    );
    await waitFor(() =>
      expect(onUploadAttachment).toHaveBeenCalledWith(
        expect.objectContaining({ name: "Pasted photo.jpg", type: "image/jpeg" }),
      ),
    );
  });

  it("pastes a photo directly into the message field", async () => {
    const { onUploadAttachment } = await setup();

    await fireEvent(screen.getByTestId("paste-input-wrapper"), "paste", {
      type: "images",
      uris: ["file:///native-paste.png"],
    });

    expect(await screen.findByText("native-paste.png")).toBeTruthy();
    await waitFor(() =>
      expect(onUploadAttachment).toHaveBeenCalledWith(
        expect.objectContaining({ name: "native-paste.png", type: "image/png" }),
      ),
    );
  });

  it("keeps a direct multi-photo paste within the four-photo limit", async () => {
    const { onUploadAttachment } = await setup();

    await fireEvent(screen.getByTestId("paste-input-wrapper"), "paste", {
      type: "images",
      uris: [1, 2, 3, 4, 5].map((number) => `file:///paste-${number}.png`),
    });

    await waitFor(() => expect(onUploadAttachment).toHaveBeenCalledTimes(4));
    expect(screen.getByText("paste-4.png")).toBeTruthy();
    expect(screen.queryByText("paste-5.png")).toBeNull();
  });

  it("does not admit a second direct paste while the first batch is being prepared", async () => {
    const { onUploadAttachment } = await setup();
    const paste = screen.getByTestId("paste-input-wrapper").props.onPaste;

    await act(async () => {
      const first = paste({
        type: "images",
        uris: [1, 2, 3, 4].map((number) => `file:///first-${number}.png`),
      });
      const second = paste({
        type: "images",
        uris: [1, 2, 3, 4].map((number) => `file:///second-${number}.png`),
      });
      await Promise.all([first, second]);
    });

    await waitFor(() => expect(onUploadAttachment).toHaveBeenCalledTimes(4));
    expect(screen.queryByText("second-1.png")).toBeNull();
  });

  it("holds Send while a pasted photo is read off the clipboard", async () => {
    const Clipboard = jest.requireMock("expo-clipboard");
    Clipboard.hasImageAsync.mockResolvedValueOnce(true);
    let finish: () => void = () => {};
    Clipboard.getImageAsync.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = () =>
            resolve({ data: "data:image/jpeg;base64,anBlZw==", size: { width: 4, height: 3 } });
        }),
    );
    choosePhotoSource(0);
    const onSend = jest.fn(async () => {});
    await setup(onSend);
    await fireEvent.changeText(input(), "with a photo");
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    await waitFor(() => expect(Clipboard.getImageAsync).toHaveBeenCalled());
    await fireEvent.press(screen.getByLabelText("Send"));
    expect(onSend).not.toHaveBeenCalled();
    finish();
    expect(await screen.findByText("Pasted photo.jpg")).toBeTruthy();
  });

  it("says why the server refused a photo", async () => {
    choosePhotoSource(0);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [{ uri: "file:///a.png", fileName: "a.png", mimeType: "image/png", fileSize: 10 }],
    });
    const refusal = "attachment bytes do not match the declared content type";
    await render(
      <RoomComposer
        members={members}
        placeholder="Message"
        disabled={false}
        reconnecting={false}
        onSend={jest.fn(async () => {})}
        onUploadAttachment={jest.fn().mockRejectedValue(new ApiError(400, {}, refusal))}
        draftKey={draftKey()}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    expect(await screen.findByText(refusal)).toBeTruthy();
    // Removing the photo takes its failure with it.
    await fireEvent.press(screen.getByLabelText("Remove a.png"));
    expect(screen.queryByText(refusal)).toBeNull();
  });

  it("keeps a failed upload on its own chip, retryable", async () => {
    choosePhotoSource(0);
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValue({
      canceled: false,
      assets: [
        { uri: "file:///a.png", fileName: "a.png", mimeType: "image/png", fileSize: 10 },
      ],
    });
    const onUploadAttachment = jest
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ id: "att-1" });
    const onSend = jest.fn(async () => {});
    await render(
      <RoomComposer
        members={members}
        placeholder="Message"
        disabled={false}
        reconnecting={false}
        onSend={onSend}
        onUploadAttachment={onUploadAttachment}
        draftKey={draftKey()}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    // The failure belongs to the photo, not to the message: the chip offers
    // the retry, and the rest of the draft is untouched.
    expect(await screen.findByLabelText("Retry uploading a.png")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Retry uploading a.png"));
    await waitFor(() => expect(screen.queryByLabelText("Retry uploading a.png")).toBeNull());
    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(expect.objectContaining({ attachmentIds: ["att-1"] })),
    );
    expect(onUploadAttachment).toHaveBeenCalledTimes(2);
  });

  it("draws the send button in ink and the field as a pill", async () => {
    await setup();
    await fireEvent.changeText(input(), "hello");
    expect(StyleSheet.flatten(screen.getByLabelText("Send").props.style).backgroundColor).toBe(
      chatPalette.light.fg,
    );
    expect(StyleSheet.flatten(screen.getByTestId("composer-pill").props.style)).toMatchObject({
      borderRadius: 20,
      backgroundColor: chatPalette.light.surface,
      borderColor: chatPalette.light.edge,
    });
  });

  it("puts the attach action on the leading plus", async () => {
    await setup();
    expect(screen.getByTestId("composer-plus")).toBeTruthy();
    expect(screen.getByLabelText("Attach photo")).toBeTruthy();
  });
});

describe("RoomComposer motion", () => {
  beforeEach(() => {
    mockInsets = { top: 0, bottom: 0, left: 0, right: 0 };
  });

  const bar = () => screen.getByTestId("composer-outer");
  const send = () => screen.getByLabelText("Send");
  /**
   * Spying on the mocked module is what distinguishes a SPRING from a style
   * flip: the mock resolves `withSpring` to its target value synchronously, so
   * the rendered style alone cannot tell the two apart — the call can.
   */
  let withSpring: jest.SpyInstance;
  beforeEach(() => {
    withSpring = jest.spyOn(Reanimated, "withSpring");
  });
  afterEach(() => withSpring.mockRestore());

  // Focus is the field's own outline firming up: no line across the screen,
  // and never a static style flipped on a `focused` boolean (a hard jump).
  it("springs the field's outline on focus, and draws no line against the timeline", async () => {
    await setup();
    const barStyle = () => StyleSheet.flatten(bar().props.style);
    const fieldStyle = () => StyleSheet.flatten(screen.getByTestId("composer-pill").props.style);
    expect(barStyle().borderTopWidth).toBeUndefined();
    expect(barStyle().shadowRadius).toBeUndefined();
    expect(barStyle().elevation).toBeUndefined();
    // At rest the animated style resolves to the palette's edge token.
    expect(fieldStyle().borderColor).toBe(chatPalette.light.edge);

    await fireEvent(input(), "focus");
    // Critically damped, both ways: a focus ring must never overshoot its colour.
    expect(withSpring).toHaveBeenLastCalledWith(1, expect.objectContaining({ dampingRatio: 1 }));
    await fireEvent(input(), "blur");
    expect(withSpring).toHaveBeenLastCalledWith(0, expect.objectContaining({ dampingRatio: 1 }));
    expect(fieldStyle().shadowRadius).toBeUndefined();
  });

  it("is one line tall at rest and grows from there", async () => {
    await setup();
    // Both scale with the reader's text size, so compare them to the leading.
    const field = StyleSheet.flatten(input().props.style);
    expect(field.minHeight / field.lineHeight).toBe(field.maxHeight / field.lineHeight / 5);
    expect(field.maxHeight / field.minHeight).toBe(5);
  });

  it("scales the send button down on press-IN, before the send is dispatched", async () => {
    const onSend = jest.fn(async () => {});
    await setup(onSend);
    await fireEvent.changeText(input(), "ping");
    withSpring.mockClear();

    await fireEvent(send(), "pressIn");
    expect(withSpring).toHaveBeenLastCalledWith(0.96, expect.objectContaining({ dampingRatio: 1 }));
    // Press-down feedback is not the send: nothing has gone out yet.
    expect(onSend).not.toHaveBeenCalled();

    await fireEvent(send(), "pressOut");
    expect(withSpring).toHaveBeenLastCalledWith(1, expect.objectContaining({ dampingRatio: 1 }));
  });
});

describe("RoomComposer outer padding", () => {
  beforeEach(() => {
    mockInsets = { top: 0, bottom: 0, left: 0, right: 0 };
  });

  const outerPadding = () =>
    StyleSheet.flatten(screen.getByTestId("composer-outer").props.style).paddingBottom;

  it("floors the outer padding at 8 (spacing.sm) when there is no bottom inset", async () => {
    mockInsets = { top: 20, bottom: 0, left: 0, right: 0 };
    await setup();
    expect(outerPadding()).toBe(8);
  });

  it("uses the bottom inset itself once it exceeds the floor", async () => {
    mockInsets = { top: 59, bottom: 34, left: 0, right: 0 };
    await setup();
    expect(outerPadding()).toBe(34);
  });
});
