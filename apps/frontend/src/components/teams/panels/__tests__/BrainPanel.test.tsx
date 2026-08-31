import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { FileEntry, FileInfo } from "@/hooks/useWorkspaceFiles";

// Hook state the tests mutate per-case. useBrainFile is a spy so tests can
// assert which path the panel actually requested after a tree click.
let treeState: {
  files: FileEntry[];
  error: Error | undefined;
  isLoading: boolean;
  refresh: ReturnType<typeof vi.fn>;
};
let fileState: { file: FileInfo | null; error: Error | null; isLoading: boolean };
const useBrainFileSpy = vi.fn((path: string | null) => {
  void path; // recorded by the spy; tests assert on the call args
  return fileState;
});
const saveBrainFile = vi.fn().mockResolvedValue({ path: "x", size: 1, modified_at: 1 });
const deleteBrainFile = vi.fn().mockResolvedValue(null);

vi.mock("@/hooks/useBrainFiles", () => ({
  useBrainTree: () => treeState,
  useBrainFile: (path: string | null) => useBrainFileSpy(path),
  useBrainMutations: () => ({ saveBrainFile, deleteBrainFile }),
}));

const openChatPanel = vi.fn();
vi.mock("@/components/workspace/workspaceController", () => ({
  openChatPanel: (agentId: string) => openChatPanel(agentId),
}));

// Keep mermaid out of the unit test — FileContentViewer imports it for
// .lobster files, which Brain never renders.
vi.mock("@/components/chat/LobsterGraphView", () => ({
  LobsterGraphView: () => null,
}));

import { BrainPanel } from "../BrainPanel";
import { DEFAULT_AGENT_SENTINEL } from "@/components/workspace/panelRegistry";

const FACTS: FileEntry = {
  name: "facts.md",
  path: "main/facts.md",
  type: "file",
  size: 42,
  modified_at: 1_756_000_000,
};
const MAIN_DIR: FileEntry = {
  name: "main",
  path: "main",
  type: "dir",
  size: null,
  modified_at: 1_756_000_000,
};
const FACTS_INFO: FileInfo = {
  name: "facts.md",
  path: "main/facts.md",
  size: 42,
  modified_at: 1_756_000_000,
  content: "The sky is green.",
  binary: false,
  mime_type: "text/markdown",
};

beforeEach(() => {
  treeState = { files: [], error: undefined, isLoading: false, refresh: vi.fn() };
  fileState = { file: null, error: null, isLoading: false };
  useBrainFileSpy.mockClear();
  saveBrainFile.mockClear();
  deleteBrainFile.mockClear();
  openChatPanel.mockClear();
});

describe("BrainPanel", () => {
  it("renders the Knowledge/Brain masthead and the vault tree", () => {
    treeState.files = [MAIN_DIR, FACTS];
    render(<BrainPanel />);
    expect(screen.getByRole("heading", { name: "Brain" })).toBeInTheDocument();
    expect(screen.getByText("Knowledge")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
  });

  it("requests a page on tree selection and shows its size · modified time", () => {
    treeState.files = [MAIN_DIR, FACTS];
    fileState.file = FACTS_INFO;
    render(<BrainPanel />);
    fireEvent.click(screen.getByText("main"));
    // The tree entry is a button; the (mocked-open) viewer header repeats the
    // name in a plain span — scope to the role to hit the tree.
    fireEvent.click(screen.getByRole("button", { name: "facts.md" }));
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("main/facts.md");
    // The audit line: owners trust a brain they can see the freshness of.
    expect(screen.getByTestId("panel-masthead-meta").textContent).toMatch(/42 B · /);
  });

  it("saves edits through saveBrainFile with the selected path", async () => {
    treeState.files = [FACTS];
    fileState.file = FACTS_INFO;
    render(<BrainPanel initialPath="main/facts.md" />);
    const textarea = screen.getByDisplayValue("The sky is green.");
    fireEvent.change(textarea, { target: { value: "The sky is blue." } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() =>
      expect(saveBrainFile).toHaveBeenCalledWith("main/facts.md", "The sky is blue."),
    );
    expect(treeState.refresh).toHaveBeenCalled();
  });

  it("deletes the selected page after confirmation", async () => {
    treeState.files = [FACTS];
    fileState.file = FACTS_INFO;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<BrainPanel initialPath="main/facts.md" />);
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    await waitFor(() => expect(deleteBrainFile).toHaveBeenCalledWith("main/facts.md"));
    expect(treeState.refresh).toHaveBeenCalled();
  });

  it("shows the empty state with an Ask-Friday chip when the vault is empty", () => {
    render(<BrainPanel />);
    expect(
      screen.getByText("Your company's brain fills as your agents learn."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /ask friday/i }));
    expect(openChatPanel).toHaveBeenCalledWith(DEFAULT_AGENT_SENTINEL);
  });

  it("does not show the empty state while the tree is still loading", () => {
    treeState.isLoading = true;
    render(<BrainPanel />);
    expect(
      screen.queryByText("Your company's brain fills as your agents learn."),
    ).not.toBeInTheDocument();
  });
});
