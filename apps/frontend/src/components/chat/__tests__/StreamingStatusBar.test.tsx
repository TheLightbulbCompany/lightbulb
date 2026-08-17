/**
 * StreamingStatusBar visibility.
 *
 * The bar historically rendered only while `isStreaming` (token stream
 * flowing), so it went dark during silent windows of a live run — long tool
 * execution, the beat before the next tokens, the tool-approval pause. It was
 * then made to stay lit whenever the run is server-truth `active`.
 *
 * It also used to DERIVE its own label, printing `Running ${block.name}` — the
 * raw internal tool name, so users read `Running mcp__openclaw__web_search…`.
 * That derivation is gone: ToolActivityTrace narrates each step in plain
 * language, in place, where the work appears. The bar keeps only the job the
 * trace cannot do — covering the window where the server says a run is active
 * but this tab is seeing no stream events at all.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StreamingStatusBar } from "../StreamingStatusBar";

describe("StreamingStatusBar", () => {
  it("renders a working indicator when the run is active but nothing is streaming", () => {
    render(<StreamingStatusBar isStreaming={false} active={true} />);
    expect(screen.getByText(/working/i)).toBeInTheDocument();
  });

  it("renders nothing when not active and nothing is streaming", () => {
    const { container } = render(<StreamingStatusBar isStreaming={false} active={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when active is omitted and nothing is streaming (default off)", () => {
    const { container } = render(<StreamingStatusBar isStreaming={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("stays out of the way while the transcript itself is streaming", () => {
    // The trace and the token stream are the status while they are arriving.
    // A second, vaguer indicator underneath them is noise — and the label it
    // used to show there was the raw tool name.
    const { container } = render(<StreamingStatusBar isStreaming={true} active={true} />);
    expect(container).toBeEmptyDOMElement();
  });
});
