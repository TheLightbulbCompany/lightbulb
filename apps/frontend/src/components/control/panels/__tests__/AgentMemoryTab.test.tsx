import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

const mockGet = vi.fn();
const mockDel = vi.fn();
const mockPost = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: mockGet, del: mockDel, post: mockPost }),
}));

let mockOrg = "org_a";
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ orgId: mockOrg, userId: "user_1" }) }));

import { AgentMemoryTab, resetMemoryOverlay } from "../AgentMemoryTab";

const LIST = {
  private: [{ id: "mem-p", text: "I am allergic to nuts", createdAt: "2026-10-02T00:00:00+00:00", confirmed: false }],
  team: [
    { id: "mem-t1", text: "Warehouse is in Leeds", createdAt: "2026-10-01T00:00:00+00:00", confirmed: true },
    { id: "mem-t2", text: "Ships on Tuesdays", createdAt: "2026-10-01T00:00:00+00:00", confirmed: false },
  ],
  truncated: false,
};

const renderTab = () =>
  render(
    <SWRConfig value={{ provider: () => new Map(), revalidateOnFocus: false, dedupingInterval: 0 }}>
      <AgentMemoryTab />
    </SWRConfig>,
  );

beforeEach(() => {
  resetMemoryOverlay();
  mockOrg = "org_a";
  mockGet.mockReset().mockResolvedValue(LIST);
  mockDel.mockReset().mockResolvedValue(undefined);
  mockPost.mockReset();
});
afterEach(() => vi.restoreAllMocks());

const memoryGets = () => mockGet.mock.calls.filter(([url]) => url === "/memory").length;
const DRAFTS = {
  drafts: [
    { id: "d1", kind: "lesson", text: "Run migrations before the seed", evidence: ["s1", "s-gone"], status: "proposed" },
    { id: "d2", kind: "finding", text: "Token in a log line", evidence: [], status: "proposed" },
  ],
};
// GET by url: the memory list, the drafts, the viewer's sessions.
const route = (drafts: unknown = { drafts: [] }) =>
  mockGet.mockImplementation(async (url: string) =>
    url === "/memory/drafts" ? drafts : url === "/sessions" ? { sessions: [{ id: "s1" }] } : LIST);

