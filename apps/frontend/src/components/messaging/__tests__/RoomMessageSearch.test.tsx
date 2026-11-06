// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const messaging = vi.hoisted(() => ({ searchMessages: vi.fn(), warmSearch: vi.fn() }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ messaging }) }));
const analytics = vi.hoisted(() => ({ capture: vi.fn() }));
vi.mock("@/lib/analytics", () => analytics);

import { RoomMessageSearch } from "../RoomMessageSearch";

const hit = { messageId: "m1", roomId: "r1", messageSeq: 3, threadRootMessageId: null, authorName: "Ann",
  authorKind: "human", createdAt: "2026-10-04T00:00:00Z", snippet: "the \u0001deploy\u0002 fix" };

afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

async function typeQuery(value: string) {
  fireEvent.click(screen.getByRole("button", { name: "Search this chat" }));
  fireEvent.change(screen.getByRole("searchbox", { name: "Search messages" }), { target: { value } });
  await act(async () => { await vi.advanceTimersByTimeAsync(300); });
}

describe("RoomMessageSearch", () => {
  it("warms on open, searches this room, and opens a hit", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchMessages.mockResolvedValue({ hits: [hit] });
    const onSelect = vi.fn();
    render(<RoomMessageSearch roomId="r1" onSelect={onSelect} />);
    await typeQuery("deploy");
    expect(messaging.warmSearch).toHaveBeenCalledTimes(1);
    expect(messaging.searchMessages).toHaveBeenCalledWith("deploy", { roomId: "r1", limit: 50 });
    expect(screen.getByText("deploy").tagName).toBe("MARK");
    fireEvent.click(screen.getByRole("button", { name: /Ann/ }));
    expect(onSelect).not.toHaveBeenCalled(); // fires on close-auto-focus, like before
  });

  it("says search is starting up on a 503 and retries once", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchMessages.mockRejectedValueOnce({ status: 503 }).mockResolvedValueOnce({ hits: [hit] });
    render(<RoomMessageSearch roomId="r1" onSelect={vi.fn()} />);
    await typeQuery("deploy");
    expect(screen.getByRole("status").textContent).toMatch(/starting up/i);
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(messaging.searchMessages).toHaveBeenCalledTimes(2);
    expect(screen.getByText("deploy").tagName).toBe("MARK");
  });

  it("selects once after the dialog closes and records the result bucket", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchMessages.mockResolvedValue({ hits: [hit] });
    const onSelect = vi.fn();
    render(<RoomMessageSearch roomId="r1" onSelect={onSelect} />);
    await typeQuery("deploy");
    fireEvent.click(screen.getByRole("button", { name: /Ann/ }));
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(hit);
    expect(analytics.capture).toHaveBeenCalledTimes(1);
    expect(analytics.capture).toHaveBeenCalledWith("message_search_used", { result_count_bucket: "1-5" });
  });

  it("does not read live events while closed", () => {
    render(<RoomMessageSearch roomId="r1" onSelect={vi.fn()} />);
    expect(messaging.warmSearch).not.toHaveBeenCalled();
  });
});
