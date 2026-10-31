import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { mergeSessionRows, type EngineRecord, type LocalSession } from "@lightbulb/shared";
import type { SidebarRow } from "../roomSections";
import { AllSessionsView } from "../AllSessionsView";

const viewer = { userId: "me", nameOf: (id: string) => (id === "me" ? "You" : "Alice") };
const person = { kind: "human" as const, id: "alice", displayName: "Alice", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 };
const make = (sessionId: string, title: string, status: "busy" | "offline" | "ended", hoursAgo: number, extra: Partial<LocalSession> = {}): SidebarRow => ({
  row: mergeSessionRows([{ sessionId, deviceId: "d", humanUserId: "alice", provider: "claude", providerVersion: "2", generation: "g", status, activeTurnId: null,
    capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 3, title,
    lastEventAt: new Date(Date.now() - hoursAgo * 3600_000).toISOString(), ...extra }], [], viewer)[0],
  owner: person,
});
const mine = (id: string, title: string, status: "busy" | "ended", hoursAgo = 1) => make(id, title, status, hoursAgo, { humanUserId: "me" });
const network: SidebarRow = { owner: person, row: { id: "n9", source: "network", person: { name: "Alice", isViewer: false }, agent: "claude", title: "Friday inbox",
  folder: "ops", repo: "acme/web", computer: "Alice's Mac", state: "idle" } };

it("lists live sessions first, newest first; ended ones under a collapsed Ended, by day; searches everything", async () => {
  const onOpenSession = vi.fn();
  render(<AllSessionsView loading={false} onOpenSession={onOpenSession} onClose={vi.fn()} entries={[
    make("lost", "Lost weeks ago", "offline", 24 * 20),
    make("older", "Older run", "busy", 2),
    make("now", "Fix relay", "busy", 0),
    make("ended", "Ship docs", "ended", 24 * 10),
    make("gone", "Billing audit", "offline", 0), // 0 h: cannot cross midnight into "Yesterday"
  ]} />);
  const view = screen.getByRole("region", { name: "All sessions" });
  expect(within(within(view).getByRole("list", { name: "Live" })).getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual([
    expect.stringMatching(/Fix relay, Alice, Working/), expect.stringMatching(/Older run, Alice, Working/)]);
  // Offline for days: its Mac lost it, so it is not listed at all.
  expect(within(view).queryByText("Lost weeks ago")).toBeNull();
  const toggle = within(view).getByRole("button", { name: "Ended 2" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(within(view).queryByText("Ship docs")).toBeNull();
  await userEvent.click(toggle);
  expect(within(view).getByRole("region", { name: "Today" })).toHaveTextContent("Billing audit");
  expect(within(view).getByRole("region", { name: "Earlier" })).toHaveTextContent("Ship docs");
  await userEvent.click(toggle);
  // A search looks in Ended too.
  await userEvent.type(within(view).getByRole("searchbox", { name: "Search sessions" }), "ship");
  expect(within(view).getAllByRole("button", { name: /^Open / })).toHaveLength(1);
  await userEvent.click(within(view).getByRole("button", { name: /Ship docs/ }));
  expect(onOpenSession).toHaveBeenCalledWith("ended");
  await userEvent.clear(within(view).getByRole("searchbox", { name: "Search sessions" }));
  await userEvent.type(within(view).getByRole("searchbox", { name: "Search sessions" }), "zzz");
  expect(within(view).getByText("No sessions match.")).toBeInTheDocument();
});

it("offers Remove on the viewer's own ended sessions only, one by one or all at once", async () => {
  const onRemove = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined);
  const onRemoveEnded = vi.fn().mockResolvedValue(undefined);
  render(<AllSessionsView loading={false} onOpenSession={vi.fn()} onClose={vi.fn()} onRemove={onRemove} onRemoveEnded={onRemoveEnded} entries={[
    mine("a", "Old test", "ended"), mine("b", "Older test", "ended", 3), mine("live", "Running", "busy"), make("theirs", "Alice's", "ended", 2),
  ]} />);
  await userEvent.click(screen.getByRole("button", { name: /^Ended/ }));
  expect(screen.getAllByRole("button", { name: /Remove .* from the list/ }).map((b) => b.getAttribute("aria-label"))).toEqual([
    "Remove Old test from the list", "Remove Older test from the list"]);
  // A running one of the viewer's own: removing it ends it, and the confirmation says so (owner ruling 2026-10-08).
  await userEvent.click(screen.getByRole("button", { name: "End and remove Running" }));
  const ending = screen.getByRole("dialog", { name: "End and remove “Running”?" });
  expect(ending).toHaveTextContent("It stops on your Mac");
  await userEvent.click(within(ending).getByRole("button", { name: "Cancel" }));
  expect(onRemove).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Remove Old test from the list" }));
  const confirm = screen.getByRole("dialog", { name: "Remove “Old test” from the list?" });
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

it("lists a network row with its computer and repo, inert; finds a PR by number and opens Ended for it", async () => {
  const onRemove = vi.fn();
  render(<AllSessionsView loading={false} onOpenSession={vi.fn()} onClose={vi.fn()} onRemove={onRemove} entries={[
    network,
    make("pr", "Fix relay", "ended", 24 * 10, { pr: { number: 4527, state: "open", url: "https://x/4527" } } as Partial<LocalSession>),
  ]} />);
  const view = screen.getByRole("region", { name: "All sessions" });
  const live = within(view).getByRole("list", { name: "Live" });
  expect(live).toHaveTextContent("Friday inbox");
  expect(live).toHaveTextContent("Alice's Mac");
  expect(live).toHaveTextContent("acme/web");
  expect(within(live).queryByRole("button")).toBeNull();
  expect(within(view).queryByRole("button", { name: /Remove/ })).toBeNull();
  expect(within(view).queryByText("Fix relay")).toBeNull();
  await userEvent.type(within(view).getByRole("searchbox", { name: "Search sessions" }), "#4527");
  expect(within(view).getByRole("button", { name: /Fix relay/ })).toBeInTheDocument();
});

it("lists an offline row older than 3 days when the engine still knows it", () => {
  const engine = { session_id: "lost", status: "idle", online: false, wakes: false } as EngineRecord;
  const kept = make("lost", "Kept offline", "offline", 24 * 20, { networkSessionId: "lost" });
  render(<AllSessionsView loading={false} onOpenSession={vi.fn()} onClose={vi.fn()} entries={[
    { ...kept, row: { ...kept.row, engine } }, make("lost2", "Dropped offline", "offline", 24 * 20)]} />);
  expect(screen.getByRole("button", { name: "Ended 1" })).toBeInTheDocument();
  expect(screen.queryByText("Dropped offline")).toBeNull();
});
