/**
 * Scroll containment: MessageList must scroll ONLY its own overflow
 * container. `scrollIntoView` walks every scrollable ancestor — including
 * the overflow-clipped workspace shell, which Chrome will still scroll
 * programmatically — shifting the entire app upward and leaving a blank
 * void the user cannot scroll back from. Neither the auto-snap effect nor
 * the imperative send-path `scrollToBottom` may ever call it.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render } from "@testing-library/react";
import { MessageList, type MessageListHandle } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

function userGroup(key: string, text: string): RenderGroup {
  return {
    side: "user",
    key,
    items: [{ kind: "message", key: `${key}:m`, message: { role: "user", text } }],
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
      isTyping={false}
      agentName="Friday"
      onOpenFile={() => {}}
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
        isTyping={false}
        agentName="Friday"
        onOpenFile={() => {}}
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
