// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const renderMock = vi.fn().mockResolvedValue({ svg: '<svg data-testid="diagram"></svg>' });
vi.mock("mermaid", () => ({
  default: { initialize: vi.fn(), render: renderMock },
}));

const lobsterToMermaidMock = vi.fn();
vi.mock("@/lib/lobster", () => ({
  lobsterToMermaid: (c: string) => lobsterToMermaidMock(c),
}));

import { LobsterGraphView } from "@/components/chat/LobsterGraphView";

beforeEach(() => {
  lobsterToMermaidMock.mockReset();
  renderMock.mockClear();
});

describe("LobsterGraphView", () => {
  it("shows a loading state then renders the mermaid svg on the ok path", async () => {
    lobsterToMermaidMock.mockReturnValue({ ok: true, mermaid: "flowchart TD\n n_a[\"a\"]" });
    render(<LobsterGraphView content="steps:\n  - id: a\n    command: exec" />);
    // Synchronously before the mermaid render promise resolves, svg is null.
    expect(screen.getByText(/rendering graph/i)).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("diagram")).toBeTruthy());
    expect(renderMock).toHaveBeenCalled();
  });

  it("shows a fallback and a 'View as code' action on parse failure", () => {
    lobsterToMermaidMock.mockReturnValue({ ok: false, reason: "parse", message: "bad" });
    const onViewCode = vi.fn();
    render(<LobsterGraphView content=":::" onViewCode={onViewCode} />);
    expect(screen.getByText(/couldn't render/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /view as code/i }));
    expect(onViewCode).toHaveBeenCalled();
  });

  it("shows the empty-state message when there are no steps", () => {
    lobsterToMermaidMock.mockReturnValue({ ok: false, reason: "empty", message: "No workflow steps found to graph." });
    render(<LobsterGraphView content="name: x" />);
    expect(screen.getByText(/no workflow steps/i)).toBeTruthy();
  });
});
