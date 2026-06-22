// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("@/components/chat/LobsterGraphView", () => ({
  LobsterGraphView: ({ content, onViewCode }: { content: string; onViewCode?: () => void }) => (
    <div>
      <span>GRAPH:{content.length}</span>
      <button onClick={onViewCode}>to-code</button>
    </div>
  ),
}));

import { FileContentViewer } from "@/components/chat/FileContentViewer";
import type { FileInfo } from "@/hooks/useWorkspaceFiles";

function fileOf(name: string, content: string): FileInfo {
  return {
    name,
    path: "/" + name,
    size: content.length,
    modified_at: 0,
    content,
    binary: false,
    mime_type: "text/plain",
  };
}

describe("FileContentViewer .lobster handling", () => {
  it("defaults a .lobster file to the Graph tab", () => {
    render(<FileContentViewer file={fileOf("wf.lobster", "steps:\n  - id: a")} isLoading={false} error={null} />);
    expect(screen.getByText(/^GRAPH:/)).toBeTruthy();
  });

  it("switches to the editable Code view when the Code tab is clicked", () => {
    render(<FileContentViewer file={fileOf("wf.lobster", "steps:\n  - id: a")} isLoading={false} error={null} />);
    fireEvent.click(screen.getByRole("tab", { name: /code/i }));
    expect(screen.getByRole("textbox")).toBeTruthy();
  });

  it("renders a non-lobster file directly as an editable textarea with no tabs", () => {
    render(<FileContentViewer file={fileOf("notes.txt", "hello")} isLoading={false} error={null} />);
    expect(screen.getByRole("textbox")).toBeTruthy();
    expect(screen.queryByRole("tab")).toBeNull();
  });
});
