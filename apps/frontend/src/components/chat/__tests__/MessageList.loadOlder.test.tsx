import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

function userGroup(key: string, text: string): RenderGroup {
  return {
    side: "user",
    key,
    items: [{ kind: "message", key: `${key}:m`, message: { role: "user", text } }],
  };
}

describe("MessageList load-earlier control", () => {
  it("renders the control only when older pages remain", () => {
    const { rerender } = render(
      <MessageList renderGroups={[]} agentName="Friday" hasOlder={false} />,
    );
    expect(screen.queryByRole("button", { name: /earlier messages/i })).toBeNull();

    rerender(<MessageList renderGroups={[]} agentName="Friday" hasOlder />);
    expect(screen.getByRole("button", { name: /earlier messages/i })).toBeTruthy();
  });

  it("invokes onLoadOlder when clicked", () => {
    const onLoadOlder = vi.fn();
    render(
      <MessageList renderGroups={[]} agentName="Friday" hasOlder onLoadOlder={onLoadOlder} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /earlier messages/i }));
    expect(onLoadOlder).toHaveBeenCalledOnce();
  });

  it("shows a pending label while a page is in flight", () => {
    render(
      <MessageList renderGroups={[]} agentName="Friday" hasOlder isLoadingOlder />,
    );
    expect(screen.getByText(/loading earlier messages/i)).toBeTruthy();
  });

  it("preserves distance from bottom across a load, and never uses scrollIntoView", async () => {
    // Load-bearing regression test: MessageList is bottom-anchored, so
    // prepending older messages at the top must not jump the viewport.
    // handleLoadOlder captures `scrollHeight - scrollTop` before the fetch
    // and restores `scrollTop = scrollHeight - captured` inside a rAF after
    // the promise resolves. This pins that math against the actual handler.
    const siv = vi.spyOn(Element.prototype, "scrollIntoView");

    // Capture the rAF callback instead of letting it fire immediately, so
    // the test can grow scrollHeight (simulating the prepend actually
    // landing in the DOM) BEFORE the restore runs — the exact ordering the
    // real browser produces (fetch resolves, DOM paints, THEN rAF fires).
    let rafCallback: FrameRequestCallback | null = null;
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      rafCallback = cb;
      return 0;
    });

    let resolveLoad!: () => void;
    const onLoadOlder = vi.fn(
      () => new Promise<void>((resolve) => { resolveLoad = resolve; }),
    );

    const { container } = render(
      <MessageList
        renderGroups={[userGroup("g1", "hi")]}
        agentName="Friday"
        hasOlder
        onLoadOlder={onLoadOlder}
      />,
    );

    const scroller = container.firstElementChild as HTMLElement;
    Object.defineProperty(scroller, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(scroller, "clientHeight", { value: 500, configurable: true });
    scroller.scrollTop = 300;
    // distance-from-bottom captured by handleLoadOlder = 1000 - 300 = 700

    fireEvent.click(screen.getByRole("button", { name: /earlier messages/i }));
    expect(onLoadOlder).toHaveBeenCalledOnce();

    // Resolve the in-flight page fetch and flush the microtask queue so the
    // handler's `.then(...)` runs and schedules the rAF.
    resolveLoad();
    await Promise.resolve();
    await Promise.resolve();

    expect(rafCallback).not.toBeNull();

    // Simulate the prepend growing the container BEFORE the restore runs.
    Object.defineProperty(scroller, "scrollHeight", { value: 1400, configurable: true });

    rafCallback!(0);

    // Restored scrollTop must preserve the 700px distance from the bottom:
    // newScrollHeight (1400) - capturedDistance (700) = 700. NOT left at
    // the stale 300, and NOT jumped to 0 (top) or 1400 (bottom).
    expect(scroller.scrollTop).toBe(700);
    expect(siv).not.toHaveBeenCalled();
  });
});
