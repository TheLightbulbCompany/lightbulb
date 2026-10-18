import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import type { SWRResponse } from "swr";
import {
  InlineTeamsNavProvider,
  type InlineTeamsNavValue,
} from "@/components/chat/InlineTeamsNav";

// Per-path read stub. The panel reads the stack list; on expand it reads that
// task's artifacts by `groupIssueId`; the viewer reads a document / work-product
// body by path. `mutate`, `del`, `download` are shared spies the tests inspect.
// `readPaths` records every non-null path the panel actually reads so a test can
// assert the expand fetch carries `groupIssueId`.
let reads: Record<string, Partial<SWRResponse<unknown, Error>>> = {};
const readPaths: string[] = [];
const mutate = vi.fn().mockResolvedValue(undefined);
// A null-keyed read is SWR's "don't fetch" signal; its bound mutate is a no-op.
// Distinct spy so a test can prove the panel never leans on one to revalidate.
const nullMutate = vi.fn().mockResolvedValue(undefined);
const del = vi.fn().mockResolvedValue({});
const download = vi.fn();
// SWR's `keepPreviousData`: while a NEW key is in flight the hook keeps serving
// the previous key's data (with isLoading still true). Modelled here for the
// reads that opt in, so an unstubbed path reads as "in flight", not as "empty".
let keptData: unknown;

// The panel invalidates issue-scoped SWR caches via useTeamsApi's scoped
// mutate on delete; spy on it so tests can assert the kind-appropriate key is
// refreshed. Distinct from `mutate` above (each read's own bound mutate).
const teamsMutate = vi.fn().mockResolvedValue(undefined);

vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: vi.fn() }));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null, opts?: { keepPreviousData?: boolean }) => {
      if (path === null) {
        return { data: undefined, isLoading: false, error: undefined, mutate: nullMutate };
      }
      readPaths.push(path);
      const stub = reads[path];
      if (stub) {
        if (opts?.keepPreviousData) {
          if (stub.data !== undefined) keptData = stub.data;
          // SWR keeps serving the PREVIOUS key's data when the new key errors —
          // the case the Recent view must refuse to render as results.
          else if (stub.error !== undefined) {
            return { isLoading: false, mutate, ...stub, data: keptData };
          }
        }
        return { data: undefined, isLoading: false, error: undefined, mutate, ...stub };
      }
      if (opts?.keepPreviousData && keptData !== undefined) {
        return { data: keptData, isLoading: true, error: undefined, mutate };
      }
      return { data: undefined, isLoading: false, error: undefined, mutate };
    },
    del,
    download,
    mutate: teamsMutate,
  }),
}));

// Keep react-markdown out of the unit test — assert the raw body string reached
// the renderer.
vi.mock("@/components/teams/shared/components/MarkdownView", () => ({
  MarkdownView: ({ children }: { children: string }) => <div data-testid="md">{children}</div>,
}));

import { openPanel } from "@/components/workspace/workspaceController";
import { ArtifactsPanel } from "../ArtifactsPanel";

const DOC = {
  id: "document:doc1",
  source: "document",
  mediaKind: "document",
  title: "Implementation plan",
  previewText: "Draft of the implementation plan and milestones.",
  issue: { id: "iss_1", identifier: "ISO-1", title: "Ship the thing" },
  updatedAt: "2026-01-01T00:00:00Z",
  href: "/ACME/issues/ISO-1#document-plan",
};
const WORK_PRODUCT = {
  id: "work_product:wp1",
  source: "work_product",
  mediaKind: "file",
  title: "Deploy preview",
  issue: { id: "iss_1", identifier: "ISO-1", title: "Ship the thing" },
  updatedAt: "2026-01-01T00:00:00Z",
  href: "/ACME/issues/ISO-1#work-product-wp1",
};
// The agent skill's DEFAULT upload: a work product whose bytes sit behind an
// attachment, reachable only through `downloadPath`.
const WORK_PRODUCT_FILE = {
  ...WORK_PRODUCT,
  id: "work_product:wp9",
  title: "Pricing proposal",
  mediaKind: "text",
  contentType: "text/markdown",
  downloadPath: "/api/attachments/att9/content?download=1",
  href: "/ACME/issues/ISO-1#work-product-wp9",
};
const ATTACHMENT = {
  id: "attachment:att1",
  source: "attachment",
  mediaKind: "image",
  title: "Screenshot.png",
  contentType: "image/png",
  issue: { id: "iss_1", identifier: "ISO-1", title: "Ship the thing" },
  updatedAt: "2026-01-01T00:00:00Z",
  href: "/ACME/issues/ISO-1#attachment-att1",
};
const ATTACHMENT_DL = {
  ...ATTACHMENT,
  downloadPath: "/api/attachments/att1/content?download=1",
};