describe("AgentMemoryTab", () => {
  it("renders Only you and Team with Confirmed on mem-t1 only", async () => {
    renderTab();
    expect(await screen.findByText("I am allergic to nuts")).toBeInTheDocument();
    expect(screen.getByText("Only you")).toBeInTheDocument();
    expect(screen.getByText("Team")).toBeInTheDocument();
    expect(screen.getByText("Warehouse is in Leeds")).toBeInTheDocument();
    expect(screen.getByText("Ships on Tuesdays")).toBeInTheDocument();
    expect(screen.getAllByText("Confirmed")).toHaveLength(1);
  });

  it("deletes a row after confirm", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    // Delete revalidates, so the server's next list must already lack the row.
    mockDel.mockImplementation(async () => {
      mockGet.mockResolvedValue({ ...LIST, private: [] });
    });
    renderTab();
    await screen.findByText("I am allergic to nuts");
    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    await waitFor(() => expect(mockDel).toHaveBeenCalledWith("/memory/mem-p"));
    await waitFor(() => expect(screen.queryByText("I am allergic to nuts")).not.toBeInTheDocument());
  });

  it("does not delete when cancelled", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderTab();
    await screen.findByText("I am allergic to nuts");
    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    expect(mockDel).not.toHaveBeenCalled();
  });

  it("shares optimistically without refetching", async () => {
    mockPost.mockResolvedValue({ id: "mem-new" });
    renderTab();
    await screen.findByText("I am allergic to nuts");
    expect(screen.getAllByRole("button", { name: "Share with team" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Share with team" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/memory/mem-p/share", {}));
    await waitFor(() => expect(screen.getAllByText("Confirmed")).toHaveLength(2));
    expect(screen.getByText("I am allergic to nuts")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Share with team" })).not.toBeInTheDocument();
    expect(memoryGets()).toBe(1);
  });

  it("confirms optimistically without refetching", async () => {
    mockPost.mockResolvedValue({ id: "mem-new2" });
    renderTab();
    await screen.findByText("Ships on Tuesdays");
    expect(screen.getAllByRole("button", { name: "Confirm" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/memory/mem-t2/confirm", {}));
    await waitFor(() => expect(screen.getAllByText("Confirmed")).toHaveLength(2));
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
    expect(memoryGets()).toBe(1);
  });

  it("shows the empty state", async () => {
    mockGet.mockResolvedValue({ private: [], team: [], truncated: false });
    renderTab();
    expect((await screen.findAllByText(/Nothing remembered yet/))[0]).toHaveTextContent(
      "Nothing remembered yet. New facts appear a few minutes after a conversation.",
    );
  });

  it("shows the truncation note", async () => {
    mockGet.mockResolvedValue({ ...LIST, truncated: true });
    renderTab();
    expect(await screen.findByText("Showing 200 of them.")).toBeInTheDocument();
  });

  it("shows a failed delete in an alert", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mockDel.mockRejectedValue(new Error("boom"));
    renderTab();
    await screen.findByText("I am allergic to nuts");
    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });

  it("keeps a shared row through a revalidation that still returns the old lists", async () => {
    mockPost.mockResolvedValue({ id: "mem-new" });
    renderTab();
    await screen.findByText("I am allergic to nuts");
    fireEvent.click(screen.getByRole("button", { name: "Share with team" }));
    await waitFor(() => expect(screen.getAllByText("Confirmed")).toHaveLength(2));
    // A delete revalidates; the mocked GET still returns the OLD lists.
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[2]);
    await waitFor(() => expect(memoryGets()).toBe(2));
    // The refetched (old) lists must RENDER: mem-t2 is deleted and gone, the rest stays.
    await waitFor(() => expect(screen.queryByText("Ships on Tuesdays")).not.toBeInTheDocument());
    expect(screen.getAllByText("I am allergic to nuts")).toHaveLength(1);
    expect(screen.getAllByText("Confirmed")).toHaveLength(2);
  });

  it("keeps the overlay across unmount and remount", async () => {
    mockPost.mockResolvedValue({ id: "mem-new" });
    const first = renderTab();
    await screen.findByText("I am allergic to nuts");
    fireEvent.click(screen.getByRole("button", { name: "Share with team" }));
    await waitFor(() => expect(screen.getAllByText("Confirmed")).toHaveLength(2));
    first.unmount();
    renderTab();
    await screen.findByText("Warehouse is in Leeds");
    expect(screen.getAllByText("Confirmed")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Share with team" })).not.toBeInTheDocument();
  });

  it("keeps two concurrent actions", async () => {
    let releaseShare: (v: { id: string }) => void = () => {};
    mockPost.mockImplementation((url: string) =>
      url.endsWith("/share")
        ? new Promise((r) => { releaseShare = r; })
        : Promise.resolve({ id: "mem-new2" }),
    );
    renderTab();
    await screen.findByText("I am allergic to nuts");
    fireEvent.click(screen.getByRole("button", { name: "Share with team" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(screen.getAllByText("Confirmed")).toHaveLength(2));
    expect(screen.getByRole("button", { name: "Share with team" })).toBeDisabled();
    releaseShare({ id: "mem-new" });
    await waitFor(() => expect(screen.getAllByText("Confirmed")).toHaveLength(3));
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("does not show org A's overlay rows in org B", async () => {
    mockPost.mockResolvedValue({ id: "mem-new" });
    const first = renderTab();
    await screen.findByText("I am allergic to nuts");
    fireEvent.click(screen.getByRole("button", { name: "Share with team" }));
    await waitFor(() => expect(screen.getAllByText("Confirmed")).toHaveLength(2));
    first.unmount();
    mockOrg = "org_b";
    renderTab();
    await screen.findByText("Warehouse is in Leeds");
    // Org B's server list is untouched: the private row is still private, one Confirmed.
    expect(screen.getByRole("button", { name: "Share with team" })).toBeInTheDocument();
    expect(screen.getAllByText("Confirmed")).toHaveLength(1);
  });

  it("refetches once at HOLD_MS after a share and renders the updated server lists", async () => {
    mockPost.mockResolvedValue({ id: "mem-new" });
    renderTab();
    await screen.findByText("I am allergic to nuts");
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      fireEvent.click(screen.getByRole("button", { name: "Share with team" }));
      await waitFor(() => expect(screen.getAllByText("Confirmed")).toHaveLength(2));
      expect(memoryGets()).toBe(1);
      mockGet.mockResolvedValue({
        ...LIST,
        private: [],
        team: [{ id: "mem-new", text: "I am allergic to nuts", createdAt: "2026-10-02T00:00:00+00:00", confirmed: true }, ...LIST.team],
      });
      await vi.advanceTimersByTimeAsync(90_001);
      await waitFor(() => expect(memoryGets()).toBe(2));
      await waitFor(() => expect(screen.getAllByText("I am allergic to nuts")).toHaveLength(1));
      expect(screen.getAllByText("Confirmed")).toHaveLength(2);
      expect(screen.queryByRole("button", { name: "Share with team" })).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders contested badge and history line", async () => {
    mockGet.mockResolvedValue({
      ...LIST,
      team: [
        {
          id: "mem-c", text: "API port is 9090", createdAt: "2026-10-01T00:00:00+00:00", confirmed: false,
          status: "contested", speaker: "Ana",
          history: [{ value: "8080", speaker: "Bo", occurredAt: "2026-09-30T00:00:00+00:00" }],
        },
        {
          ...LIST.team[0], status: "current",
          history: [{ value: "York", speaker: "Cy", occurredAt: "2026-09-01T00:00:00+00:00" }],
        },
      ],
    });
    renderTab();
    expect(await screen.findByText("API port is 9090")).toBeInTheDocument();
    expect(screen.getByText(/Was York/)).toBeInTheDocument(); // a current value's replaced one
    expect(screen.getByText("Contested")).toBeInTheDocument();
    expect(screen.getByText(/Also: 8080/)).toHaveTextContent(/Bo/); // the other side, not an old value
    expect(screen.queryByText(/Was 8080/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Contested only" }));
    expect(screen.getByText("API port is 9090")).toBeInTheDocument();
    expect(screen.queryByText("Warehouse is in Leeds")).not.toBeInTheDocument();
  });

  it("labels a record the overlay is holding as pending", async () => {
    mockPost.mockResolvedValue({ id: "mem-new" });
    renderTab();
    await screen.findByText("I am allergic to nuts");
    fireEvent.click(screen.getByRole("button", { name: "Share with team" }));
    expect(await screen.findByText("Pending")).toBeInTheDocument();
  });

  it("says no contested memories when the filter hides everything", async () => {
    renderTab();
    await screen.findByText("Warehouse is in Leeds");
    fireEvent.click(screen.getByRole("checkbox", { name: "Contested only" }));
    expect(screen.getAllByText("No contested memories")).toHaveLength(2);
    expect(screen.queryByText(/Nothing remembered yet/)).not.toBeInTheDocument();
  });

  it("renders suggested drafts with evidence links", async () => {
    route(DRAFTS);
    renderTab();
    expect(await screen.findByText("Run migrations before the seed")).toBeInTheDocument();
    expect(screen.getByText("Suggested")).toBeInTheDocument();
    expect(screen.getByText(/Lessons drawn from uploaded sessions\./)).toBeInTheDocument();
    expect(screen.getByText("lesson")).toBeInTheDocument();
    expect(screen.getByText("finding")).toBeInTheDocument();
    const link = await screen.findByRole("link", { name: "s1" });
    expect(link.getAttribute("href")).toContain("/chat?panel=session&session=s1");
    expect(screen.getByText("s-gone")).not.toHaveAttribute("href"); // not one of the viewer's sessions
    expect(screen.getAllByRole("button", { name: "Approve" })).toHaveLength(1); // a finding is only dismissed
    expect(screen.getAllByRole("button", { name: "Dismiss" })).toHaveLength(2);
  });

  it("approves a draft, removes it locally and refetches the drafts", async () => {
    route(DRAFTS);
    mockPost.mockResolvedValue({ queued: false });
    renderTab();
    await screen.findByText("Run migrations before the seed");
    route({ drafts: [DRAFTS.drafts[1]] });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/memory/drafts/d1/approve", {}));
    await waitFor(() => expect(screen.queryByText("Run migrations before the seed")).not.toBeInTheDocument());
    expect(screen.getByText("Token in a log line")).toBeInTheDocument();
    expect(mockGet.mock.calls.filter(([u]) => u === "/memory/drafts").length).toBeGreaterThan(1);
  });

  it("dismisses a draft", async () => {
    route(DRAFTS);
    mockPost.mockResolvedValue(undefined);
    renderTab();
    await screen.findByText("Token in a log line");
    fireEvent.click(screen.getAllByRole("button", { name: "Dismiss" })[1]);
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/memory/drafts/d2/dismiss", {}));
    await waitFor(() => expect(screen.queryByText("Token in a log line")).not.toBeInTheDocument());
  });

  it("refetches the drafts and shows the error when a draft was settled elsewhere", async () => {
    route(DRAFTS);
    mockPost.mockRejectedValue(Object.assign(new Error("draft already settled"), { status: 409 }));
    renderTab();
    await screen.findByText("Run migrations before the seed");
    route({ drafts: [DRAFTS.drafts[1]] });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already settled");
    await waitFor(() => expect(screen.queryByText("Run migrations before the seed")).not.toBeInTheDocument());
  });
});
