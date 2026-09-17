import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomTimeline } from "../RoomTimeline";
import type { RoomMessage } from "../types";

// Pin "now" to a local calendar day (via local-time Date constructors, not UTC ISO
// literals) so the "Today" divider label doesn't depend on the host's wall clock
// or timezone. Only Date is faked so the other tests' real setTimeout/scroll
// timing is unaffected; the highlight test below fakes timers itself instead.
beforeEach(() => { vi.useFakeTimers({ now: new Date(2026, 8, 11, 12, 0, 0), toFake: ["Date"] }); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const base: RoomMessage = { messageId: "m", roomId: "r", messageSeq: 0, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Friday" }, text: "x", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-11T10:00:00Z", runId: null };
const msg = (seq: number, minute: number): RoomMessage => ({ ...base, messageId: `m${seq}`, messageSeq: seq, createdAt: new Date(2026, 8, 11, 10, minute).toISOString() });

function scrollable(region: HTMLElement, { height = 400, content = 1000, top }: { height?: number; content?: number; top: number }) {
  Object.defineProperty(region, "clientHeight", { configurable: true, value: height });
  Object.defineProperty(region, "scrollHeight", { configurable: true, value: content });
  let scrollTop = top;
  Object.defineProperty(region, "scrollTop", { configurable: true, get: () => scrollTop, set: (value: number) => { scrollTop = value; } });
}

const renderMessage = (message: RoomMessage, collapsed: boolean) => <p data-collapsed={collapsed}>{message.text}</p>;

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
  const pill = screen.getByRole("button", { name: "Jump to latest, 2 new messages" });
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

it("follows streaming growth while near the bottom", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null}>
    <p>First chunk</p>
  </RoomTimeline>);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 600 });
  fireEvent.scroll(region);
  Object.defineProperty(region, "scrollHeight", { configurable: true, value: 1200 });

  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null}>
    <p>First chunk and another streaming chunk</p>
  </RoomTimeline>);

  expect(region.scrollTop).toBe(1200);
  expect(screen.queryByRole("button", { name: /jump to latest/i })).not.toBeInTheDocument();
});

it("preserves an intentional scroll-away during streaming and offers a jump", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null}>
    <p>First chunk</p>
  </RoomTimeline>);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 0 });
  fireEvent.scroll(region);
  Object.defineProperty(region, "scrollHeight", { configurable: true, value: 1200 });

  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null}>
    <p>First chunk and another streaming chunk</p>
  </RoomTimeline>);

  expect(region.scrollTop).toBe(0);
  expect(screen.getByRole("button", { name: "Jump to latest" })).toBeInTheDocument();
});

it("preserves the visible history anchor when older messages are prepended", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(2, 1), msg(3, 2)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 200 });
  fireEvent.scroll(region);
  Object.defineProperty(region, "scrollHeight", { configurable: true, value: 1200 });

  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1), msg(3, 2)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);

  expect(region.scrollTop).toBe(400);
  expect(screen.queryByRole("button", { name: /jump to latest/i })).not.toBeInTheDocument();
});

it("starts a newly selected room at its latest content", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 0 });
  fireEvent.scroll(region);

  rerender(<RoomTimeline roomId="another" messages={[{ ...msg(2, 1), roomId: "another" }]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);

  expect(region.scrollTop).toBe(region.scrollHeight);
});

it("resumes follow mode after a successful send token", () => {
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} followToken={0} renderMessage={renderMessage} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 0 });
  fireEvent.scroll(region);

  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} followToken={1} renderMessage={renderMessage} empty={null}>
    <p>Sending…</p>
  </RoomTimeline>);

  expect(region.scrollTop).toBe(region.scrollHeight);
});

it("follows asynchronous image and layout growth without a message update", () => {
  let resized: ResizeObserverCallback | undefined;
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) { resized = callback; }
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null}>
    <div role="img" aria-label="Attachment" />
  </RoomTimeline>);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 600 });
  fireEvent.scroll(region);
  Object.defineProperty(region, "scrollHeight", { configurable: true, value: 1300 });

  act(() => { resized?.([], {} as ResizeObserver); });

  expect(region.scrollTop).toBe(1300);
});

it("uses instant motion for Jump to latest when reduced motion is requested", () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true } as MediaQueryList)));
  const { rerender } = render(<RoomTimeline roomId="r" messages={[msg(1, 0)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  const region = screen.getByRole("region", { name: "Messages" });
  scrollable(region, { top: 0 });
  const scrollTo = vi.fn(({ top }: ScrollToOptions) => { region.scrollTop = Number(top); });
  Object.defineProperty(region, "scrollTo", { configurable: true, value: scrollTo });
  fireEvent.scroll(region);
  rerender(<RoomTimeline roomId="r" messages={[msg(1, 0), msg(2, 1)]} readSeq={null} ownLastSentSeq={null} renderMessage={renderMessage} empty={null} />);
  scrollTo.mockClear();

  fireEvent.click(screen.getByRole("button", { name: "Jump to latest, 1 new message" }));

  expect(scrollTo).toHaveBeenCalledWith({ top: region.scrollHeight, behavior: "auto" });
});
