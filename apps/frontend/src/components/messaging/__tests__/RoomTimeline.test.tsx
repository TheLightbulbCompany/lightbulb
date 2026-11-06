import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomTimeline, StickToBottom } from "../RoomTimeline";
import type { RoomMessage } from "../types";

// Pin "now" to a local calendar day (via local-time Date constructors, not UTC ISO
// literals) so the "Today" divider label doesn't depend on the host's wall clock
// or timezone. Only Date is faked so the other tests' real setTimeout/scroll
// timing is unaffected; the highlight test below fakes timers itself instead.
beforeEach(() => { vi.useFakeTimers({ now: new Date(2026, 8, 11, 12, 0, 0), toFake: ["Date"] }); });
afterEach(() => { vi.useRealTimers(); });

const base: RoomMessage = { messageId: "m", roomId: "r", messageSeq: 0, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Friday" }, text: "x", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-11T10:00:00Z", runId: null };
const msg = (seq: number, minute: number): RoomMessage => ({ ...base, messageId: `m${seq}`, messageSeq: seq, createdAt: new Date(2026, 8, 11, 10, minute).toISOString() });

function scrollable(region: HTMLElement, { height = 400, content = 1000, top }: { height?: number; content?: number; top: number }) {
  Object.defineProperty(region, "clientHeight", { configurable: true, value: height });
  Object.defineProperty(region, "scrollHeight", { configurable: true, value: content });
  let scrollTop = top;
  Object.defineProperty(region, "scrollTop", { configurable: true, get: () => scrollTop, set: (value: number) => { scrollTop = value; } });
}

/** A real reader scrolling up: at the bottom first, then moving away. */
function scrollUp(region: HTMLElement, to = 0) {
  region.scrollTop = region.scrollHeight - region.clientHeight;
  fireEvent.scroll(region);
  region.scrollTop = to;
  fireEvent.scroll(region);
}

const renderMessage = (message: RoomMessage, collapsed: boolean) => <p data-collapsed={collapsed}>{message.text}</p>;

/**
 * tests/setup.ts installs a no-op ResizeObserver, so a component that reacts to
 * one can't be driven at all. Swap in a capturing one whose callback the test
 * fires by hand, then put the stub back.
 */
function captureResizeObserver() {
  const original = global.ResizeObserver;
  const callbacks: ResizeObserverCallback[] = [];
  class Capturing {
    constructor(callback: ResizeObserverCallback) { callbacks.push(callback); }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  global.ResizeObserver = Capturing as unknown as typeof ResizeObserver;
  return {
    resizeTo(region: HTMLElement, content: number) {
      Object.defineProperty(region, "scrollHeight", { configurable: true, value: content });
      act(() => { callbacks.forEach((callback) => callback([], {} as ResizeObserver)); });
    },
    restore() { global.ResizeObserver = original; },
  };
}

// A streaming run grows the timeline as tool rows arrive and shrinks it again
// when the trace folds on settle — all inside one message, so no seq changes and
// the growth effect never fires. Without height tracking the reader is left
// staring above the reply they were waiting for.
it("stays pinned to the bottom when live content grows and then shrinks", () => {
  const observer = captureResizeObserver();
  try {
    render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null}><p>live run</p></RoomTimeline>);
    const region = screen.getByRole("region", { name: "Messages" });
    scrollable(region, { top: 600 });
    observer.resizeTo(region, 2000);
    expect(region.scrollTop).toBe(2000);
    observer.resizeTo(region, 1200);
    expect(region.scrollTop).toBe(1200);
  } finally {
    observer.restore();
  }
});

it("never yanks a viewer who scrolled up when live content resizes", () => {
  const observer = captureResizeObserver();
  try {
    render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null}><p>live run</p></RoomTimeline>);
    const region = screen.getByRole("region", { name: "Messages" });
    scrollable(region, { top: 0 });
    scrollUp(region);
    observer.resizeTo(region, 2000);
    expect(region.scrollTop).toBe(0);
  } finally {
    observer.restore();
  }
});