function group(over: Record<string, unknown> = {}) {
  return {
    id: "g1",
    groupBy: "task",
    issue: { id: "iss_1", identifier: "ISO-1", title: "Ship the thing" },
    title: "Ship the thing",
    count: 3,
    mediaKinds: ["document", "image"],
    previewArtifacts: [DOC, WORK_PRODUCT, ATTACHMENT],
    updatedAt: "2026-01-01T00:00:00Z",
    href: "/ACME/artifacts?groupBy=task&groupIssueId=iss_1",
    ...over,
  };
}

// Flat newest-first read (the default Recent view).
const RECENT_PATH = "/artifacts?groupBy=none&limit=100";
const STACK_PATH = "/artifacts?groupBy=task";
function stubRecent(artifacts: unknown[], nextCursor: string | null = null) {
  reads[RECENT_PATH] = { data: { artifacts, nextCursor }, isLoading: false, error: undefined };
}

// Stack-list read (the By task tab).
function stubStackList(groups: unknown[]) {
  reads["/artifacts?groupBy=task"] = {
    data: { artifacts: [], groups, nextCursor: null },
    isLoading: false,
    error: undefined,
  };
}

// Expanded-stack read for one issue (the artifacts of that task).
function stubExpanded(
  issueId: string,
  artifacts: unknown[],
  selectedGroup: unknown = group(),
) {
  reads[`/artifacts?groupBy=task&groupIssueId=${issueId}`] = {
    data: { artifacts, groups: [], selectedGroup, nextCursor: null },
    isLoading: false,
    error: undefined,
  };
}

// Radix tabs activate on mousedown, not click (see InboxToolbar.test.tsx).
function switchToByTask() {
  fireEvent.mouseDown(screen.getByRole("tab", { name: "By task" }));
}
function switchToRecent() {
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Recent" }));
}

// The By task tab renders stack cards; click the (single) stack to expand it.
function expandStack() {
  fireEvent.click(screen.getByTestId("artifact-stack-card"));
}

// Deleting revalidates the gallery with ONE key filter over every
// `/teams/artifacts?…` key (see `confirmDelete`) rather than the exact keys the
// panel happens to be subscribed to. Pull the predicate off the spy so tests
// assert on what it MATCHES, not on its identity.
function galleryKeyFilter(): (key?: unknown) => boolean {
  const call = teamsMutate.mock.calls.find(([k]) => typeof k === "function");
  if (!call) throw new Error("no key-filter mutate call");
  return call[0] as (key?: unknown) => boolean;
}

function cardFor(title: string): HTMLElement {
  const card = screen
    .getAllByTestId("artifact-card")
    .find((c) => within(c).queryByText(title, { selector: "h3" }));
  if (!card) throw new Error(`no artifact card titled ${title}`);
  return card;
}

beforeEach(() => {
  // A search test that throws before its own `useRealTimers` must not leave
  // fake timers installed for every test after it.
  vi.useRealTimers();
  reads = {};
  keptData = undefined;
  readPaths.length = 0;
  mutate.mockClear();
  nullMutate.mockClear();
  teamsMutate.mockClear();
  del.mockClear();
  del.mockResolvedValue({});
  download.mockReset();
  // Default: image thumbnails resolve to a blob so ImagePreview doesn't reject.
  download.mockResolvedValue(new Blob(["x"], { type: "image/png" }));
  // jsdom has no object-URL impl — stub it for thumbnails + the viewer.
  (globalThis.URL.createObjectURL as unknown) = vi.fn(() => "blob:fake");
  (globalThis.URL.revokeObjectURL as unknown) = vi.fn();
});

