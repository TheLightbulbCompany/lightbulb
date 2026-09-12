/**
 * "Jump to latest" — and, far more importantly, when it must NOT appear.
 *
 * The gating is the whole feature. Upstream shipped this affordance gated on
 * "newer content exists below" alone and it flashed on every single reply,
 * because that condition is structurally true from the first streaming tick of
 * every turn — the transcript always has content arriving at its tail. The
 * shipped predicate (OpenClawChatUI/ChatView.swift,
 * `chatReaderShowsJumpToLatest`) therefore ANDs it with live-edge geometry.
 *
 * Ours does the same: the button appears only when the reader has scrolled
 * away AND something arrived since they did. Either half alone is a bug —
 * "content arrived" alone flashes on every reply, "scrolled away" alone parks
 * a permanent button over a static transcript someone is re-reading.
 *
 * Geometry is driven the way MessageList.scrollContainment.test.tsx drives it:
 * real scrollTop/scrollHeight/clientHeight on the container plus a stubbed
 * rAF, because the component measures the real thing at a 120px threshold and
 * coalesces scroll events to one measurement per frame.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

function streamGroup(text: string): RenderGroup[] {
  return [
    {
      side: "assistant",
      key: "g-stream",
      items: [{ kind: "stream", key: "g-stream:s", text, runId: "run-1" }],
    },
  ];
}

let frames: FrameRequestCallback[] = [];

function installFrameStub() {
  frames = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    frames.push(cb);
    return frames.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
}

/** Let the coalescing window turn over so the next scroll event re-measures. */
function nextFrame() {
  const pending = frames;
  frames = [];
  for (const cb of pending) cb(0);
}

function renderStream(text: string) {
  return render(
    <MessageList
      renderGroups={streamGroup(text)}
      isStreaming
      agentName="Friday"
    />,
  );
}

/** 1000px of content through a 200px viewport. */
function sizedScroller(container: HTMLElement): HTMLElement {
  const el = container.firstElementChild as HTMLElement;
  Object.defineProperty(el, "scrollHeight", { value: 1000, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: 200, configurable: true });
  return el;
}

function scrollTo(scroller: HTMLElement, top: number) {
  scroller.scrollTop = top;
  fireEvent.scroll(scroller);
  nextFrame();
}

const jumpButton = () => screen.queryByRole("button", { name: /jump to latest/i });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("MessageList — jump to latest", () => {
  it("stays hidden while the reader is at the live edge and content streams in", () => {
    installFrameStub();
    const { container, rerender } = renderStream("chunk");
    const scroller = sizedScroller(container);

    // distance = 1000 - 800 - 200 = 0 → inside the 120px near-bottom band.
    scrollTo(scroller, 800);
    rerender(
      <MessageList
        renderGroups={streamGroup("chunk chunk")}
        isStreaming
        agentName="Friday"
      />,
    );

    expect(jumpButton()).toBeNull();
  });

  it("stays hidden when the reader scrolled away but nothing new has arrived", () => {
    installFrameStub();
    const { container } = renderStream("chunk");
    const scroller = sizedScroller(container);

    scrollTo(scroller, 0);

    expect(jumpButton()).toBeNull();
  });

  it("appears only once BOTH hold — scrolled away and content arrived since", () => {
    installFrameStub();
    const { container, rerender } = renderStream("chunk");
    const scroller = sizedScroller(container);

    scrollTo(scroller, 0);
    rerender(
      <MessageList
        renderGroups={streamGroup("chunk chunk")}
        isStreaming
        agentName="Friday"
      />,
    );

    expect(jumpButton()).toBeInTheDocument();
  });

  it("clicking it returns to the bottom and dismisses itself", async () => {
    installFrameStub();
    const { container, rerender } = renderStream("chunk");
    const scroller = sizedScroller(container);

    scrollTo(scroller, 0);
    rerender(
      <MessageList
        renderGroups={streamGroup("chunk chunk")}
        isStreaming
        agentName="Friday"
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /jump to latest/i }));

    expect(scroller.scrollTop).toBe(1000);
    expect(jumpButton()).toBeNull();
  });

  it("re-pins autoscroll after the jump, so later chunks keep the view at the bottom", async () => {
    installFrameStub();
    const { container, rerender } = renderStream("chunk");
    const scroller = sizedScroller(container);

    scrollTo(scroller, 0);
    rerender(
      <MessageList
        renderGroups={streamGroup("chunk chunk")}
        isStreaming
        agentName="Friday"
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /jump to latest/i }));

    // A jump that leaves isNearBottom false would strand the reader at the
    // bottom with the button re-appearing on the very next chunk.
    scroller.scrollTop = 0;
    rerender(
      <MessageList
        renderGroups={streamGroup("chunk chunk chunk")}
        isStreaming
        agentName="Friday"
      />,
    );

    expect(scroller.scrollTop).toBe(1000);
    expect(jumpButton()).toBeNull();
  });
});