// The owner's bug: a pinned reader's scroll event lands AFTER a streamed chunk
// taller than the threshold, so it reads "far from the bottom". That is content
// growth, not the reader leaving -- they must keep following.
it("keeps following when content outgrows the threshold before the scroll event lands", () => {
  const observer = captureResizeObserver();
  try {
    render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null}><p>live run</p></RoomTimeline>);
    const region = screen.getByRole("region", { name: "Messages" });
    scrollable(region, { top: 600 });
    fireEvent.scroll(region);
    Object.defineProperty(region, "scrollHeight", { configurable: true, value: 1300 });
    fireEvent.scroll(region);
    observer.resizeTo(region, 1600);
    expect(region.scrollTop).toBe(1600);
  } finally {
    observer.restore();
  }
});

it("takes the sender to the bottom (smoothly) on send, even when scrolled up", () => {
  const scrollTo = vi.fn(function (this: HTMLElement, options: ScrollToOptions) { this.scrollTop = options.top!; });
  Element.prototype.scrollTo = scrollTo as unknown as typeof Element.prototype.scrollTo;
  const props = { roomId: "r", messages: [msg(1, 0)], readSeq: null, ownLastSentSeq: null, renderMessage, empty: null };
  const { rerender } = render(<RoomTimeline {...props} sendToken={0} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 600 });
  scrollUp(region);
  rerender(<RoomTimeline {...props} sendToken={1} />);
  expect(scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: "smooth" });
  expect(region.scrollTop).toBe(1000);
  // ...and keeps following what arrives next (the Working card, the reply).
  rerender(<RoomTimeline {...props} messages={[msg(1, 0), msg(2, 1)]} sendToken={1} />);
  expect(region.scrollTop).toBe(region.scrollHeight);
  expect(screen.queryByRole("button", { name: /new message/ })).not.toBeInTheDocument();
});

it("follows a thread's replies and takes a thread sender to the bottom", () => {
  const observer = captureResizeObserver();
  try {
    const scrollTo = vi.fn(function (this: HTMLElement, options: ScrollToOptions) { this.scrollTop = options.top!; });
    Element.prototype.scrollTo = scrollTo as unknown as typeof Element.prototype.scrollTo;
    const { container, rerender } = render(<StickToBottom className="box" pinToken={0}><p>replies</p></StickToBottom>);
    const box = container.querySelector<HTMLElement>(".box")!;
    scrollable(box, { top: 0 });
    scrollUp(box);
    observer.resizeTo(box, 1500);
    expect(box.scrollTop).toBe(0);
    rerender(<StickToBottom className="box" pinToken={1}><p>replies</p></StickToBottom>);
    expect(box.scrollTop).toBe(1500);
    observer.resizeTo(box, 1800);
    expect(box.scrollTop).toBe(1800);
  } finally {
    observer.restore();
  }
});

it("renders separators for dates and the New line, and collapsed flags", () => {
  render(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={1} ownLastSentSeq={null} renderMessage={renderMessage} empty={<p>Empty</p>} />);
  expect(screen.getByRole("separator", { name: "Today" })).toBeInTheDocument();
  expect(screen.getByRole("separator", { name: "New messages" })).toBeInTheDocument();
  expect(screen.getAllByText("x").map((node) => node.getAttribute("data-collapsed"))).toEqual(["false", "false"]);
});

// The sticky pill must be confined to its own day (no stacking, no rule over text).
it("groups each day's rows under that day's divider", () => {
  const yesterday = { ...msg(1, 0), createdAt: new Date(2026, 8, 10, 10, 0).toISOString() };
  const { container } = render(<RoomTimeline roomId="r" messages={[yesterday, msg(2, 1), msg(3, 2)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  const days = [...container.querySelectorAll(".room-day")];
  expect(days.map((day) => [day.firstElementChild?.getAttribute("aria-label"), day.querySelectorAll("[data-message-id]").length])).toEqual([["Yesterday", 1], ["Today", 2]]);
});

it("shows the empty state when there are no messages", () => {
  render(<RoomTimeline roomId="r" messages={[]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={<p>Start the conversation</p>} />);
  expect(screen.getByText("Start the conversation")).toBeInTheDocument();
});

it("scrolls to the bottom on mount and when already at the bottom", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 600 });
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  expect(region.scrollTop).toBe(region.scrollHeight);
  expect(screen.queryByRole("button", { name: /new message/ })).not.toBeInTheDocument();
});

it("shows a pill instead of yanking the viewport when scrolled up, and clears it on click", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 0 });
  scrollUp(region);
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1), msg(3, 2)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  expect(region.scrollTop).toBe(0);
  const pill = screen.getByRole("button", { name: "2 new messages" });
  fireEvent.click(pill);
  expect(region.scrollTop).toBe(region.scrollHeight);
  expect(screen.queryByRole("button", { name: /new message/ })).not.toBeInTheDocument();
});