describe("ArtifactsPanel — Recent view (default)", () => {
  it("opens on a flat newest-first feed bucketed by date, without a task stack", async () => {
    stubRecent([
      { ...DOC, updatedAt: new Date().toISOString() },
      { ...ATTACHMENT, updatedAt: "2020-01-01T00:00:00Z" },
    ]);
    render(<ArtifactsPanel />);
    expect(readPaths).toContain(RECENT_PATH);
    expect(readPaths).not.toContain("/artifacts?groupBy=task");
    expect(screen.getByRole("heading", { name: "Past 7 days" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Older" })).toBeInTheDocument();
    expect(screen.getAllByTestId("artifact-card")).toHaveLength(2);
    expect(screen.queryByTestId("artifact-stack-card")).not.toBeInTheDocument();
    // The image card's thumbnail resolves off-render; flush it inside the test.
    await waitFor(() => expect(download).toHaveBeenCalledWith("/issues/iss_1/attachments/att1/content"));
  });

  it("says when the feed is capped so an owner never mistakes 100 for everything", () => {
    stubRecent([DOC], "cursor-1");
    render(<ArtifactsPanel />);
    expect(
      screen.getByText("Showing the latest 100. Search to find older deliverables."),
    ).toBeInTheDocument();
  });

  it("tells an owner who is already searching to narrow, not to search", () => {
    vi.useFakeTimers();
    stubRecent([DOC], "cursor-1");
    reads[`${RECENT_PATH}&q=plan`] = {
      data: { artifacts: [DOC], nextCursor: "cursor-2" },
      isLoading: false,
      error: undefined,
    };
    render(<ArtifactsPanel />);

    fireEvent.change(screen.getByRole("searchbox", { name: "Search the Library" }), {
      target: { value: "plan" },
    });
    act(() => {
      vi.advanceTimersByTime(300);
    });

    expect(
      screen.getByText("Showing the latest 100. Narrow your search to find older deliverables."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Showing the latest 100. Search to find older deliverables."),
    ).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("switches to the By task stacks and back", () => {
    stubRecent([DOC]);
    stubStackList([group()]);
    render(<ArtifactsPanel />);
    switchToByTask();
    expect(screen.getByTestId("artifact-stack-card")).toBeInTheDocument();
    switchToRecent();
    expect(screen.getByTestId("artifact-card")).toBeInTheDocument();
  });
});

describe("ArtifactsPanel — expanded stack survives a tab round trip", () => {
  it("comes back to the stack LIST, not the stack it had open", () => {
    stubRecent([DOC]);
    stubStackList([group()]);
    stubExpanded("iss_1", [WORK_PRODUCT]);
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();
    expect(screen.getByTestId("artifact-card")).toBeInTheDocument();

    switchToRecent();
    switchToByTask();

    expect(screen.getByTestId("artifact-stack-card")).toBeInTheDocument();
    expect(screen.queryByTestId("artifact-card")).not.toBeInTheDocument();
  });
});

describe("ArtifactsPanel — stack list + expand", () => {
  it("shows a stack card by default and expands it into artifact cards, fetching by groupIssueId", () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [DOC, WORK_PRODUCT]);
    render(<ArtifactsPanel />);
    switchToByTask();

    // Default view: the stack card, no per-artifact cards yet.
    expect(screen.getByTestId("artifact-stack-card")).toBeInTheDocument();
    expect(screen.queryByTestId("artifact-card")).not.toBeInTheDocument();

    expandStack();

    // The expand fetch carried the groupIssueId, and the artifact cards render.
    expect(readPaths).toContain("/artifacts?groupBy=task&groupIssueId=iss_1");
    expect(screen.getAllByTestId("artifact-card").length).toBe(2);
    expect(screen.queryByTestId("artifact-stack-card")).not.toBeInTheDocument();
  });

  it("returns to the stack list via the back control", () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [DOC]);
    render(<ArtifactsPanel />);
    switchToByTask();

    expandStack();
    expect(screen.getByTestId("artifact-card")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("artifact-stack-back"));
    expect(screen.getByTestId("artifact-stack-card")).toBeInTheDocument();
    expect(screen.queryByTestId("artifact-card")).not.toBeInTheDocument();
  });
});

describe("ArtifactsPanel — artifact card previews", () => {
  it("renders a preview-text box for a doc and an icon for a no-preview file, with title + identifier", () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [DOC, WORK_PRODUCT]);
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();

    const docCard = cardFor("Implementation plan");
    expect(within(docCard).getByTestId("artifact-preview-text")).toHaveTextContent(
      "Draft of the implementation plan and milestones.",
    );
    // Both title and the issue identifier show on the card.
    expect(within(docCard).getByText("Implementation plan")).toBeInTheDocument();
    expect(within(docCard).getByText("ISO-1")).toBeInTheDocument();

    const fileCard = cardFor("Deploy preview");
    expect(within(fileCard).getByTestId("artifact-preview-icon")).toBeInTheDocument();
    expect(within(fileCard).queryByTestId("artifact-preview-text")).not.toBeInTheDocument();
  });
});

