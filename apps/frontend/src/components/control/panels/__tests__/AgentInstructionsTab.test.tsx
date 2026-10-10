import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { AgentInstructionsTab } from "../AgentInstructionsTab";

// Capture the (agentId, path) the tab reads, and feed back a fake FileInfo so
// FileContentViewer mounts an editable buffer for that path.
let lastReadAgentId: string | null = null;
let lastReadPath: string | null = null;
vi.mock("@/hooks/useWorkspaceFiles", () => ({
  useConfigFile: (agentId: string | null, path: string | null) => {
    lastReadAgentId = agentId;
    lastReadPath = path;
    return {
      file: path
        ? {
            name: path.split("/").pop() ?? path,
            path,
            size: 0,
            modified_at: 0,
            content: `content-of-${path}`,
            binary: false,
            mime_type: "text/markdown",
          }
        : null,
      isLoading: false,
      error: undefined,
    };
  },
}));

// saveWorkspaceFile is the single write path. Spy on it to assert the
// (agentId, path, content, "config") call shape.
const saveWorkspaceFile = vi.fn().mockResolvedValue({ status: "ok", path: "x" });
vi.mock("@/lib/api", () => ({
  useApi: () => ({ saveWorkspaceFile }),
}));

// Stub FileContentViewer to a minimal editor surface: it surfaces the loaded
// file path and a Save button that invokes onSave with deterministic content,
// so the test asserts the wiring without dragging the real textarea/markdown
// stack into the sandbox.
vi.mock("@/components/chat/FileContentViewer", () => ({
  FileContentViewer: ({
    file,
    onSave,
  }: {
    file: { path: string } | null;
    onSave?: (content: string) => Promise<void>;
  }) => (
    <div data-testid="file-content-viewer">
      <span data-testid="viewer-path">{file?.path ?? "none"}</span>
      <button
        type="button"
        onClick={() => onSave?.(`edited:${file?.path}`)}
      >
        Save
      </button>
    </div>
  ),
}));

describe("AgentInstructionsTab", () => {
  beforeEach(() => {
    saveWorkspaceFile.mockClear();
    lastReadAgentId = null;
    lastReadPath = null;
  });

  it("renders the identity-file options, never IDENTITY.md (Hermes refuses its save)", () => {
    render(<AgentInstructionsTab agentId="agent_42" />);
    expect(screen.queryByRole("button", { name: "IDENTITY.md" })).toBeNull();
    expect(screen.getByRole("button", { name: "SOUL.md" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "USER.md" })).toBeInTheDocument();
  });

  it("defaults to reading SOUL.md", () => {
    render(<AgentInstructionsTab agentId="agent_42" />);
    expect(lastReadAgentId).toBe("agent_42");
    expect(lastReadPath).toBe("SOUL.md");
    expect(screen.getByTestId("viewer-path")).toHaveTextContent("SOUL.md");
  });

  it("reads USER.md when its option is selected", () => {
    render(<AgentInstructionsTab agentId="agent_42" />);
    fireEvent.click(screen.getByRole("button", { name: "USER.md" }));
    expect(lastReadPath).toBe("USER.md");
    expect(screen.getByTestId("viewer-path")).toHaveTextContent("USER.md");
  });

  it("reads MEMORY.md and explains it when selected", () => {
    render(<AgentInstructionsTab agentId="agent_42" />);
    expect(screen.queryByText(/notes this agent saved/i)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "MEMORY.md" }));
    expect(lastReadPath).toBe("MEMORY.md");
    expect(
      screen.getByText("Notes this agent saved for itself. Every room sees them."),
    ).toBeInTheDocument();
  });

  it("saves via saveWorkspaceFile(agentId, path, content, 'config')", async () => {
    render(<AgentInstructionsTab agentId="agent_42" />);
    fireEvent.click(screen.getByRole("button", { name: "SOUL.md" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(saveWorkspaceFile).toHaveBeenCalledWith(
      "agent_42",
      "SOUL.md",
      "edited:SOUL.md",
      "config",
    );
  });

  it("shows the next-session note", () => {
    render(<AgentInstructionsTab agentId="agent_42" />);
    expect(
      screen.getByText(/changes apply on the agent's next session/i),
    ).toBeInTheDocument();
  });

  it("marks the active identity-file option with aria-pressed", () => {
    render(<AgentInstructionsTab agentId="agent_42" />);
    expect(screen.getByRole("button", { name: "SOUL.md" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "USER.md" }));
    expect(screen.getByRole("button", { name: "USER.md" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "SOUL.md" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });
});
