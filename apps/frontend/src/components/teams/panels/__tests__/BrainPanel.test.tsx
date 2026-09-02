import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { FileEntry, FileInfo } from "@/hooks/useWorkspaceFiles";

// Hook state the tests mutate per-case. useBrainFile is a spy so tests can
// assert which page the panel actually requested after a navigation.
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

// Every ASK exit is a draft handed to the default agent's composer (T1).
const openChatPanelWithDraft = vi.fn((draft: string) => {
  void draft; // recorded by the spy; tests assert on the call args
  return true;
});
vi.mock("@/components/workspace/workspaceController", () => ({
  openChatPanelWithDraft: (d: string) => openChatPanelWithDraft(d),
}));

// The ASK fallback needs the default agent by name when there is no dock.
let agentsState: { defaultId: string | undefined };
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => agentsState }));

// Only notifyError is spied — notifyApiError stays real, since the upload
// tests below exercise it end to end.
const notifyErrorSpy = vi.fn();
vi.mock("@/lib/notify", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/notify")>();
  return { ...actual, notifyError: (m: string) => notifyErrorSpy(m) };
});

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

// The Dreams tab renders DreamsPanel's body verbatim (folded in from its
// retired standalone control panel) — stub it so this suite never opens the
// gateway RPCs DreamsPanel itself makes.
vi.mock("@/components/control/panels/DreamsPanel", () => ({
  DreamsPanel: () => <div data-testid="dreams-panel-body">DreamsPanel</div>,
}));

import {
  consumeFocusComposerRequest,
  _resetFocusComposerRequestForTests,
} from "@/lib/focusComposerRequest";
import { BrainPanel } from "../BrainPanel";

const f = (path: string, type: "file" | "dir" = "file", size = 42): FileEntry => ({
  name: path.slice(path.lastIndexOf("/") + 1),
  path,
  type,
  size: type === "file" ? size : null,
  modified_at: 1_756_000_000,
});
const INDEX = f("index.md");
const ENTITIES = f("entities", "dir");
const ENTITIES_INDEX = f("entities/index.md");
const ADA = f("entities/ada-lovelace.md");
const INBOX_DIR_ENTRY = f("inbox", "dir");
const DECK = f("inbox/deck.pdf");
const REPORTS_DIR = f("reports", "dir");
const REPORT = f("reports/state-of-the-vault.md");
const SCAFFOLD = [INDEX, ENTITIES, ENTITIES_INDEX];
const REPORTS_ONLY = [INDEX, REPORTS_DIR, REPORT];
const POPULATED = [INDEX, ENTITIES, ENTITIES_INDEX, ADA, INBOX_DIR_ENTRY, DECK];
// A vault compile has actually run over: index.md carries the managed block.
const COMPILED_INDEX = [
  "# Wiki Index",
  "",
  "## Generated",
  "<!-- openclaw:wiki:index:start -->",
  "### Sources",
  "- [[sources/deck|Pitch deck]]",
  "### Entities",
  "- [[entities/ada-lovelace|Ada Lovelace]]",
  "<!-- openclaw:wiki:index:end -->",
].join("\n");
const SOURCES = f("sources", "dir");
const SOURCES_INDEX = f("sources/index.md");
const DECK_MD = f("sources/deck.md");
const COMPILED = [...POPULATED, SOURCES, SOURCES_INDEX, DECK_MD];

const md = (path: string, content: string): FileInfo => ({
  name: path.slice(path.lastIndexOf("/") + 1),
  path,
  size: content.length,
  modified_at: 1_756_000_000,
  content,
  binary: false,
  mime_type: path.endsWith(".md") ? "text/markdown" : "text/plain",
});

beforeEach(() => {
  treeState = { files: [], error: undefined, isLoading: false, refresh: vi.fn() };
  fileState = { file: null, error: null, isLoading: false, key: null };
  useBrainFileSpy.mockClear();
  saveBrainFile.mockClear();
  deleteBrainFile.mockClear();
  uploadBrainFiles.mockClear();
  uploadBrainFiles.mockResolvedValue({ uploaded: [] });
  openChatPanelWithDraft.mockClear();
  openChatPanelWithDraft.mockReturnValue(true);
  notifyErrorSpy.mockClear();
  agentsState = { defaultId: "agent_1" };
  globalMutate.mockClear();
  _resetFocusComposerRequestForTests();
  window.history.replaceState(null, "", "/chat?panel=teams-brain");
});

