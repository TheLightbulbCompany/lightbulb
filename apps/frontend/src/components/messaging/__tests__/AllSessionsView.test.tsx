import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { AllSessionsView } from "../AllSessionsView";

const make = (sessionId: string, title: string, status: "busy" | "offline" | "ended", hoursAgo: number) => ({
  session: { sessionId, deviceId: "d", humanUserId: "alice", provider: "claude" as const, providerVersion: "2", generation: "g", status, activeTurnId: null,
    capabilities: { sendMode: "queue" as const, steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 3, title,
    lastEventAt: new Date(Date.now() - hoursAgo * 3600_000).toISOString() },
  ownerName: "Alice", own: false,
});

it("groups every session by day, newest first, searches, and opens one", async () => {
  const onOpenSession = vi.fn();
  render(<AllSessionsView loading={false} onOpenSession={onOpenSession} onClose={vi.fn()} entries={[
    make("old", "Billing audit", "offline", 24 * 20),
    make("now", "Fix relay", "busy", 0),
    make("ended", "Ship docs", "ended", 24 * 10),
  ]} />);
  const view = screen.getByRole("region", { name: "All sessions" });
  expect(within(view).getByRole("region", { name: "Today" })).toHaveTextContent("Fix relay");
  expect(within(within(view).getByRole("region", { name: "Earlier" })).getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual([
    expect.stringMatching(/Ship docs, Alice, Ended/), expect.stringMatching(/Billing audit, Alice, Offline/)]);
  await userEvent.type(within(view).getByRole("searchbox", { name: "Search sessions" }), "billing");
  expect(within(view).getAllByRole("button", { name: /^Open / })).toHaveLength(1);
  await userEvent.click(within(view).getByRole("button", { name: /Billing audit/ }));
  expect(onOpenSession).toHaveBeenCalledWith("old");
  await userEvent.clear(within(view).getByRole("searchbox", { name: "Search sessions" }));
  await userEvent.type(within(view).getByRole("searchbox", { name: "Search sessions" }), "zzz");
  expect(within(view).getByText("No sessions match.")).toBeInTheDocument();
});
