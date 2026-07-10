/**
 * StreamingStatusBar visibility across silent windows.
 *
 * The bar historically rendered only while `isStreaming` (token stream
 * flowing), so it went dark during silent windows of a live run — long tool
 * execution, the beat before the next tokens, the tool-approval pause. It
 * must now also stay lit whenever the run is server-truth `active`, showing
 * a plain "Working…" label when active but not streaming, and still return
 * null when neither streaming nor active.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StreamingStatusBar } from "../StreamingStatusBar";

describe("StreamingStatusBar", () => {
  it("renders a working indicator when the run is active but no tool is streaming", () => {
    render(<StreamingStatusBar isStreaming={false} renderItems={[]} active={true} />);
    expect(screen.getByText(/working/i)).toBeInTheDocument();
  });

  it("renders nothing when not active and nothing is streaming", () => {
    const { container } = render(
      <StreamingStatusBar isStreaming={false} renderItems={[]} active={false} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when active is omitted and nothing is streaming (default off)", () => {
    const { container } = render(<StreamingStatusBar isStreaming={false} renderItems={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("still shows the derived tool label while isStreaming, ignoring active", () => {
    render(
      <StreamingStatusBar
        isStreaming={true}
        active={true}
        renderItems={[
          {
            kind: "message",
            key: "m1",
            message: {
              role: "assistant",
              content: [{ type: "tool_use", id: "t1", name: "bash", args: {} }],
            },
          },
        ]}
      />,
    );
    expect(screen.getByText(/running bash/i)).toBeInTheDocument();
  });
});
