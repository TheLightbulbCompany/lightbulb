// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { ChatInput } from "../ChatInput";
import {
  requestFocusComposer,
  _resetFocusComposerRequestForTests,
} from "@/lib/focusComposerRequest";

// ChatInput consumes useSlashCommands (gateway-backed). This test renders
// without a GatewayProvider, so stub the hook to an empty command set.
vi.mock("@/hooks/useSlashCommands", () => ({
  useSlashCommands: (): { commands: never[]; isLoading: boolean } => ({
    commands: [],
    isLoading: false,
  }),
}));

afterEach(_resetFocusComposerRequestForTests);

describe("ChatInput focus event", () => {
  it("focuses the textarea on getstarted:focus-composer", () => {
    // Typed to ChatInput's real `onSend` signature so tsc (which compiles
    // test files in CI) is satisfied.
    const onSend = (_message: string, _files?: File[]): void => {};
    render(<ChatInput onSend={onSend} disabled={false} />);
    window.dispatchEvent(new CustomEvent("getstarted:focus-composer"));
    expect(document.activeElement?.tagName.toLowerCase()).toBe("textarea");
  });

  it("seeds an empty composer from a pending draft when it mounts for that agent", () => {
    const onSend = (_m: string, _f?: File[]): void => {};
    requestFocusComposer("a-1", "Look in our brain for: pricing");
    render(<ChatInput onSend={onSend} agentId="a-1" disabled={false} />);
    expect(screen.getByRole("textbox")).toHaveValue("Look in our brain for: pricing");
  });
});
