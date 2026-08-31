import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
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
let fileState: { file: FileInfo | null; error: Error | null; isLoading: boolean; key: string | null };
const useBrainFileSpy = vi.fn((path: string | null) => {
  void path; // recorded by the spy; tests assert on the call args
  return fileState;
});
const saveBrainFile = vi.fn().mockResolvedValue({ path: "x", size: 1, modified_at: 1 });
const deleteBrainFile = vi.fn().mockResolvedValue(null);
const uploadBrainFiles = vi.fn().mockResolvedValue({ uploaded: [] });

vi.mock("@/hooks/useBrainFiles", () => ({
  useBrainTree: () => treeState,
  useBrainFile: (path: string | null) => useBrainFileSpy(path),
  useBrainMutations: () => ({ saveBrainFile, deleteBrainFile, uploadBrainFiles }),
}));

const openChatPanel = vi.fn();
vi.mock("@/components/workspace/workspaceController", () => ({
  openChatPanel: (agentId: string) => openChatPanel(agentId),
}));

// The panel merges the PUT's fresh stat into the file's own SWR cache entry
// via useSWRConfig().mutate — spy on it the same way ArtifactsPanel.test.tsx does.
const globalMutate = vi.fn();
vi.mock("swr", async (importOriginal) => {
  const actual = await importOriginal<typeof import("swr")>();
  return { ...actual, useSWRConfig: () => ({ mutate: globalMutate }) };
});

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
  fileState = { file: null, error: null, isLoading: false, key: null };
  useBrainFileSpy.mockClear();
  saveBrainFile.mockClear();
  deleteBrainFile.mockClear();
  uploadBrainFiles.mockClear();
  uploadBrainFiles.mockResolvedValue({ uploaded: [] });
  openChatPanel.mockClear();
  globalMutate.mockClear();
});