describe("ArtifactsPanel — open in the artifact pane", () => {
  const PANE = { pushUrl: true };

  it("opens a document by its key", () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [DOC]);
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();
    fireEvent.click(cardFor("Implementation plan"));
    expect(openPanel).toHaveBeenCalledWith("artifact", { src: "paperclip", kind: "document", issue: "iss_1", key: "plan", ident: "ISO-1" }, PANE);
  });

  it("opens an attachment by its raw id", () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [ATTACHMENT]);
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();
    fireEvent.click(cardFor("Screenshot.png"));
    expect(openPanel).toHaveBeenCalledWith("artifact", { src: "paperclip", kind: "attachment", issue: "iss_1", id: "att1", name: "Screenshot.png", type: "image/png" }, PANE);
  });

  it("opens a file-backed work product by the attachment id in its download path", () => {
    stubRecent([WORK_PRODUCT_FILE]);
    render(<ArtifactsPanel />);
    fireEvent.click(cardFor("Pricing proposal"));
    expect(openPanel).toHaveBeenCalledWith("artifact", { src: "paperclip", kind: "work_product", issue: "iss_1", id: "att9", name: "Pricing proposal", type: "text/markdown" }, PANE);
  });

  it("falls back to opening the task when no pane params can be derived", () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [WORK_PRODUCT]);
    const openDetail = vi.fn();
    render(
      <InlineTeamsNavProvider value={{ openDetail, leaveToList: vi.fn() }}>
        <ArtifactsPanel />
      </InlineTeamsNavProvider>,
    );
    switchToByTask();
    expandStack();
    fireEvent.click(cardFor("Deploy preview"));
    expect(openPanel).not.toHaveBeenCalled();
    expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss_1");
  });
});

describe("ArtifactsPanel — secondary icons stop propagation (do not open the viewer)", () => {
  it("open-in-issue routes to the issue detail and does not open the viewer", () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [DOC]);
    const openDetail = vi.fn();
    const value: InlineTeamsNavValue = { openDetail, leaveToList: vi.fn() };
    render(
      <InlineTeamsNavProvider value={value}>
        <ArtifactsPanel />
      </InlineTeamsNavProvider>,
    );
    switchToByTask();
    expandStack();

    const link = within(cardFor("Implementation plan")).getByRole("link", {
      name: "Open in issue",
    });
    expect(link).toHaveAttribute("href", "/chat?panel=teams-issue-detail&detailId=iss_1");
    fireEvent.click(link);

    expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss_1");
    // The card's open-viewer surface must NOT have fired.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("download triggers the authenticated download and does not open the viewer", async () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [ATTACHMENT_DL]);
    // jsdom doesn't honor the <a download> attribute and logs a navigation
    // "not implemented" warning for the programmatic click; stub it and assert
    // the anchor was clicked to save the fetched bytes.
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();

    fireEvent.click(
      within(cardFor("Screenshot.png")).getByRole("button", { name: "Download Screenshot.png" }),
    );

    await waitFor(() => expect(download).toHaveBeenCalledWith("/issues/iss_1/attachments/att1/content"));
    await waitFor(() => expect(anchorClick).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    anchorClick.mockRestore();
  });

  it("delete opens the confirm gate and does not open the viewer", () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [DOC]);
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();

    fireEvent.click(
      within(cardFor("Implementation plan")).getByRole("button", {
        name: "Delete Implementation plan",
      }),
    );

    expect(screen.getByText("Delete this deliverable?")).toBeInTheDocument();
    // The confirm dialog is a dialog, but the VIEWER (which would show the
    // markdown body) must not have opened.
    expect(screen.queryByTestId("md")).not.toBeInTheDocument();
    expect(del).not.toHaveBeenCalled();
  });
});

