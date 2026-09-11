import { afterEach, beforeEach, describe, expect, it } from "vitest";
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

  it("restores the base title on unmount", () => {
    const { unmount } = render(<UnreadTitleBadge unread={5} />);
    expect(document.title).toBe("(5) Lightbulb");
    unmount();
    expect(document.title).toBe("Lightbulb");
  });
});
