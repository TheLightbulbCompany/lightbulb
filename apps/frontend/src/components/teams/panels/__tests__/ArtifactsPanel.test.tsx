import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { SWRResponse } from "swr";
import {
  InlineTeamsNavProvider,
  type InlineTeamsNavValue,
} from "@/components/chat/InlineTeamsNav";

// Per-path read stub. The panel reads the grouped gallery; the viewer reads a
// document / work-product body by path. `mutate`, `del`, `download` are shared
// spies the tests inspect.
let reads: Record<string, Partial<SWRResponse<unknown, Error>>> = {};
const mutate = vi.fn().mockResolvedValue(undefined);
const del = vi.fn().mockResolvedValue({});
const download = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) =>
      path !== null && reads[path]
        ? { data: undefined, isLoading: false, error: undefined, mutate, ...reads[path] }
        : { data: undefined, isLoading: false, error: undefined, mutate },
    del,
    download,
  }),
}));

// Keep react-markdown out of the unit test — assert the raw body string reached
// the renderer.
vi.mock("@/components/teams/shared/components/MarkdownView", () => ({
  MarkdownView: ({ children }: { children: string }) => <div data-testid="md">{children}</div>,
}));

import { ArtifactsPanel } from "../ArtifactsPanel";

const DOC = {
  id: "document:doc1",
  source: "document",
  mediaKind: "document",
  title: "Implementation plan",
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

function stubGallery(groups: unknown[]) {
  reads["/artifacts?groupBy=task"] = {
    data: { artifacts: [], groups, nextCursor: null },
    isLoading: false,
    error: undefined,
  };
}

beforeEach(() => {
  reads = {};
  mutate.mockClear();
  del.mockClear();
  download.mockReset();
  // jsdom has no object-URL impl — stub it for the attachment viewer.
  (globalThis.URL.createObjectURL as unknown) = vi.fn(() => "blob:fake");
  (globalThis.URL.revokeObjectURL as unknown) = vi.fn();
});

describe("ArtifactsPanel — view on click", () => {
  it("opens a document artifact's content in the viewer (markdown body fetched by key)", async () => {
    stubGallery([group()]);
    reads["/issues/iss_1/documents/plan"] = {
      data: { id: "doc1", key: "plan", body: "Implementation details here" },
    };
    render(<ArtifactsPanel />);

    // Title text lives only on the open-viewer button (the trash button is
    // icon-only, so getByText is unambiguous).
    fireEvent.click(screen.getByText(/Implementation plan/));

    expect(await screen.findByText("Implementation details here")).toBeInTheDocument();
  });

  it("opens a work-product artifact's content in the viewer (summary fetched + matched by id)", async () => {
    stubGallery([group()]);
    reads["/issues/iss_1/work-products"] = {
      data: { workProducts: [{ id: "wp1", title: "Deploy preview", summary: "Shipped to staging", url: "https://x" }] },
    };
    render(<ArtifactsPanel />);

    fireEvent.click(screen.getByText(/Deploy preview/));

    expect(await screen.findByText("Shipped to staging")).toBeInTheDocument();
  });

  it("opens an image attachment inline via the BFF content route", async () => {
    stubGallery([group()]);
    download.mockResolvedValue(new Blob(["x"], { type: "image/png" }));
    render(<ArtifactsPanel />);

    fireEvent.click(screen.getByText(/Screenshot\.png/));

    const img = await screen.findByAltText("Screenshot.png");
    expect(img).toBeInTheDocument();
    expect(download).toHaveBeenCalledWith("/attachments/att1/content");
  });

  it("keeps the group header as a secondary open-task affordance", () => {
    stubGallery([group()]);
    const openDetail = vi.fn();
    const value: InlineTeamsNavValue = { openDetail, leaveToList: vi.fn() };
    render(
      <InlineTeamsNavProvider value={value}>
        <ArtifactsPanel />
      </InlineTeamsNavProvider>,
    );

    const header = screen.getByText("ISO-1").closest("a");
    expect(header).toHaveAttribute("href", "/chat?panel=teams-issue-detail&detailId=iss_1");
    fireEvent.click(screen.getByText("ISO-1"));
    expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss_1");
  });
});

describe("ArtifactsPanel — delete", () => {
  it("confirms before deleting a document, then calls the document DELETE endpoint and refreshes", async () => {
    stubGallery([group()]);
    render(<ArtifactsPanel />);

    fireEvent.click(screen.getByRole("button", { name: "Delete Implementation plan" }));
    // Confirm gate shown; nothing deleted yet.
    expect(screen.getByText("Delete this artifact?")).toBeInTheDocument();
    expect(del).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("/issues/iss_1/documents/plan"));
    expect(mutate).toHaveBeenCalled();
  });

  it("routes a work-product delete to the work-products DELETE endpoint", async () => {
    stubGallery([group()]);
    render(<ArtifactsPanel />);

    fireEvent.click(screen.getByRole("button", { name: "Delete Deploy preview" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("/work-products/wp1"));
  });

  it("routes an attachment delete to the issue-scoped attachment DELETE endpoint", async () => {
    stubGallery([group()]);
    render(<ArtifactsPanel />);

    fireEvent.click(screen.getByRole("button", { name: "Delete Screenshot.png" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("/issues/iss_1/attachments/att1"));
  });

  it("cancels without deleting", () => {
    stubGallery([group()]);
    render(<ArtifactsPanel />);

    fireEvent.click(screen.getByRole("button", { name: "Delete Implementation plan" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(del).not.toHaveBeenCalled();
  });
});

describe("ArtifactsPanel — sort", () => {
  it("orders groups newest-first by default and reverses to oldest-first", () => {
    stubGallery([
      group({ id: "g_new", issue: { id: "iss_new", identifier: "ISO-2", title: "Newer" }, title: "Newer", updatedAt: "2026-02-01T00:00:00Z" }),
      group({ id: "g_old", issue: { id: "iss_old", identifier: "ISO-1", title: "Older" }, title: "Older", updatedAt: "2026-01-01T00:00:00Z" }),
    ]);
    render(<ArtifactsPanel />);

    const identifiersNewest = screen.getAllByText(/^ISO-\d$/).map((el) => el.textContent);
    expect(identifiersNewest).toEqual(["ISO-2", "ISO-1"]);

    fireEvent.click(screen.getByRole("button", { name: "Oldest" }));
    const identifiersOldest = screen.getAllByText(/^ISO-\d$/).map((el) => el.textContent);
    expect(identifiersOldest).toEqual(["ISO-1", "ISO-2"]);
  });
});

describe("ArtifactsPanel — empty", () => {
  it("shows the empty state when there are no groups", () => {
    stubGallery([]);
    render(<ArtifactsPanel />);
    expect(screen.getByText("No artifacts yet.")).toBeInTheDocument();
  });
});
