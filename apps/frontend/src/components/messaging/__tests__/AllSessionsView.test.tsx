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

it("offers Remove on the viewer's own ended sessions only, one by one or all at once", async () => {
  const onRemove = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined);
  const onRemoveEnded = vi.fn().mockResolvedValue(undefined);
  const mine = (id: string, title: string, status: "busy" | "ended", hoursAgo = 1) => ({ ...make(id, title, status, hoursAgo), own: true, ownerName: "You" });
  render(<AllSessionsView loading={false} onOpenSession={vi.fn()} onClose={vi.fn()} onRemove={onRemove} onRemoveEnded={onRemoveEnded} entries={[
    mine("a", "Old test", "ended"), mine("b", "Older test", "ended", 3), mine("live", "Running", "busy"), make("theirs", "Alice's", "ended", 2),
  ]} />);
  expect(screen.getAllByRole("button", { name: /Remove .* from the list/ }).map((b) => b.getAttribute("aria-label"))).toEqual([
    "Remove Old test from the list", "Remove Older test from the list"]);
  // A running one of the viewer's own: removing it ends it, and the confirmation says so (owner ruling 2026-10-08).
  await userEvent.click(screen.getByRole("button", { name: "End and remove Running" }));
  const ending = screen.getByRole("dialog", { name: "End and remove this session?" });
  expect(ending).toHaveTextContent("It stops on your Mac");
  await userEvent.click(within(ending).getByRole("button", { name: "Cancel" }));
  expect(onRemove).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Remove Old test from the list" }));
  const confirm = screen.getByRole("dialog", { name: "Remove this session from the list?" });
  await userEvent.click(within(confirm).getByRole("button", { name: "Remove" }));
  expect(onRemove).toHaveBeenCalledWith("a");
  // A failure keeps the confirmation open and says so; a second try goes through.
  expect(await within(confirm).findByRole("alert")).toHaveTextContent("Could not remove it. Try again.");
  await userEvent.click(within(confirm).getByRole("button", { name: "Remove" }));
  expect(onRemove).toHaveBeenCalledTimes(2);
  await userEvent.click(screen.getByRole("button", { name: "Remove my ended sessions" }));
  await userEvent.click(within(screen.getByRole("dialog", { name: "Remove your ended sessions?" })).getByRole("button", { name: "Remove" }));
  expect(onRemoveEnded).toHaveBeenCalledTimes(1);
});
