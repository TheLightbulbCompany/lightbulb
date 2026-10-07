/**
 * How MessageList moves its own scroll position.
 *
 * Scroll containment: MessageList must scroll ONLY its own overflow
 * container. `scrollIntoView` walks every scrollable ancestor — including
 * the overflow-clipped workspace shell, which Chrome will still scroll
 * programmatically — shifting the entire app upward and leaving a blank
 * void the user cannot scroll back from. Neither the auto-snap effect nor
 * the imperative send-path `scrollToBottom` may ever call it.
 *
 * Scroll throttling: the pinned-to-bottom measurement runs at most once per
 * animation frame, and only ever from a real scroll event — never from a
 * deferred read that would see a streaming commit's fresh content and
 * mistake it for the user having scrolled away.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render, fireEvent } from "@testing-library/react";
import { MessageList, type MessageListHandle } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

function userGroup(key: string, text: string): RenderGroup {
  return {
    side: "user",
    key,
    items: [{ kind: "message", key: `${key}:m`, message: { role: "user", text } }],
  };
}

function streamGroup(text: string): RenderGroup {
  return {
    side: "assistant",
    key: "g-stream",
    items: [{ kind: "stream", key: "g-stream:s", text, runId: "run-1" }],
  };
}

function renderList(
  groups: RenderGroup[],
  ref?: React.Ref<MessageListHandle>,
) {
  return render(
    <MessageList
      ref={ref}
      renderGroups={groups}
      isStreaming={false}
      agentName="Friday"
    />,
  );
}

function scrollerOf(container: HTMLElement): HTMLElement {
  const el = container.firstElementChild as HTMLElement;
  Object.defineProperty(el, "scrollHeight", { value: 640, configurable: true });
  return el;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MessageList scroll containment", () => {
  it("snaps its own container — never scrollIntoView — when a user message arrives", () => {
    const siv = vi.spyOn(Element.prototype, "scrollIntoView");
    const { container, rerender } = renderList([userGroup("g1", "hi")]);
    const scroller = scrollerOf(container);

    rerender(
      <MessageList
        renderGroups={[userGroup("g1", "hi"), userGroup("g2", "again")]}
        isStreaming={false}
        agentName="Friday"
      />,
    );

    expect(siv).not.toHaveBeenCalled();
    expect(scroller.scrollTop).toBe(640);
  });

  it("scrollToBottom (send path) scrolls the container only", () => {
    const siv = vi.spyOn(Element.prototype, "scrollIntoView");
    const ref = React.createRef<MessageListHandle>();
    const { container } = renderList([userGroup("g1", "hi")], ref);
    const scroller = scrollerOf(container);

    ref.current!.scrollToBottom();

    expect(siv).not.toHaveBeenCalled();
    expect(scroller.scrollTop).toBe(640);
  });
});

describe("MessageList scroll throttling", () => {
  let frames: FrameRequestCallback[] = [];
  let cancelled: number[] = [];

  function installFrameStub() {
    frames = [];
    cancelled = [];
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      frames.push(cb);
      return frames.length;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => {
      cancelled.push(id);
    });
  }

  function nextFrame() {
    const pending = frames;
    frames = [];
    for (const cb of pending) cb(0);
  }

  /** Scroller sized so 1000px of content shows through a 200px viewport. */
  function sizedScroller(container: HTMLElement): HTMLElement {
    const el = container.firstElementChild as HTMLElement;
    Object.defineProperty(el, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(el, "clientHeight", { value: 200, configurable: true });
    return el;
  }

  function renderStream(text: string) {
    return render(
      <MessageList
        renderGroups={[streamGroup(text)]}
        isStreaming
        agentName="Friday"
      />,
    );
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("measures the frame's first scroll event and drops the rest until the next frame", () => {
    installFrameStub();
    const { container, rerender } = renderStream("chunk");
    const scroller = sizedScroller(container);

    // Scrolled to the top — the user is reading, not pinned.
    scroller.scrollTop = 0;
    fireEvent.scroll(scroller);

    // Same frame: a second event back at the bottom must NOT re-measure,
    // otherwise a burst's last position wins instead of its first.
    scroller.scrollTop = 900;
    fireEvent.scroll(scroller);

    rerender(
      <MessageList
        renderGroups={[streamGroup("chunk chunk")]}
        isStreaming
        agentName="Friday"
      />,
    );

    expect(scroller.scrollTop).toBe(900);
  });

  it("re-measures once the frame turns over, restoring autoscroll", () => {
    installFrameStub();
    const { container, rerender } = renderStream("chunk");
    const scroller = sizedScroller(container);

    scroller.scrollTop = 0;
    fireEvent.scroll(scroller);

    nextFrame();
    scroller.scrollTop = 900;
    fireEvent.scroll(scroller);

    rerender(
      <MessageList
        renderGroups={[streamGroup("chunk chunk")]}
        isStreaming
        agentName="Friday"
      />,
    );

    expect(scroller.scrollTop).toBe(1000);
  });

  it("keeps a scrolled-up reader parked while chunks stream in", () => {
    installFrameStub();
    const { container, rerender } = renderStream("chunk");
    const scroller = sizedScroller(container);

    scroller.scrollTop = 0;
    fireEvent.scroll(scroller);
    nextFrame();

    for (const text of ["chunk chunk", "chunk chunk chunk"]) {
      rerender(
        <MessageList
          renderGroups={[streamGroup(text)]}
          isStreaming
          agentName="Friday"
        />,
      );
    }

    expect(scroller.scrollTop).toBe(0);
  });

  it("cancels a pending frame on unmount", () => {
    installFrameStub();
    const { container, unmount } = renderStream("chunk");
    const scroller = sizedScroller(container);

    scroller.scrollTop = 0;
    fireEvent.scroll(scroller);
    unmount();

    expect(cancelled).toEqual([1]);
  });
});
