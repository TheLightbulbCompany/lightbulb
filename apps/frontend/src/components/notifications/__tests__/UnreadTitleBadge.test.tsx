import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { UnreadTitleBadge } from "../UnreadTitleBadge";

describe("UnreadTitleBadge", () => {
  beforeEach(() => {
    document.title = "Lightbulb";
  });
  afterEach(() => {
    document.title = "Lightbulb";
  });

  it("prefixes the title with the unread count and drops the prefix at zero", () => {
    const { rerender } = render(<UnreadTitleBadge unread={3} />);
    expect(document.title).toBe("(3) Lightbulb");
    rerender(<UnreadTitleBadge unread={0} />);
    expect(document.title).toBe("Lightbulb");
  });

  it("mirrors the count onto the desktop dock icon and clears it at zero", () => {
    const setBadgeCount = vi.fn().mockResolvedValue(undefined);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__TAURI__ = { window: { getCurrentWindow: () => ({ setBadgeCount }) } };
    try {
      const { rerender } = render(<UnreadTitleBadge unread={3} />);
      rerender(<UnreadTitleBadge unread={0} />);
      expect(setBadgeCount.mock.calls).toEqual([[3], [undefined]]);
    } finally {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      delete (window as any).__TAURI__;
    }
  });

  it("names the place and keeps the count in front of it", () => {
    const { rerender } = render(<UnreadTitleBadge unread={0} title="Home · Lightbulb" />);
    expect(document.title).toBe("Home · Lightbulb");
    rerender(<UnreadTitleBadge unread={2} title="Launch plan · Lightbulb" />);
    expect(document.title).toBe("(2) Launch plan · Lightbulb");
  });

  it("restores the base title on unmount", () => {
    const { unmount } = render(<UnreadTitleBadge unread={5} />);
    expect(document.title).toBe("(5) Lightbulb");
    unmount();
    expect(document.title).toBe("Lightbulb");
  });
});
