import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const messaging = vi.hoisted(() => ({ searchMessages: vi.fn(), warmSearch: vi.fn() }));
const open = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ messaging }), useSuperAgent: () => false }));
vi.mock("@/components/teams/command-palette/useFilteredCommandResults", () => ({
  useFilteredCommandResults: () => ({ agents: [], issues: [], projects: [] }),
}));
vi.mock("@/components/settings/CompanyStripeSection", () => ({ useCompanyStripeStatus: () => ({ data: null }) }));
vi.mock("@/components/messaging/roomCommands", () => ({ useRoomCommandResults: () => [] }));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspaceOptional: () => ({
    directoryProps: { rooms: [{ roomId: "r1", kind: "channel", name: "general", participants: [] }] },
    openNotificationTarget: open,
  }),
}));

import { CommandPalette } from "@/components/teams/command-palette/CommandPalette";

const hit = { messageId: "m1", roomId: "r1", messageSeq: 1, threadRootMessageId: "t1", authorName: "Ann",
  authorKind: "human", createdAt: "2026-10-04T00:00:00Z", snippet: "the \u0001zzq\u0002 fix" };
// The label is split around the highlighted match: find the element whose whole text is the snippet.
const snippetText = (text: string) => (_: string, el: Element | null) =>
  el?.tagName === "SPAN" && el.textContent === text && !Array.from(el.children).some((child) => child.textContent === text);

beforeEach(() => {
  vi.useFakeTimers();
  messaging.warmSearch.mockResolvedValue(undefined);
  messaging.searchMessages.mockResolvedValue({ hits: [hit] });
});
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

async function typeQuery(value: string) {
  render(<CommandPalette open onOpenChange={vi.fn()} />);
  fireEvent.change(screen.getByPlaceholderText(/search messages/i), { target: { value } });
  await act(async () => { await vi.advanceTimersByTimeAsync(300); });
}

describe("palette Messages group", () => {
  test("renders the hit with plain snippet and author · room", async () => {
    await typeQuery("zzq");
    expect(screen.getByText("Messages")).toBeInTheDocument();
    expect(screen.getByText(snippetText("the zzq fix"))).toBeInTheDocument();
    expect(screen.getByText("zzq").tagName).toBe("MARK");
    expect(screen.getByText("Ann · general")).toBeInTheDocument();
  });

  test("click opens the target", async () => {
    await typeQuery("zzq");
    fireEvent.click(screen.getByText(snippetText("the zzq fix")));
    expect(open).toHaveBeenCalledWith("r1", "t1", "m1");
  });

  test("Enter on the selected hit opens the target", async () => {
    await typeQuery("zzq");
    fireEvent.keyDown(screen.getByPlaceholderText(/search messages/i), { key: "Enter" });
    expect(open).toHaveBeenCalledWith("r1", "t1", "m1");
  });

  test("503 with no other match says search is starting up", async () => {
    messaging.searchMessages.mockRejectedValue({ status: 503 });
    await typeQuery("zzq");
    expect(screen.getByText("Search is starting up…")).toBeInTheDocument();
    expect(screen.queryByText("No results.")).not.toBeInTheDocument();
  });
});