it("always scrolls to the bottom for the viewer's own send", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 0 });
  scrollUp(region);
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={null} ownLastSentSeq={2} renderMessage={renderMessage} empty={null} />);
  expect(region.scrollTop).toBe(region.scrollHeight);
});

it("scrolls to and highlights a target message", () => {
  vi.useRealTimers();
  vi.useFakeTimers({ now: new Date(2026, 8, 11, 12, 0, 0), toFake: ["Date", "setTimeout", "clearTimeout"] });
  const scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView;
  render(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={null} ownLastSentSeq={null} targetMessageId="m1" renderMessage={renderMessage} empty={null} />);
  const article = screen.getAllByText("x")[0].closest("[data-message-id]")!;
  expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });
  expect(article).toHaveAttribute("data-highlight", "true");
  act(() => { vi.advanceTimersByTime(2600); });
  expect(article).not.toHaveAttribute("data-highlight");
  vi.useRealTimers();
});

it("re-scrolls to the same target message when its token bumps", () => {
  const scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView;
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={null} ownLastSentSeq={null} targetMessageId="m1" targetToken={1} renderMessage={renderMessage} empty={null} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);
  // Same targetMessageId, same token: re-selecting is a no-op (already handled).
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={null} ownLastSentSeq={null} targetMessageId="m1" targetToken={1} renderMessage={renderMessage} empty={null} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);
  // Same targetMessageId, new token (re-selecting the same search result): scrolls again.
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={null} ownLastSentSeq={null} targetMessageId="m1" targetToken={2} renderMessage={renderMessage} empty={null} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(2);
});

it("keeps message bodies stable across highlights while honoring content and action changes", () => {
  vi.useRealTimers();
  vi.useFakeTimers({ now: new Date(2026, 8, 11, 12, 0, 0), toFake: ["Date", "setTimeout", "clearTimeout"] });
  const messages = [msg(1, 0), msg(2, 1)];
  const draw = vi.fn(renderMessage);
  const props = { roomId: "r", messages, readSeq: null, ownLastSentSeq: null, renderMessage: draw, empty: null };
  const { container, rerender } = render(<RoomTimeline {...props} />);
  expect(draw).toHaveBeenCalledTimes(2);
  draw.mockClear();

  rerender(<RoomTimeline {...props} targetMessageId="m2" />);
  const target = container.querySelector('[data-message-id="m2"]')!;
  expect(target).toHaveAttribute("data-highlight", "true");
  expect(draw).not.toHaveBeenCalled();
  act(() => { vi.advanceTimersByTime(2600); });
  expect(target).not.toHaveAttribute("data-highlight");
  expect(draw).not.toHaveBeenCalled();

  const onAction = vi.fn();
  const nextDraw = vi.fn((message: RoomMessage, collapsed: boolean) => <button data-collapsed={collapsed} onClick={() => onAction(message.messageId)}>{message.text}</button>);
  rerender(<RoomTimeline {...props} renderMessage={nextDraw} />);
  expect(nextDraw).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getAllByRole("button", { name: "x" })[1]);
  expect(onAction).toHaveBeenCalledWith("m2");

  rerender(<RoomTimeline {...props} renderMessage={nextDraw} messages={[{ ...messages[0], text: "Edited" }, messages[1]]} readSeq={1} />);
  expect(screen.getByRole("button", { name: "Edited" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "x" })).toHaveAttribute("data-collapsed", "false");
});