afterEach(() => {
  // window.confirm is stubbed inside individual tests below (vi.spyOn) —
  // restore it so a later test never inherits a silently auto-confirming
  // (or auto-denying) dialog.
  vi.restoreAllMocks();
});

describe("BrainPanel", () => {
  it("lands on the vault's index.md rendered, with the split count in the masthead", () => {
    treeState.files = POPULATED;
    fileState.file = md("index.md", "# Wiki Index\n\n[[entities/ada-lovelace|Ada Lovelace]]");
    render(<BrainPanel />);
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("index.md");
    expect(screen.getByRole("heading", { name: "Wiki Index" })).toBeInTheDocument();
    expect(screen.getByTestId("panel-masthead-meta").textContent).toMatch(/1 filed · 1 in inbox/);
    expect(screen.queryByRole("searchbox", { name: /find a page/i })).not.toBeNull();
  });

  it("scaffold vault: zero notice, no fake page count, still lands on index.md", () => {
    treeState.files = SCAFFOLD;
    fileState.file = md("index.md", "# Wiki Index\n\n- No compiled pages yet.");
    render(<BrainPanel />);
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("index.md");
    expect(screen.getByText("Nothing filed yet.")).toBeInTheDocument();
    expect(screen.getByTestId("panel-masthead-meta").textContent).toMatch(/0 filed · 0 in inbox/);
  });

  it("the compiled landing is type cards, not the index's own bullet list", () => {
    treeState.files = COMPILED;
    fileState.file = md("index.md", COMPILED_INDEX);
    render(<BrainPanel />);
    expect(screen.getByText("Sources")).toBeInTheDocument();
    expect(screen.getByText("Entities")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Wiki Index" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Pitch deck" }));
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("sources/deck.md");
  });

  it("a card's Open goes to the section index compile wrote", () => {
    treeState.files = COMPILED;
    fileState.file = md("index.md", COMPILED_INDEX);
    render(<BrainPanel />);
    // sources/index.md is in the tree, entities/index.md is too — one Open each.
    fireEvent.click(screen.getAllByRole("button", { name: "Open" })[0]);
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("sources/index.md");
  });

  it("a section with no index.md written yet has no Open", () => {
    treeState.files = COMPILED.filter((e) => e.path !== "sources/index.md");
    fileState.file = md("index.md", COMPILED_INDEX);
    render(<BrainPanel />);
    expect(screen.getAllByRole("button", { name: "Open" })).toHaveLength(1);
  });

  it("owner prose in a compiled index.md renders above the cards, not behind them", () => {
    treeState.files = COMPILED;
    fileState.file = md("index.md", `# Wiki Index\n\nWe sell candles.\n\n## Generated\n${COMPILED_INDEX.split("## Generated\n")[1]}`);
    render(<BrainPanel />);
    expect(screen.getByText("We sell candles.")).toBeInTheDocument();
    expect(screen.getByText("Sources")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Wiki Index" })).toBeNull();
  });

  it("an index with no managed block falls back to the plain rendered page", () => {
    treeState.files = POPULATED;
    fileState.file = md("index.md", "# Wiki Index\n\n[[entities/ada-lovelace|Ada Lovelace]]");
    render(<BrainPanel />);
    expect(screen.getByRole("heading", { name: "Wiki Index" })).toBeInTheDocument();
    expect(screen.queryByText("Recently updated")).toBeNull();
  });

  it("Recently updated lists filed pages under the cards and opens one", () => {
    treeState.files = COMPILED;
    fileState.file = md("index.md", COMPILED_INDEX);
    render(<BrainPanel />);
    expect(screen.getByText("Recently updated")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /ada-lovelace/ }));
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("entities/ada-lovelace.md");
  });

  it("Edit on the compiled landing still opens the raw index.md", () => {
    treeState.files = COMPILED;
    fileState.file = md("index.md", COMPILED_INDEX);
    render(<BrainPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(COMPILED_INDEX);
  });

  it("inbox files get the partial notice whose exit drafts the filing prompt", () => {
    treeState.files = POPULATED;
    render(<BrainPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Ask Friday to file it" }));
    expect(openChatPanelWithDraft).toHaveBeenCalledWith(
      expect.stringMatching(/1 file in the brain inbox/),
    );
  });

  it("clicking the inbox count lists inbox files; picking one opens it", () => {
    treeState.files = POPULATED;
    render(<BrainPanel />);
    fireEvent.click(screen.getByRole("button", { name: /1 in inbox/ }));
    fireEvent.click(screen.getByRole("button", { name: /inbox\/deck\.pdf/ }));
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("inbox/deck.pdf");
  });

  it("a wikilink click navigates in-panel", () => {
    treeState.files = POPULATED;
    fileState.file = md("index.md", "[[entities/ada-lovelace|Ada Lovelace]]");
    render(<BrainPanel />);
    fireEvent.click(screen.getByRole("link", { name: "Ada Lovelace" }));
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("entities/ada-lovelace.md");
  });

  it("breadcrumb: Index and the section index are reachable from a deep page", () => {
    treeState.files = POPULATED;
    fileState.file = md("entities/ada-lovelace.md", "# Ada");
    render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
    fireEvent.click(screen.getByRole("button", { name: "entities" }));
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("entities/index.md");
    fireEvent.click(screen.getByRole("button", { name: "Index" }));
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("index.md");
  });

  it("Edit opens the raw editor; Save writes through saveBrainFile and returns to reading", async () => {
    treeState.files = POPULATED;
    fileState.file = md("entities/ada-lovelace.md", "---\ntitle: Ada\n---\n# Ada");
    fileState.key = "/container/brain/file?path=entities%2Fada-lovelace.md";
    render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    // The editor is the raw file INCLUDING frontmatter — the audit view.
    // (getByDisplayValue normalizes whitespace, so a multi-line value is
    // matched off the element, not through a TextMatch.)
    const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(ta.value).toBe("---\ntitle: Ada\n---\n# Ada");
    fireEvent.change(ta, { target: { value: "---\ntitle: Ada\n---\n# Ada Lovelace" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() =>
      expect(saveBrainFile).toHaveBeenCalledWith(
        "entities/ada-lovelace.md",
        "---\ntitle: Ada\n---\n# Ada Lovelace",
      ),
    );
    expect(globalMutate).toHaveBeenCalledWith(fileState.key, expect.any(Function), false);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Edit" })).not.toBeNull());
  });

  it("leaving Edit with unsaved changes asks first; declining keeps the editor", () => {
    treeState.files = POPULATED;
    fileState.file = md("entities/ada-lovelace.md", "# Ada");
    render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByDisplayValue("# Ada"), { target: { value: "# Ada!" } });
    vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.click(screen.getByRole("button", { name: "View" }));
    expect(screen.getByDisplayValue("# Ada!")).toBeInTheDocument();
  });

  it("a stale ?path that 404s shows the missing-page notice and clears the durable param once", () => {
    treeState.files = POPULATED;
    fileState.error = Object.assign(new Error("API 404"), { status: 404 });
    const onPathUnavailable = vi.fn();
    const { rerender } = render(
      <BrainPanel initialPath="main/index.md" onPathUnavailable={(pth) => onPathUnavailable(pth)} />,
    );
    expect(screen.getByText(/No page at main\/index\.md/)).toBeInTheDocument();
    // Once per stale path, not once per render. The shell hands a FRESH inline
    // arrow down on every render (TeamsPanelRouter's brain branch), so the
    // effect's deps really do change — only clearedRef keeps this at 1.
    rerender(
      <BrainPanel initialPath="main/index.md" onPathUnavailable={(pth) => onPathUnavailable(pth)} />,
    );
    expect(onPathUnavailable).toHaveBeenCalledTimes(1);
    expect(onPathUnavailable).toHaveBeenCalledWith("main/index.md");
  });

  it("a rejected ?path (403 from the traversal guard) clears the durable param too", () => {
    // Otherwise a persisted layout reopens the dead link on every restore.
    treeState.files = POPULATED;
    fileState.error = Object.assign(new Error("API 403"), { status: 403 });
    const onPathUnavailable = vi.fn();
    render(<BrainPanel initialPath="../secrets.md" onPathUnavailable={onPathUnavailable} />);
    expect(onPathUnavailable).toHaveBeenCalledWith("../secrets.md");
  });

  it("Refresh asks before replacing a dirty draft", () => {
    treeState.files = POPULATED;
    fileState.file = md("entities/ada-lovelace.md", "# Ada\n");
    render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "# Ada — DRAFT" } });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(treeState.refresh).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toHaveValue("# Ada — DRAFT");
    confirmSpy.mockRestore();
  });

  it("name search filters the tree and says it cannot see contents; no match hands off to ASK with the query", () => {
    treeState.files = POPULATED;
    render(<BrainPanel />);
    const box = screen.getByRole("searchbox", { name: /find a page by name/i });
    fireEvent.change(box, { target: { value: "ada" } });
    expect(screen.getByRole("button", { name: /entities\/ada-lovelace\.md/ })).toBeInTheDocument();
    expect(screen.getByText(/names only/i)).toBeInTheDocument();
    fireEvent.change(box, { target: { value: "pricing" } });
    expect(screen.getByText("No page named that.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /ask friday to search/i }));
    expect(openChatPanelWithDraft).toHaveBeenCalledWith(expect.stringContaining('"pricing"'));
  });

  it("a reports-only vault names the reports instead of claiming nothing is filed", () => {
    treeState.files = REPORTS_ONLY;
    fileState.file = md("index.md", "# Wiki Index");
    render(<BrainPanel />);
    expect(screen.getByText("Only the auto-written reports so far.")).toBeInTheDocument();
    expect(screen.queryByText("Nothing filed yet.")).toBeNull();
    // The count stays honest — wiki_search does index reports/.
    expect(screen.getByTestId("panel-masthead-meta").textContent).toMatch(/1 filed · 0 in inbox/);
  });

  it("searching guards a dirty editor, and an accepted discard does not re-prompt", () => {
    treeState.files = POPULATED;
    fileState.file = md("entities/ada-lovelace.md", "# Ada");
    render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "# Ada — DRAFT" } });
    const box = screen.getByRole("searchbox", { name: /find a page by name/i });

    // Searching unmounts the editor, so it takes the same guard as a tab
    // switch. Declining keeps both the query and the draft.
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.change(box, { target: { value: "ada" } });
    expect((box as HTMLInputElement).value).toBe("");
    expect(screen.getByDisplayValue("# Ada — DRAFT")).toBeInTheDocument();

    confirmSpy.mockReturnValue(true);
    fireEvent.change(box, { target: { value: "ada" } });
    expect((box as HTMLInputElement).value).toBe("ada");
    expect(screen.queryByRole("textbox")).toBeNull();

    // The accepted discard must clear the lifted dirty flag — FileContentViewer
    // never reports dirty=false on unmount, so a stale flag would fire a
    // phantom prompt here.
    expect(confirmSpy).toHaveBeenCalledTimes(2);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Dreams" }));
    expect(confirmSpy).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("dreams-panel-body")).toBeInTheDocument();
  });

  it("searching guards a dirty NON-markdown file, which has no Edit toggle to be `editing` behind", () => {
    // .txt/.csv/.json come back binary:false, so BrainPage hands them straight
    // to the editable viewer and the Edit/View toggle never renders — a draft
    // is dirty while `editing` is false. The guard cannot key on `editing`.
    treeState.files = [...POPULATED, f("sources/raw.txt")];
    fileState.file = md("sources/raw.txt", "hello");
    render(<BrainPanel initialPath="sources/raw.txt" />);
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "hello there" } });
    const box = screen.getByRole("searchbox", { name: /find a page by name/i });

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.change(box, { target: { value: "raw" } });
    expect((box as HTMLInputElement).value).toBe("");
    expect(screen.getByDisplayValue("hello there")).toBeInTheDocument();

    confirmSpy.mockReturnValue(true);
    fireEvent.change(box, { target: { value: "raw" } });
    expect((box as HTMLInputElement).value).toBe("raw");
    expect(screen.queryByDisplayValue("hello there")).toBeNull();

    expect(confirmSpy).toHaveBeenCalledTimes(2);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Dreams" }));
    expect(confirmSpy).toHaveBeenCalledTimes(2);
  });

  it("the toolbar ASK button drafts the open-ended prompt", () => {
    treeState.files = POPULATED;
    render(<BrainPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Ask Friday" }));
    expect(openChatPanelWithDraft).toHaveBeenCalledWith(
      "Look in our brain and tell me what we know about: ",
    );
  });

  it("Delete removes the open page after confirmation", async () => {
    treeState.files = POPULATED;
    fileState.file = md("entities/ada-lovelace.md", "# Ada");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    await waitFor(() => expect(deleteBrainFile).toHaveBeenCalledWith("entities/ada-lovelace.md"));
    expect(treeState.refresh).toHaveBeenCalled();
  });

  it("shows the empty state with an Ask-Friday chip when the vault is empty", () => {
    render(<BrainPanel />);
    expect(
      screen.getByText("Your company's brain fills as your agents learn."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /ask friday/i }));
    expect(openChatPanelWithDraft).toHaveBeenCalledWith(
      "Look in our brain and tell me what we know about: ",
    );
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

  it("uploads dropped files onto the notes pane and refreshes the tree", async () => {
    treeState.files = POPULATED;
    render(<BrainPanel />);
    const pane = screen.getByTestId("brain-notes-pane");
    const file = new File(["hello"], "notes.md", { type: "text/markdown" });
    fireEvent.drop(pane, { dataTransfer: { files: [file] } });
    await waitFor(() => expect(uploadBrainFiles).toHaveBeenCalledWith([file]));
    await waitFor(() => expect(treeState.refresh).toHaveBeenCalled());
  });

  it("uploads files picked via the file input", async () => {
    treeState.files = POPULATED;
    render(<BrainPanel />);
    const input = screen.getByLabelText(/upload files/i) as HTMLInputElement;
    const file = new File(["hello"], "notes.md", { type: "text/markdown" });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(uploadBrainFiles).toHaveBeenCalledWith([file]));
    await waitFor(() => expect(treeState.refresh).toHaveBeenCalled());
  });

  it("shows an error toast and still refreshes when an upload fails", async () => {
    treeState.files = POPULATED;
    uploadBrainFiles.mockRejectedValueOnce(new Error("too big"));
    render(<BrainPanel />);
    const input = screen.getByLabelText(/upload files/i) as HTMLInputElement;
    const file = new File(["hello"], "notes.md", { type: "text/markdown" });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(uploadBrainFiles).toHaveBeenCalled());
    await waitFor(() => expect(treeState.refresh).toHaveBeenCalled());
  });

  it("ignores a second drop while an upload is already in flight (T3 review F7)", async () => {
    treeState.files = POPULATED;
    let resolveUpload: (v: { uploaded: never[] }) => void = () => {};
    uploadBrainFiles.mockImplementation(
      () => new Promise((resolve) => { resolveUpload = resolve; }),
    );
    render(<BrainPanel />);
    const pane = screen.getByTestId("brain-notes-pane");
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
    treeState.files = POPULATED;
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
    treeState.files = POPULATED;
    fileState.file = md("entities/ada-lovelace.md", "# Ada");
    const { rerender } = render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("entities/ada-lovelace.md");
    // openPanel on an already-open panel focuses + merges params rather than
    // remounting — simulate that by rerendering with a new initialPath prop.
    rerender(<BrainPanel initialPath="concepts/icp.md" />);
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("concepts/icp.md");
  });

  describe("ASK with no dock attached (mobile, or pre-roster desktop)", () => {
    it("hands the draft to the mobile chat shell instead of dropping it", () => {
      // attachDockviewApi is the desktop shell's; chat/page.tsx renders
      // MobileChatShell instead, so openChatPanelWithDraft returns false.
      treeState.files = POPULATED;
      openChatPanelWithDraft.mockReturnValue(false);
      const selected: unknown[] = [];
      const focused: unknown[] = [];
      window.addEventListener("selectAgent", (e) => selected.push((e as CustomEvent).detail));
      window.addEventListener("getstarted:focus-composer", (e) =>
        focused.push((e as CustomEvent).detail),
      );

      render(<BrainPanel />);
      fireEvent.click(screen.getByRole("button", { name: "Ask Friday" }));

      expect(consumeFocusComposerRequest("agent_1")).toEqual({
        draft: "Look in our brain and tell me what we know about: ",
      });
      expect(selected).toEqual([{ agentId: "agent_1", source: "notification" }]);
      expect(focused).toEqual([{ agentId: "agent_1" }]);
      expect(window.location.pathname).toBe("/chat");
      expect(notifyErrorSpy).not.toHaveBeenCalled();
    });

    it("says so, and navigates nowhere, when no agent has resolved yet", () => {
      treeState.files = POPULATED;
      openChatPanelWithDraft.mockReturnValue(false);
      agentsState = { defaultId: undefined };
      render(<BrainPanel />);
      fireEvent.click(screen.getByRole("button", { name: "Ask Friday" }));
      expect(notifyErrorSpy).toHaveBeenCalledWith(
        "Friday isn't ready yet — try again in a moment.",
      );
      expect(consumeFocusComposerRequest("agent_1")).toBeNull();
      expect(window.location.pathname).toBe("/chat");
      expect(window.location.search).toBe("?panel=teams-brain");
    });
  });

  describe("the inbox is not a one-way door (I2)", () => {
    it("has an Index exit back to the landing", () => {
      treeState.files = POPULATED;
      render(<BrainPanel />);
      fireEvent.click(screen.getByRole("button", { name: /1 in inbox/ }));
      expect(screen.getByRole("button", { name: /inbox\/deck\.pdf/ })).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Index" }));
      expect(useBrainFileSpy).toHaveBeenLastCalledWith("index.md");
      expect(screen.queryByRole("button", { name: /inbox\/deck\.pdf/ })).toBeNull();
    });

    it("does not offer the count as a button when the inbox is empty", () => {
      treeState.files = SCAFFOLD;
      fileState.file = md("index.md", "# Wiki Index");
      render(<BrainPanel />);
      expect(screen.getByTestId("panel-masthead-meta").textContent).toMatch(/0 in inbox/);
      expect(screen.queryByRole("button", { name: /in inbox/ })).toBeNull();
    });
  });

  it("claims no counts and no zero notice before the tree has been read (I3)", () => {
    treeState.isLoading = true;
    treeState.files = [];
    render(<BrainPanel />);
    expect(screen.queryByTestId("panel-masthead-meta")).toBeNull();
    expect(screen.queryByText("Nothing filed yet.")).toBeNull();
    expect(screen.queryByTestId("brain-empty-drop-zone")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("Refresh re-reads the tree and the open page (I4)", () => {
    treeState.files = POPULATED;
    fileState.file = md("entities/ada-lovelace.md", "# Ada");
    fileState.key = "/container/brain/file?path=entities%2Fada-lovelace.md";
    render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(treeState.refresh).toHaveBeenCalled();
    expect(globalMutate).toHaveBeenCalledWith(fileState.key);
  });

  it("the breadcrumb keeps every directory of a nested page", () => {
    const NESTED = f("entities/people/ada.md");
    treeState.files = [...POPULATED, f("entities/people", "dir"), NESTED];
    fileState.file = md("entities/people/ada.md", "# Ada");
    render(<BrainPanel initialPath="entities/people/ada.md" />);
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(crumbs.textContent).toContain("people");
    // Only the top-level dir has a section index to open.
    fireEvent.click(screen.getByRole("button", { name: "entities" }));
    expect(useBrainFileSpy).toHaveBeenLastCalledWith("entities/index.md");
  });

  describe("Notes | Dreams tabs", () => {
    it("renders a Notes | Dreams tab bar, defaulting to Notes (the vault pane shows)", () => {
      treeState.files = POPULATED;
      render(<BrainPanel />);
      const notesTab = screen.getByRole("tab", { name: "Notes" });
      expect(notesTab).toHaveAttribute("aria-selected", "true");
      expect(screen.getByRole("tab", { name: "Dreams" })).toBeInTheDocument();
      expect(screen.getByTestId("brain-notes-pane")).toBeInTheDocument();
    });

    it("mounts the DreamsPanel body on the Dreams tab and hides the Notes pane", () => {
      treeState.files = POPULATED;
      render(<BrainPanel />);
      // Radix activates a tab on mousedown, not on the synthetic click event.
      fireEvent.mouseDown(screen.getByRole("tab", { name: "Dreams" }));
      expect(screen.getByTestId("dreams-panel-body")).toBeInTheDocument();
      expect(screen.queryByTestId("brain-notes-pane")).not.toBeInTheDocument();
    });

    it("hides the Notes-only masthead meta/actions (counts, size · modified, Delete) on the Dreams tab", () => {
      treeState.files = POPULATED;
      fileState.file = md("entities/ada-lovelace.md", "# Ada");
      render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
      expect(screen.getByTestId("panel-masthead-meta")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /delete/i })).toBeInTheDocument();

      fireEvent.mouseDown(screen.getByRole("tab", { name: "Dreams" }));

      expect(screen.getByTestId("dreams-panel-body")).toBeInTheDocument();
      expect(screen.queryByTestId("panel-masthead-meta")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /delete/i })).not.toBeInTheDocument();
    });

    it("switching back to Notes restores the vault pane", () => {
      treeState.files = POPULATED;
      render(<BrainPanel />);
      fireEvent.mouseDown(screen.getByRole("tab", { name: "Dreams" }));
      expect(screen.getByTestId("dreams-panel-body")).toBeInTheDocument();

      fireEvent.mouseDown(screen.getByRole("tab", { name: "Notes" }));
      expect(screen.getByTestId("brain-notes-pane")).toBeInTheDocument();
      expect(screen.queryByTestId("dreams-panel-body")).not.toBeInTheDocument();
    });

    it("initialTab='dreams' seeds the Dreams tab active on mount (old ?panel=dreams links)", () => {
      treeState.files = POPULATED;
      render(<BrainPanel initialTab="dreams" />);
      expect(screen.getByRole("tab", { name: "Dreams" })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByTestId("dreams-panel-body")).toBeInTheDocument();
      expect(screen.queryByTestId("brain-notes-pane")).not.toBeInTheDocument();
    });

    it("guards a dirty editor on tab switch — declining discard keeps Notes and the draft (review finding 1)", () => {
      treeState.files = POPULATED;
      fileState.file = md("entities/ada-lovelace.md", "# Ada");
      render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
      fireEvent.change(screen.getByDisplayValue("# Ada"), {
        target: { value: "# Ada — DRAFT" },
      });

      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
      fireEvent.mouseDown(screen.getByRole("tab", { name: "Dreams" }));

      expect(confirmSpy).toHaveBeenCalled();
      expect(screen.queryByTestId("dreams-panel-body")).not.toBeInTheDocument();
      // Notes stayed mounted throughout — the draft was never unmounted/reset.
      expect(screen.getByDisplayValue("# Ada — DRAFT")).toBeInTheDocument();
    });

    it("guards a dirty editor on tab switch — accepting discard switches to Dreams (review finding 1)", () => {
      treeState.files = POPULATED;
      fileState.file = md("entities/ada-lovelace.md", "# Ada");
      render(<BrainPanel initialPath="entities/ada-lovelace.md" />);
      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
      fireEvent.change(screen.getByDisplayValue("# Ada"), {
        target: { value: "# Ada — DRAFT" },
      });

      vi.spyOn(window, "confirm").mockReturnValue(true);
      fireEvent.mouseDown(screen.getByRole("tab", { name: "Dreams" }));

      expect(screen.getByTestId("dreams-panel-body")).toBeInTheDocument();
    });
  });
});
