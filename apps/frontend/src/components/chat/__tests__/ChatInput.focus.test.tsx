// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";

import { ChatInput } from "../ChatInput";

// ChatInput consumes useSlashCommands (gateway-backed). This test renders
// without a GatewayProvider, so stub the hook to an empty command set.
vi.mock("@/hooks/useSlashCommands", () => ({
  useSlashCommands: (): { commands: never[]; isLoading: boolean } => ({
    commands: [],
    isLoading: false,
  }),
}));

describe("ChatInput focus event", () => {
  it("focuses the textarea on getstarted:focus-composer", () => {
    // Typed to ChatInput's real `onSend` signature so tsc (which compiles
    // test files in CI) is satisfied.
    const onSend = (_message: string, _files?: File[]): void => {};
    render(<ChatInput onSend={onSend} disabled={false} />);
    window.dispatchEvent(new CustomEvent("getstarted:focus-composer"));
    expect(document.activeElement?.tagName.toLowerCase()).toBe("textarea");
  });
});