it("reads older history when the reader scrolls to the top, and leaves a failure to a retry", () => {
  const onLoadOlder = vi.fn();
  const props = { roomId: "r", readSeq: null, ownLastSentSeq: null, renderMessage, empty: null, historyComplete: false, onLoadOlder };
  const { rerender } = render(<RoomTimeline {...props} messages={[msg(5, 4), msg(6, 5)]} historyLoading />);
  const region = screen.getByRole("region", { name: "Messages" });
  expect(onLoadOlder).not.toHaveBeenCalled(); // a page is already on its way
  // The page lands with the reader far from the top: nothing more is asked for.
  scrollable(region, { top: 900 });
  rerender(<RoomTimeline {...props} messages={[msg(3, 2), msg(4, 3), msg(5, 4), msg(6, 5)]} />);
  expect(onLoadOlder).not.toHaveBeenCalled();
  scrollable(region, { top: 100 });
  fireEvent.scroll(region);
  expect(onLoadOlder).toHaveBeenCalledOnce();
  fireEvent.scroll(region);
  expect(onLoadOlder).toHaveBeenCalledOnce();

  rerender(<RoomTimeline {...props} messages={[msg(3, 2), msg(4, 3), msg(5, 4), msg(6, 5)]} historyError="offline" />);
  expect(screen.getByRole("alert")).toHaveTextContent("Could not load older messages.");
  fireEvent.scroll(region);
  expect(onLoadOlder).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(onLoadOlder).toHaveBeenCalledTimes(2);
});

it("keeps the reader's place when a retried page lands", () => {
  let rowTop = 30;
  const rect = vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const top = this.getAttribute("data-message-id") === "m5" ? rowTop : 0;
    return { top, bottom: top + 40 } as DOMRect;
  });
  try {
    const onLoadOlder = vi.fn();
    const props = { roomId: "r", readSeq: null, ownLastSentSeq: null, renderMessage, empty: null, onLoadOlder };
    const { rerender } = render(<RoomTimeline {...props} messages={[msg(5, 4), msg(6, 5)]} historyComplete={false} historyError="offline" />);
    const region = screen.getByRole("region", { name: "Messages" });
    scrollable(region, { top: 60 });
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onLoadOlder).toHaveBeenCalledOnce();
    rowTop = 230;
    rerender(<RoomTimeline {...props} messages={[msg(3, 2), msg(4, 3), msg(5, 4), msg(6, 5)]} historyComplete />);
    expect(region.scrollTop).toBe(260);
  } finally {
    rect.mockRestore();
  }
});

it("renders the leading node above the oldest message, and alone when there are no messages", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage}
    leading={<p>This is the beginning of #general.</p>} empty={null} />);
  expect(screen.getByText("This is the beginning of #general.")).toBeInTheDocument();
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage}
    leading={<p>This is the beginning of #general.</p>} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  expect(region.firstElementChild).toHaveTextContent("This is the beginning of #general.");
});

it("keeps the visible message in place when older history is prepended", () => {
  let rowTop = 20;
  const rect = vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const top = this.getAttribute("data-message-id") === "m2" ? rowTop : 0;
    return { top, bottom: top + 40 } as DOMRect;
  });
  try {
    const onLoadOlder = vi.fn();
    const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(2, 1), msg(3, 2)]} readSeq={null}
      ownLastSentSeq={null} renderMessage={renderMessage} empty={null} historyComplete={false} onLoadOlder={onLoadOlder} />);
    const region = screen.getByRole("region", { name: "Messages" });
    // Nothing to press: a window at its top asks by itself, once.
    expect(screen.queryByRole("button", { name: /older/i })).not.toBeInTheDocument();
    expect(onLoadOlder).toHaveBeenCalledOnce();
    scrollable(region, { top: 40 });
    rerender(<RoomTimeline roomId="r" messages={[msg(2, 1), msg(3, 2)]} readSeq={null} historyLoading
      ownLastSentSeq={null} renderMessage={renderMessage} empty={null} historyComplete={false} onLoadOlder={onLoadOlder} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading older messages…");
    rowTop = 120;
    rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1), msg(3, 2)]} readSeq={null}
      ownLastSentSeq={null} renderMessage={renderMessage} empty={null} historyComplete={true} onLoadOlder={onLoadOlder} />);
    expect(region.scrollTop).toBe(140);
    expect(onLoadOlder).toHaveBeenCalledOnce();
  } finally {
    rect.mockRestore();
  }
});
