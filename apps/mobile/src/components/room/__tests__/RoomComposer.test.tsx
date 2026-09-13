import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { StyleSheet } from "react-native";
import * as Reanimated from "react-native-reanimated";
import { colors } from "../../../config/theme";
import type { RoomMember } from "@lightbulb/shared";
import { RoomComposer } from "../RoomComposer";

// `attachments.ts` → `adapter.ts` reaches expo-crypto at import time.
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

// RNTL 14's render/fireEvent are async — every call here is awaited, the
// convention the other room suites already follow.
const setup = async (
  onSend = jest.fn(async () => {}),
  onTypingChange?: (typing: boolean) => void,
) => {
  const onUploadAttachment = jest.fn(async () => ({ id: "att-1" }));
  await render(
    <RoomComposer
      members={members}
      placeholder="Message"
      disabled={false}
      reconnecting={false}
      onSend={onSend}
      onUploadAttachment={onUploadAttachment}
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

describe("RoomComposer", () => {
  beforeEach(() => {
    mockInsets = { top: 0, bottom: 0, left: 0, right: 0 };
  });

  it("opens the mention typeahead on @ and completes the picked handle", async () => {
    await setup();
    await caretAt(4);
    await fireEvent.changeText(input(), "hi @f");
    await caretAt(5);
    await waitFor(() => expect(screen.getByText("@friday")).toBeTruthy());
    await fireEvent.press(screen.getByText("@friday"));
    expect(input().props.value).toBe("hi @friday ");
  });

  it("matches a human by display name with an internal space", async () => {
    await setup();
    await fireEvent.changeText(input(), "@Aden K");
    await caretAt(7);
    await waitFor(() => expect(screen.getByText("Aden Kim")).toBeTruthy());
    // A member with no server-side handle still says what it is.
    expect(screen.getByText("Person")).toBeTruthy();
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

  it("uploads a picked photo and sends its attachment id", async () => {
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [{ uri: "file:///a.png", fileName: "a.png", mimeType: "image/png", fileSize: 10 }],
    });
    const onSend = jest.fn(async () => {});
    const { onUploadAttachment } = await setup(onSend);
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    await waitFor(() =>
      expect(onUploadAttachment).toHaveBeenCalledWith({
        uri: "file:///a.png",
        name: "a.png",
        type: "image/png",
      }),
    );
    // The chip proves the photo is attached before the message goes out.
    expect(screen.getByText("a.png")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(expect.objectContaining({ attachmentIds: ["att-1"] })),
    );
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

  it("rejects a photo over the 10MB cap without uploading it", async () => {
    const ImagePicker = jest.requireMock("expo-image-picker");
    ImagePicker.launchImageLibraryAsync.mockResolvedValueOnce({
      canceled: false,
      assets: [
        { uri: "file:///big.png", fileName: "big.png", mimeType: "image/png", fileSize: 11e6 },
      ],
    });
    const { onUploadAttachment } = await setup();
    await fireEvent.press(screen.getByLabelText("Attach photo"));
    await waitFor(() => expect(screen.getByText("big.png exceeds the 10MB limit")).toBeTruthy());
    expect(onUploadAttachment).not.toHaveBeenCalled();
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

  // The defect this replaces: `focused && styles.cardFocused` swapped a border
  // colour and a 16→24pt shadow with no animation at all — a hard visual jump
  // on every tap into the composer.
  it("springs the bar's hairline on focus instead of flipping a static style", async () => {
    await setup();
    const style = () => StyleSheet.flatten(bar().props.style);
    // The bar IS the hairline — flat on the page ground, no card left.
    expect(style().borderTopWidth).toBe(StyleSheet.hairlineWidth);
    expect(style().shadowRadius).toBeUndefined();
    expect(style().elevation).toBeUndefined();
    expect(style().borderRadius).toBeUndefined();
    // At rest the animated style resolves to the plain edge token.
    expect(style().borderTopColor).toBe(colors.edge);

    await fireEvent(input(), "focus");
    // Critically damped, both ways: a focus ring must never overshoot its colour.
    expect(withSpring).toHaveBeenLastCalledWith(1, expect.objectContaining({ dampingRatio: 1 }));
    await fireEvent(input(), "blur");
    expect(withSpring).toHaveBeenLastCalledWith(0, expect.objectContaining({ dampingRatio: 1 }));
    // …and no second static variant appeared to do the work instead.
    expect(style().shadowRadius).toBeUndefined();
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
