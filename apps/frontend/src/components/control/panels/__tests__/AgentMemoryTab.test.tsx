import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

const mockGet = vi.fn();
const mockDel = vi.fn();
const mockPost = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: mockGet, del: mockDel, post: mockPost }),
}));

import { AgentMemoryTab } from "../AgentMemoryTab";

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
  mockGet.mockReset().mockResolvedValue(LIST);
  mockDel.mockReset().mockResolvedValue(undefined);
  mockPost.mockReset();
});
afterEach(() => vi.restoreAllMocks());

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
    expect(mockGet).toHaveBeenCalledTimes(1);
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
    expect(mockGet).toHaveBeenCalledTimes(1);
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
    expect(await screen.findByText("Showing the newest 200.")).toBeInTheDocument();
  });

  it("shows a failed delete in an alert", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mockDel.mockRejectedValue(new Error("boom"));
    renderTab();
    await screen.findByText("I am allergic to nuts");
    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });
});
