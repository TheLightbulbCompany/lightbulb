import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { ApiError, type LiveEvent, type SessionScreen } from "@lightbulb/shared";

import { WATCH_EVERY_MS } from "../../../lib/sessionTerminal";
import { SessionTerminal } from "../SessionTerminal";

const mockInjected: string[] = [];
let mockOnMessage: ((event: { nativeEvent: { data: string } }) => void) | undefined;
let mockEnded: (() => void) | undefined;
jest.mock("react-native-webview", () => {
  const react = require("react");
  const { View } = require("react-native");
  return {
    WebView: react.forwardRef((props: { onMessage: typeof mockOnMessage; onContentProcessDidTerminate: () => void; testID: string }, ref: unknown) => {
      mockOnMessage = props.onMessage;
      mockEnded = props.onContentProcessDidTerminate;
      react.useImperativeHandle(ref, () => ({ injectJavaScript: (js: string) => { mockInjected.push(js); } }));
      return react.createElement(View, { testID: props.testID });
    }),
  };
});
const mockClipboard = jest.fn(async () => "from clipboard");
jest.mock("expo-clipboard", () => ({ getStringAsync: () => mockClipboard() }), { virtual: true });

const handlers = new Set<(event: LiveEvent) => void>();
const onEvent = (handler: (event: LiveEvent) => void) => { handlers.add(handler); return () => { handlers.delete(handler); }; };
const session = { sessionId: "s", generation: "g" };
const frame = (kind: SessionScreen["kind"], seq: number, more: Partial<SessionScreen> = {}): SessionScreen =>
  ({ sessionId: "s", generation: "g", epoch: 1, seq, cols: 80, rows: 24, kind, data: btoa(`${kind}${seq}`), ...more });
const emit = (payload: SessionScreen) => act(() => { handlers.forEach((h) => h({ type: "workspace.session.screen", payload } as LiveEvent)); });
const fromPage = (message: object) => act(async () => { mockOnMessage?.({ nativeEvent: { data: JSON.stringify(message) } }); for (let i = 0; i < 50; i += 1) await Promise.resolve(); });
/** What the page was told, in order. */
const told = () => mockInjected.map((js) => JSON.parse(js.slice("window.lb(".length, js.lastIndexOf(");true;"))) as { t: string; [k: string]: unknown });

const watch = jest.fn(async (_sessionId: string, _fresh?: boolean) => {});
const input = jest.fn(async (_data: string, _epoch: number) => {});
const onLive = jest.fn();
type Props = Partial<Parameters<typeof SessionTerminal>[0]>;
const view = (props: Props = {}) => <SessionTerminal session={session} onEvent={onEvent} watch={watch} onLive={onLive} active {...props} />;

beforeEach(() => {
  jest.useFakeTimers();
  mockInjected.length = 0; handlers.clear();
  watch.mockReset(); watch.mockResolvedValue(undefined);
  input.mockReset(); input.mockResolvedValue(undefined);
  onLive.mockReset(); mockClipboard.mockClear();
});
afterEach(() => jest.useRealTimers());

