// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const messaging = vi.hoisted(() => ({ searchSessions: vi.fn(), warmSearch: vi.fn() }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ messaging }) }));
const nav = vi.hoisted(() => ({ openSessionPanel: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", () => nav);
vi.mock("../MessagingWorkspaceContext", () => ({
  useMessagingWorkspaceOptional: () => ({ resolveSessionOwner: (id: string) => ({ ownerName: id === "u_ann" ? "Ann" : "Bo", own: false }) }),
}));

import { SessionSearch } from "../SessionSearch";

const prompt = { sessionId: "s1", seq: 7, authorKind: "person", authorId: "u_ann", authorName: "", createdAt: "2026-10-26T00:00:00Z",
  snippet: "why does \u0001deploy\u0002 fail", sessionTitle: "Fix deploy", provider: "claude", humanUserId: "u_ann" };
const reply = { ...prompt, seq: 8, authorKind: "agent", authorId: "", authorName: "Claude Code", snippet: "the \u0001deploy\u0002 key expired" };

afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

describe("SessionSearch", () => {
  it("searches session text, names who said it, and opens the hit's turn", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchSessions.mockResolvedValue({ hits: [prompt, reply] });
    render(<SessionSearch />);
    fireEvent.click(screen.getByRole("button", { name: "Search sessions" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Search session text" }), { target: { value: "deploy" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(messaging.warmSearch).toHaveBeenCalledTimes(1);
    expect(messaging.searchSessions).toHaveBeenCalledWith("deploy", { limit: 50 });
    expect(screen.getByText("2 turns found")).toBeTruthy();
    expect(screen.getAllByText("deploy")[0].tagName).toBe("MARK");
    expect(screen.getByRole("button", { name: /Claude Code/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Ann/ }));
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(nav.openSessionPanel).toHaveBeenCalledWith("s1", 7);
  });

  it("says when nothing matches", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchSessions.mockResolvedValue({ hits: [] });
    render(<SessionSearch />);
    fireEvent.click(screen.getByRole("button", { name: "Search sessions" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Search session text" }), { target: { value: "nothing" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(screen.getByText(/Nothing matches/)).toBeTruthy();
  });
});