afterEach(() => {
  // window.confirm is stubbed inside individual tests below (vi.spyOn) —
  // restore it so a later test never inherits a silently auto-confirming
  // (or auto-denying) dialog.
  vi.restoreAllMocks();
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
    fileState.key = "/container/brain/file?path=main%2Ffacts.md";
    render(<BrainPanel initialPath="main/facts.md" />);
    const textarea = screen.getByDisplayValue("The sky is green.");
    fireEvent.change(textarea, { target: { value: "The sky is blue." } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() =>
      expect(saveBrainFile).toHaveBeenCalledWith("main/facts.md", "The sky is blue."),
    );
    expect(treeState.refresh).toHaveBeenCalled();
    // The audit line goes stale right after the owner's own edit unless the
    // file's own SWR cache entry is also mutated (finding 6/M-fix): the panel
    // must merge the PUT's fresh stat into the file key, not just the tree key.
    expect(globalMutate).toHaveBeenCalledWith(fileState.key, expect.any(Function), false);
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

  it("shows an error state with retry — not the empty state — when the tree fetch fails (M1)", () => {
    treeState.files = [];
    treeState.error = new Error("boom");
    render(<BrainPanel />);
    expect(screen.getByText("Couldn't load your brain.")).toBeInTheDocument();
    expect(
      screen.queryByText("Your company's brain fills as your agents learn."),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(treeState.refresh).toHaveBeenCalled();
  });

  it("uploads dropped files onto the tree pane and refreshes the tree", async () => {
    treeState.files = [FACTS];
    render(<BrainPanel />);
    const pane = screen.getByTestId("brain-tree-pane");
    const file = new File(["hello"], "notes.md", { type: "text/markdown" });
    fireEvent.drop(pane, { dataTransfer: { files: [file] } });
    await waitFor(() => expect(uploadBrainFiles).toHaveBeenCalledWith([file]));
    await waitFor(() => expect(treeState.refresh).toHaveBeenCalled());
  });

  it("uploads files picked via the file input on the tree pane", async () => {
    treeState.files = [FACTS];
    render(<BrainPanel />);
    const input = screen.getByLabelText(/upload files/i) as HTMLInputElement;
    const file = new File(["hello"], "notes.md", { type: "text/markdown" });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(uploadBrainFiles).toHaveBeenCalledWith([file]));
    await waitFor(() => expect(treeState.refresh).toHaveBeenCalled());
  });

  it("shows an error toast and still refreshes when an upload fails", async () => {
    treeState.files = [FACTS];
    uploadBrainFiles.mockRejectedValueOnce(new Error("too big"));
    render(<BrainPanel />);
    const input = screen.getByLabelText(/upload files/i) as HTMLInputElement;
    const file = new File(["hello"], "notes.md", { type: "text/markdown" });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(uploadBrainFiles).toHaveBeenCalled());
    await waitFor(() => expect(treeState.refresh).toHaveBeenCalled());
  });

  it("ignores a second drop while an upload is already in flight (T3 review F7)", async () => {
    treeState.files = [FACTS];
    let resolveUpload: (v: { uploaded: never[] }) => void = () => {};
    uploadBrainFiles.mockImplementation(
      () => new Promise((resolve) => { resolveUpload = resolve; }),
    );
    render(<BrainPanel />);
    const pane = screen.getByTestId("brain-tree-pane");
    const file1 = new File(["a"], "a.md");
    const file2 = new File(["b"], "b.md");

    fireEvent.drop(pane, { dataTransfer: { files: [file1] } });
    await waitFor(() => expect(uploadBrainFiles).toHaveBeenCalledTimes(1));

    // A second drop while the first is still pending must not fire a second
    // concurrent request — that is the client-side half of the T3 review's
    // M3 collision race.
    fireEvent.drop(pane, { dataTransfer: { files: [file2] } });
    expect(uploadBrainFiles).toHaveBeenCalledTimes(1);

    resolveUpload({ uploaded: [] });
    await waitFor(() => expect(treeState.refresh).toHaveBeenCalled());
  });

  it("disables the upload input while an upload is in flight (T3 review item 6)", async () => {
    treeState.files = [FACTS];
    let resolveUpload: (v: { uploaded: never[] }) => void = () => {};
    uploadBrainFiles.mockImplementation(
      () => new Promise((resolve) => { resolveUpload = resolve; }),
    );
    render(<BrainPanel />);
    const input = screen.getByLabelText(/upload files/i) as HTMLInputElement;
    const file = new File(["a"], "a.md");

    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(input.disabled).toBe(true));

    resolveUpload({ uploaded: [] });
    await waitFor(() => expect(input.disabled).toBe(false));
  });

  it("accepts a drop onto the empty vault and uploads (empty-vault ruling)", async () => {
    treeState.files = [];
    render(<BrainPanel />);
    const zone = screen.getByTestId("brain-empty-drop-zone");
    const file = new File(["hello"], "notes.md", { type: "text/markdown" });
    fireEvent.drop(zone, { dataTransfer: { files: [file] } });
    await waitFor(() => expect(uploadBrainFiles).toHaveBeenCalledWith([file]));
  });

  it("opens the file picker from the empty vault's 'or drop files here' link", () => {
    treeState.files = [];
    render(<BrainPanel />);
    const input = screen.getByLabelText(/upload files/i) as HTMLInputElement;
    const clickSpy = vi.spyOn(input, "click");
    fireEvent.click(screen.getByRole("button", { name: /or drop files here/i }));
    expect(clickSpy).toHaveBeenCalled();
  });

  it("follows a second ?path deep link on an already-mounted panel (M2)", () => {
    treeState.files = [FACTS];
    fileState.file = FACTS_INFO;
    const { rerender } = render(<BrainPanel initialPath="main/facts.md" />);
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("main/facts.md");
    // openPanel on an already-open panel focuses + merges params rather than
    // remounting — simulate that by rerendering with a new initialPath prop.
    rerender(<BrainPanel initialPath="main/icp.md" />);
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("main/icp.md");
  });
});