describe("SessionTerminal", () => {
  it("hosts xterm from the bundled page, with no network address", async () => {
    await render(view());
    expect(screen.getByTestId("session-terminal")).toBeTruthy();
    expect(screen.getByText("Connecting…")).toBeTruthy();
  });

  it("asks for a fresh screen on open and renews every 15 s", async () => {
    await render(view());
    expect(watch.mock.calls).toEqual([["s", true]]);
    await act(async () => { jest.advanceTimersByTime(WATCH_EVERY_MS * 2); });
    expect(watch.mock.calls).toEqual([["s", true], ["s", false], ["s", false]]);
  });

  it("stops watching while the screen is away and asks fresh on return", async () => {
    const { rerender } = await render(view());
    await rerender(view({ active: false }));
    await act(async () => { jest.advanceTimersByTime(WATCH_EVERY_MS * 3); });
    expect(watch).toHaveBeenCalledTimes(1);
    await rerender(view({ active: true }));
    expect(watch.mock.calls).toEqual([["s", true], ["s", true]]);
  });

  it("does not ask fresh again when only the watch function changes, and does for the kept screen", async () => {
    const { rerender } = await render(view());
    const again = jest.fn(async () => {});
    await rerender(view({ watch: again }));
    expect(again.mock.calls).toEqual([["s", false]]);
    await rerender(view({ watch: again, kept: true }));
    expect(again.mock.calls).toEqual([["s", false], ["s", true]]);
  });

  it("stops renewing once the watch is refused", async () => {
    watch.mockRejectedValue(new ApiError(403, null));
    await render(view());
    await act(async () => { jest.advanceTimersByTime(WATCH_EVERY_MS * 3); });
    expect(watch).toHaveBeenCalledTimes(1);
  });

  it("says a full session is full, stops asking, and asks again on return", async () => {
    watch.mockRejectedValue(new ApiError(409, { code: "LOCAL_SESSION_VIEWERS_FULL" }, "This session already has 20 live viewers."));
    const { rerender } = await render(view());
    expect(await screen.findByText("This session already has 20 live viewers.")).toBeTruthy();
    await act(async () => { jest.advanceTimersByTime(WATCH_EVERY_MS * 3 + 30_000); });
    expect(watch).toHaveBeenCalledTimes(1);
    // Never the "update its Lightbulb app" line: the Mac is fine.
    expect(screen.queryByText(/Update its Lightbulb app/)).toBeNull();
    // Away and back: a seat may have freed.
    watch.mockResolvedValue(undefined);
    await rerender(view({ active: false }));
    await rerender(view({ active: true }));
    expect(watch.mock.calls).toEqual([["s", true], ["s", true]]);
    expect(screen.queryByText("This session already has 20 live viewers.")).toBeNull();
    expect(screen.getByText("Connecting…")).toBeTruthy();
  });

  it("holds frames until the page is ready, then writes them in order", async () => {
    await render(view());
    await emit(frame("history", 0));
    await emit(frame("snapshot", 1));
    expect(mockInjected).toEqual([]);
    await fromPage({ t: "ready" });
    expect(told().map((m) => m.t)).toEqual(["canType", "mode", "draw"]);
    expect(told()[2].steps).toHaveLength(2);
    expect(onLive).toHaveBeenLastCalledWith(true);
    expect(screen.queryByText("Connecting…")).toBeNull();
    await emit(frame("output", 2));
    expect(told()).toHaveLength(4);
  });

  it("asks the Mac for a fresh screen when frames were lost, and not while away", async () => {
    const { rerender } = await render(view());
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 0));
    await emit(frame("snapshot", 2));
    await act(async () => { jest.advanceTimersByTime(300); });
    expect(watch.mock.calls).toEqual([["s", true], ["s", true]]);
    await rerender(view({ active: false }));
    await act(async () => { jest.advanceTimersByTime(30_000); });
    await emit(frame("snapshot", 5));
    await act(async () => { jest.advanceTimersByTime(300); });
    expect(watch).toHaveBeenCalledTimes(2);
  });

  it("ignores another session's frames and another generation's", async () => {
    await render(view());
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 0, { sessionId: "other" }));
    await emit(frame("snapshot", 0, { generation: "older" }));
    expect(told().filter((m) => m.t === "draw")).toEqual([]);
  });

  it("says Reconnecting once a picture it had goes stale", async () => {
    await render(view());
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 0));
    await act(async () => { jest.advanceTimersByTime(30_000); });
    expect(screen.getByText("Reconnecting…")).toBeTruthy();
  });

  it("is read-only for a viewer: no key bar, and nothing typed is sent", async () => {
    await render(view({ input, admitted: false }));
    await fromPage({ t: "ready" });
    expect(told()[0]).toEqual({ t: "canType", can: false });
    expect(screen.queryByLabelText("Terminal keys")).toBeNull();
    await fromPage({ t: "data", d: "ls\r" });
    expect(input).not.toHaveBeenCalled();
  });

  it("sends an admitted person's keys with the screen's epoch", async () => {
    const onCanType = jest.fn();
    await render(view({ input, admitted: true, onCanType }));
    await fromPage({ t: "ready" });
    // No picture yet: there is no screen epoch to type against, so no keyboard, no key bar, nothing sent.
    expect(told()[0]).toEqual({ t: "canType", can: false });
    expect(onCanType).toHaveBeenLastCalledWith(false);
    expect(screen.queryByLabelText("Terminal keys")).toBeNull();
    await fromPage({ t: "data", d: "rm" });
    expect(input).not.toHaveBeenCalled();
    await emit(frame("snapshot", 0, { epoch: 4 }));
    expect(told().at(-1)).toEqual({ t: "canType", can: true });
    expect(onCanType).toHaveBeenLastCalledWith(true);
    await fromPage({ t: "data", d: "y" });
    expect(input.mock.calls.map(([data, epoch]) => [atob(data), epoch])).toEqual([["y", 4]]);
  });

  it("offers the keys a phone lacks; Ctrl holds for the next key", async () => {
    await render(view({ input, admitted: true }));
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 0));
    await fireEvent.press(screen.getByLabelText("Escape"));
    await fireEvent.press(screen.getByLabelText("Up arrow"));
    expect(told().slice(-2)).toEqual([{ t: "key", key: "esc" }, { t: "key", key: "up" }]);
    await fireEvent.press(screen.getByLabelText("Control, applies to the next key"));
    expect(screen.getByLabelText("Control, applies to the next key").props.accessibilityState).toMatchObject({ selected: true });
    await fromPage({ t: "data", d: "c" });
    await fromPage({ t: "data", d: "c" });
    expect(input.mock.calls.map(([data]) => atob(data))).toEqual(["\x03", "c"]);
    expect(screen.getByLabelText("Control, applies to the next key").props.accessibilityState).toMatchObject({ selected: false });
  });

  it("pastes the clipboard through the terminal", async () => {
    await render(view({ input, admitted: true }));
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 0));
    await fireEvent.press(screen.getByLabelText("Paste"));
    await act(async () => { for (let i = 0; i < 10; i += 1) await Promise.resolve(); });
    expect(told().at(-1)).toEqual({ t: "paste", d: "from clipboard" });
  });

  it("stops typing once the Mac refuses it", async () => {
    const onCanType = jest.fn();
    input.mockRejectedValue(new ApiError(409, { code: "LOCAL_SESSION_UNSUPPORTED" }));
    await render(view({ input, admitted: true, onCanType }));
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 0));
    await fromPage({ t: "data", d: "a" });
    expect(onCanType).toHaveBeenLastCalledWith(false);
    expect(told().at(-1)).toEqual({ t: "canType", can: false });
    expect(screen.queryByLabelText("Terminal keys")).toBeNull();
  });

  it("switches between Fit and Readable", async () => {
    await render(view());
    await fromPage({ t: "ready" });
    expect(told()[1]).toEqual({ t: "mode", mode: "fit" });
    await fireEvent.press(screen.getByText("Readable"));
    expect(told().at(-1)).toEqual({ t: "mode", mode: "readable" });
  });

  it("remembers Readable on this phone", async () => {
    const first = await render(view());
    await fireEvent.press(screen.getByText("Readable"));
    await first.unmount();
    mockInjected.length = 0;
    await render(view());
    await fromPage({ t: "ready" });
    expect(told()[1]).toEqual({ t: "mode", mode: "readable" });
    // Back to the default for the tests that follow: the store is the module's own.
    await fireEvent.press(screen.getByText("Fit"));
  });

  it("keeps every key on one row: nothing to scroll to", async () => {
    await render(view({ input, admitted: true }));
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 0));
    const bar = screen.getByLabelText("Terminal keys");
    expect(bar.type).toBe("View");
    for (const name of ["Escape", "Tab", "Control, applies to the next key", "Left arrow", "Down arrow", "Up arrow", "Right arrow", "Paste", "Show or hide the keyboard"]) {
      expect(screen.getByLabelText(name)).toBeTruthy();
    }
    await fireEvent.press(screen.getByLabelText("Show or hide the keyboard"));
    expect(told().at(-1)).toEqual({ t: "keyboard" });
  });

  it("starts over with a fresh ask when iOS ends the page's process", async () => {
    await render(view());
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 0));
    expect(screen.queryByText("Connecting…")).toBeNull();
    mockInjected.length = 0;
    onLive.mockClear();
    await act(async () => { mockEnded?.(); });
    expect(watch.mock.calls.at(-1)).toEqual(["s", true]);
    // The new page is empty: it says so, and after 30 s with no picture it says that too.
    expect(onLive).toHaveBeenLastCalledWith(false);
    expect(screen.getByText("Connecting…")).toBeTruthy();
    await emit(frame("snapshot", 1));
    expect(mockInjected).toEqual([]);
    await fromPage({ t: "ready" });
    // A new page is empty: it is told everything again, and the picture starts over.
    expect(told().map((m) => m.t)).toEqual(["canType", "mode", "draw"]);
    expect((told()[2].steps as { fresh: boolean }[])[0].fresh).toBe(true);
  });

  it("keeps a held Ctrl through a page restart: what the bar shows is what the next key gets", async () => {
    await render(view({ input, admitted: true }));
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 0));
    await fireEvent.press(screen.getByLabelText("Control, applies to the next key"));
    await act(async () => { mockEnded?.(); });
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 1));
    expect(screen.getByLabelText("Control, applies to the next key").props.accessibilityState).toMatchObject({ selected: true });
    await fromPage({ t: "data", d: "c" });
    expect(input.mock.calls.map(([data]) => atob(data))).toEqual(["\x03"]);
    expect(screen.getByLabelText("Control, applies to the next key").props.accessibilityState).toMatchObject({ selected: false });
  });

  it("goes read-only when the page restarts, until its first picture", async () => {
    await render(view({ input, admitted: true }));
    await fromPage({ t: "ready" });
    await emit(frame("snapshot", 0));
    expect(screen.getByLabelText("Terminal keys")).toBeTruthy();
    await act(async () => { mockEnded?.(); });
    expect(screen.queryByLabelText("Terminal keys")).toBeNull();
    await fromPage({ t: "ready" });
    await fromPage({ t: "data", d: "x" });
    expect(input).not.toHaveBeenCalled();
    await act(async () => { jest.advanceTimersByTime(30_000); });
    expect(screen.getByText(/Nothing has arrived from the Mac in 30 seconds/)).toBeTruthy();
    await emit(frame("snapshot", 1));
    expect(screen.getByLabelText("Terminal keys")).toBeTruthy();
  });
});
