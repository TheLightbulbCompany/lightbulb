import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomTimeline } from "../RoomTimeline";
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
    fireEvent.scroll(region);
    observer.resizeTo(region, 2000);
    expect(region.scrollTop).toBe(0);
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
  fireEvent.scroll(region);
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
  fireEvent.scroll(region);
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
  act(() => { vi.advanceTimersByTime(2000); });
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

it("renders the leading node above the oldest message, and alone when there are no messages", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage}
    leading={<p>This is the beginning of #general.</p>} empty={null} />);
  expect(screen.getByText("This is the beginning of #general.")).toBeInTheDocument();
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage}
    leading={<p>This is the beginning of #general.</p>} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  expect(region.firstElementChild).toHaveTextContent("This is the beginning of #general.");
});