describe("ArtifactsPanel — delete", () => {
  it("confirms before deleting a document, then calls the document DELETE endpoint and refreshes", async () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [DOC]);
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();

    fireEvent.click(screen.getByRole("button", { name: "Delete Implementation plan" }));
    expect(screen.getByText("Delete this deliverable?")).toBeInTheDocument();
    expect(del).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("/issues/iss_1/documents/plan"));
    // Invalidates the open stack (bound mutate) and the issue-scoped documents
    // list so the task detail drops the deleted artifact too.
    expect(mutate).toHaveBeenCalled();
    await waitFor(() =>
      expect(teamsMutate).toHaveBeenCalledWith("/teams/issues/iss_1/documents"),
    );
    // …and the OTHER view's key, which is null-keyed right now: its bound mutate
    // is dead, so the refresh has to go through the global cache or the deleted
    // deliverable is still listed when the owner flips to Recent.
    expect(galleryKeyFilter()(`/teams${RECENT_PATH}`)).toBe(true);
    expect(nullMutate).not.toHaveBeenCalled();
  });

  it("revalidates the By task list when the delete happens in Recent", async () => {
    stubRecent([DOC]);
    render(<ArtifactsPanel />);

    fireEvent.click(screen.getByRole("button", { name: "Delete Implementation plan" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(del).toHaveBeenCalledWith("/issues/iss_1/documents/plan"));
    await waitFor(() => expect(teamsMutate).toHaveBeenCalledWith(expect.any(Function)));
    expect(galleryKeyFilter()(`/teams${STACK_PATH}`)).toBe(true);
    expect(nullMutate).not.toHaveBeenCalled();
  });

  it("routes a work-product delete to the issue-scoped work-products DELETE endpoint", async () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [WORK_PRODUCT]);
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();

    fireEvent.click(screen.getByRole("button", { name: "Delete Deploy preview" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("/issues/iss_1/work-products/wp1"));
    await waitFor(() =>
      expect(teamsMutate).toHaveBeenCalledWith("/teams/issues/iss_1/work-products"),
    );
  });

  it("routes an attachment delete to the issue-scoped attachment DELETE endpoint", async () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [ATTACHMENT]);
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();

    fireEvent.click(screen.getByRole("button", { name: "Delete Screenshot.png" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("/issues/iss_1/attachments/att1"));
    await waitFor(() =>
      expect(teamsMutate).toHaveBeenCalledWith("/teams/issues/iss_1/attachments"),
    );
  });

  it("keeps the confirm dialog open with an error when the delete fails", async () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [DOC]);
    del.mockRejectedValueOnce(new Error("boom"));
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();

    fireEvent.click(screen.getByRole("button", { name: "Delete Implementation plan" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    expect(
      await screen.findByText("Couldn’t delete this deliverable. Please try again."),
    ).toBeInTheDocument();
    // Dialog stays open (still asking to confirm), gallery not revalidated.
    expect(screen.getByText("Delete this deliverable?")).toBeInTheDocument();
    expect(teamsMutate).not.toHaveBeenCalled();
  });

  it("cancels without deleting", () => {
    stubStackList([group()]);
    stubExpanded("iss_1", [DOC]);
    render(<ArtifactsPanel />);
    switchToByTask();
    expandStack();

    fireEvent.click(screen.getByRole("button", { name: "Delete Implementation plan" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(del).not.toHaveBeenCalled();
  });
});

describe("ArtifactsPanel — sort", () => {
  it("orders stacks newest-first by default and reverses to oldest-first", () => {
    stubStackList([
      group({
        id: "g_new",
        issue: { id: "iss_new", identifier: "ISO-2", title: "Newer" },
        title: "Newer",
        updatedAt: "2026-02-01T00:00:00Z",
      }),
      group({
        id: "g_old",
        issue: { id: "iss_old", identifier: "ISO-1", title: "Older" },
        title: "Older",
        updatedAt: "2026-01-01T00:00:00Z",
      }),
    ]);
    render(<ArtifactsPanel />);
    switchToByTask();

    const identifiersNewest = screen.getAllByText(/^ISO-\d$/).map((el) => el.textContent);
    expect(identifiersNewest).toEqual(["ISO-2", "ISO-1"]);

    fireEvent.click(screen.getByRole("button", { name: "Oldest" }));
    const identifiersOldest = screen.getAllByText(/^ISO-\d$/).map((el) => el.textContent);
    expect(identifiersOldest).toEqual(["ISO-1", "ISO-2"]);
  });
});

describe("ArtifactsPanel \u2014 search", () => {
  it("debounces the box and re-reads with q on both views", () => {
    vi.useFakeTimers();
    stubRecent([DOC]);
    stubStackList([group()]);
    render(<ArtifactsPanel />);

    fireEvent.change(screen.getByRole("searchbox", { name: "Search the Library" }), {
      target: { value: "pricing" },
    });
    // Still the unsuffixed key \u2014 a keystroke must not be a BFF search.
    expect(readPaths).not.toContain(`${RECENT_PATH}&q=pricing`);

    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(readPaths).toContain(`${RECENT_PATH}&q=pricing`);

    // The query rides the By task read too \u2014 it is one box over the whole panel.
    switchToByTask();
    expect(readPaths).toContain("/artifacts?groupBy=task&q=pricing");
    vi.useRealTimers();
  });

  it("shows a no-match notice with a Clear exit instead of the zero copy", () => {
    vi.useFakeTimers();
    stubRecent([DOC]);
    // The q read RESOLVES empty. An unstubbed path now reads as "still in
    // flight" (the feed keeps previous data), and a miss is only a miss once
    // the search has actually come back.
    reads[`${RECENT_PATH}&q=zzz`] = {
      data: { artifacts: [], nextCursor: null },
      isLoading: false,
      error: undefined,
    };
    render(<ArtifactsPanel />);

    fireEvent.change(screen.getByRole("searchbox", { name: "Search the Library" }), {
      target: { value: "zzz" },
    });
    act(() => {
      vi.advanceTimersByTime(300);
    });

    // A search miss, not an empty Library.
    expect(screen.getByText("Nothing in the Library matches \u201Czzz\u201D.")).toBeInTheDocument();
    expect(screen.queryByText("Nothing made yet.")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(screen.getByTestId("artifact-card")).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("keeps the loaded cards on screen while the settled search is still in flight", () => {
    vi.useFakeTimers();
    stubRecent([DOC]);
    const { rerender } = render(<ArtifactsPanel />);

    fireEvent.change(screen.getByRole("searchbox", { name: "Search the Library" }), {
      target: { value: "pricing" },
    });
    act(() => {
      vi.advanceTimersByTime(300);
    });

    // The new key is in flight: the feed the owner was reading stays put — no
    // blank "Loading the Library…", and no premature no-match claim.
    expect(readPaths).toContain(`${RECENT_PATH}&q=pricing`);
    expect(screen.getByTestId("artifact-card")).toBeInTheDocument();
    expect(screen.queryByText("Loading the Library…")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Nothing in the Library matches “pricing”."),
    ).not.toBeInTheDocument();

    // …and the notice lands once that read actually comes back empty.
    reads[`${RECENT_PATH}&q=pricing`] = {
      data: { artifacts: [], nextCursor: null },
      isLoading: false,
      error: undefined,
    };
    rerender(<ArtifactsPanel />);
    expect(screen.getByText("Nothing in the Library matches “pricing”.")).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("shows unavailable — not the previous query's cards — when a search fails", () => {
    vi.useFakeTimers();
    stubRecent([DOC]);
    reads[`${RECENT_PATH}&q=plan`] = {
      data: { artifacts: [DOC], nextCursor: null },
      isLoading: false,
      error: undefined,
    };
    reads[`${RECENT_PATH}&q=planning`] = {
      data: undefined,
      isLoading: false,
      error: new Error("boom"),
    };
    render(<ArtifactsPanel />);
    const box = screen.getByRole("searchbox", { name: "Search the Library" });

    fireEvent.change(box, { target: { value: "plan" } });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(screen.getByTestId("artifact-card")).toBeInTheDocument();

    fireEvent.change(box, { target: { value: "planning" } });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    vi.useRealTimers();

    // `keepPreviousData` still holds the "plan" results. Rendering them here
    // would claim they matched "planning" — a read failure is never results.
    expect(screen.queryByTestId("artifact-card")).not.toBeInTheDocument();
    const notice = screen.getByRole("status");
    expect(notice).toHaveAttribute("data-panel-notice", "unavailable");
    expect(
      within(notice).getByText("We couldn't load the Library right now."),
    ).toBeInTheDocument();
  });

  it("revalidates the q-suffixed gallery key when a delete happens under a search", async () => {
    vi.useFakeTimers();
    stubRecent([DOC]);
    reads[`${RECENT_PATH}&q=pricing`] = {
      data: { artifacts: [DOC], nextCursor: null },
      isLoading: false,
      error: undefined,
    };
    render(<ArtifactsPanel />);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search the Library" }), {
      target: { value: "pricing" },
    });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    vi.useRealTimers();

    fireEvent.click(screen.getByRole("button", { name: "Delete Implementation plan" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(del).toHaveBeenCalledWith("/issues/iss_1/documents/plan"));
    // Every `q` is its own key. Revalidating only the pair the panel is
    // subscribed to leaves the deleted deliverable listed under the bare feed
    // the owner sees the moment they clear the box — so one key filter covers
    // the whole gallery, and nothing else.
    await waitFor(() => expect(teamsMutate).toHaveBeenCalledWith(expect.any(Function)));
    const matches = galleryKeyFilter();
    expect(matches(`/teams${RECENT_PATH}`)).toBe(true);
    expect(matches(`/teams${RECENT_PATH}&q=pricing`)).toBe(true);
    expect(matches(`/teams${RECENT_PATH}&q=foo`)).toBe(true);
    expect(matches(`/teams${STACK_PATH}`)).toBe(true);
    expect(matches("/teams/issues")).toBe(false);
  });
});

describe("ArtifactsPanel \u2014 honest states", () => {
  it("renders the zero notice when there is genuinely nothing", () => {
    stubRecent([]);
    render(<ArtifactsPanel />);
    const notice = screen.getByRole("status");
    expect(notice).toHaveAttribute("data-panel-notice", "zero");
    expect(within(notice).getByText("Nothing made yet.")).toBeInTheDocument();
    // The panel is "Library" everywhere else (masthead, sidebar, tab title);
    // the IA rename left "artifacts" behind in the body copy. Nothing the
    // reader sees may say it.
    expect(screen.queryByText(/artifacts?/i)).not.toBeInTheDocument();
  });

  it("renders unavailable — never zero — when the read fails", () => {
    reads[RECENT_PATH] = {
      data: undefined,
      isLoading: false,
      error: new Error("boom"),
    };
    render(<ArtifactsPanel />);
    const notice = screen.getByRole("status");
    expect(notice).toHaveAttribute("data-panel-notice", "unavailable");
    expect(
      within(notice).getByText("We couldn't load the Library right now."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Nothing made yet.")).not.toBeInTheDocument();
  });
});
