import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { mergeSessionRows, type EngineRecord, type LocalSession, type SessionRow } from "@lightbulb/shared";
import { SessionListRow, sessionAge, sessionListTime } from "../SessionListRow";
import type { SidebarRow } from "../roomSections";

const session: LocalSession = { sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2", generation: "g", status: "busy", activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0, title: "Load test" };

const marks = { pinned: false, archived: false, onTogglePin: vi.fn(), onToggleArchive: vi.fn(), onMarkUnread: vi.fn() };
const owner = { kind: "human" as const, id: "alice", displayName: "Alice Able", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 };

const viewer = { userId: "alice", nameOf: () => "Alice" };
/** A backend row, as the web builds it; `viewerId` other than alice makes it a teammate's. */
const rowOf = (backend: LocalSession = session, unread?: SidebarRow["unread"], engine?: EngineRecord): SidebarRow =>
  ({ row: mergeSessionRows([backend], engine ? [{ ...engine, session_id: backend.networkSessionId ?? backend.sessionId }] : [], viewer)[0], owner, unread });
const own = { ...session, humanUserId: "alice" };
const mate = { ...session, humanUserId: "bob" };
const engineRec: EngineRecord = { session_id: "n1", person: "Alice", person_id: "alice", person_verified: true, computer: "Mac", agent: "claude", folder: "web", repo: null,
  branch: null, status: "idle", doing: "", title: null, tier: "t", online: true, wakes: false, user_id: "alice" };
const networkRow = (changes: Partial<SessionRow> = {}, unread?: SidebarRow["unread"]): SidebarRow => ({ owner, unread,
  row: { id: "n1", source: "network", person: { name: "Alice", isViewer: true }, agent: "claude", title: "Friday inbox", folder: "ops", state: "idle", ...changes } });
const ui = (entry: SidebarRow, props: Partial<Parameters<typeof SessionListRow>[0]> = {}) =>
  render(<ul><SessionListRow entry={entry} current={false} {...props} /></ul>);
const ago = (ms: number) => Date.now() - ms;

it("shows a session's unread chat on its row, and nothing when there is none", () => {
  const { rerender } = render(<ul><SessionListRow entry={rowOf({ ...mate, chatUnread: 3 })} current={false} /></ul>);
  expect(screen.getByRole("button", { name: /, 3 unread in chat$/ })).toBeInTheDocument();
  expect(document.querySelector(".session-chat-unread")).toHaveTextContent("3");
  rerender(<ul><SessionListRow entry={rowOf({ ...mate, chatUnread: 0 })} current={false} /></ul>);
  expect(screen.getByRole("button").getAttribute("aria-label")).not.toMatch(/unread in chat/);
  expect(document.querySelector(".session-chat-unread")).toBeNull();
});

it("says Waiting, not Waiting on you, of a teammate's session", () => {
  const { container } = render(<ul><SessionListRow entry={rowOf({ ...mate, status: "waiting-approval", waitingFor: "permission prompt" })} current={false} /></ul>);
  expect(container.querySelector(".session-row-state")).toHaveTextContent(/^Waiting$/);
  expect(screen.getByRole("button", { name: /^Open / }).getAttribute("aria-label")).not.toContain("on you");
});

it("All sessions spells the state out, with its time", () => {
  const { container } = render(<ul><SessionListRow entry={rowOf({ ...mate, cwdName: "web", status: "busy", lastEventAt: new Date().toISOString() })} current={false} /></ul>);
  expect(container).toHaveTextContent("Alice · web");
  expect(container.querySelector(".session-row-state")).toHaveTextContent("Working");
  expect(container.querySelector(".session-row-time")).toBeInTheDocument();
});

it("shows no status words: a pulsing dot while working, an orange dot while waiting, the time otherwise, faded once gone", () => {
  const at = new Date().toISOString();
  const row = (changes: Partial<LocalSession>) => render(<ul><SessionListRow entry={rowOf({ ...mate, cwdName: "web", lastEventAt: at, ...changes })} current={false} oneLine /></ul>);
  const { container, unmount } = row({ status: "busy" });
  expect(container.querySelector(".session-working")).not.toBeNull();
  expect(container).not.toHaveTextContent(/Working|Idle|Online|unavailable/);
  expect(container.querySelector(".session-row-time")).toBeInTheDocument();
  unmount();
  const waiting = row({ status: "waiting-approval" });
  expect(waiting.container.querySelector(".session-unread--waiting")).not.toBeNull();
  expect(waiting.container.querySelector(".session-row-time")).toBeNull();
  waiting.unmount();
  const unknown = row({ status: "unknown" });
  expect(unknown.container.querySelector(".session-working")).toBeNull();
  expect(screen.getByRole("button").getAttribute("aria-label")).toMatch(/, Online$/);
  unknown.unmount();
  const ended = row({ status: "ended" });
  expect(ended.container.querySelector(".session-row--gone")).not.toBeNull();
});

it("tells the time like Happy: a clock today, a weekday this week, then a date", () => {
  const now = new Date(2026, 9, 13, 19, 30).getTime();
  expect(sessionListTime(new Date(2026, 9, 13, 17, 5).toISOString(), now)).toMatch(/5:05/);
  // Yesterday evening is yesterday, not "today" by a rolling 24 hours.
  expect(sessionListTime(new Date(2026, 9, 12, 20, 0).toISOString(), new Date(2026, 9, 13, 8, 0).getTime()))
    .toBe(new Date(2026, 9, 12).toLocaleDateString([], { weekday: "short" }));
  expect(sessionListTime(new Date(2026, 9, 10, 9, 0).toISOString(), now)).toBe(new Date(2026, 9, 10).toLocaleDateString([], { weekday: "short" }));
  expect(sessionListTime(new Date(2026, 8, 1).toISOString(), now)).toBe(new Date(2026, 8, 1).toLocaleDateString([], { month: "numeric", day: "numeric" }));
  expect(sessionListTime(null, now)).toBe("");
});

it("keeps the sidebar row to one line, with owner and folder only in its accessible label", () => {
  const { container } = render(<ul><SessionListRow entry={rowOf({ ...mate, cwdName: "web" }, "unread")} current={false} oneLine /></ul>);
  const row = screen.getByRole("button");
  expect(row).toHaveAccessibleName(/^Open .*Load test, Alice, in web, Working, new output$/);
  expect(row).not.toHaveTextContent("Alice");
  expect(row).not.toHaveTextContent("web");
  expect(container.querySelector(".session-tool-badge")).not.toBeNull();
  expect(container.querySelector(".session-unread--unread")).not.toBeNull();
});

it("shows the owner's face with the tool icon as a corner badge, and the label still names the owner", () => {
  const { container } = render(<ul><SessionListRow entry={rowOf({ ...mate })} current={false} /></ul>);
  const avatar = container.querySelector(".participant-avatar");
  expect(avatar).toHaveTextContent("AA");
  expect(avatar?.parentElement?.querySelector(".session-tool-badge--corner")).not.toBeNull();
  expect(screen.getByRole("button").getAttribute("aria-label")).toMatch(/Alice/);
});

it("keeps sidebar session actions in one compact overflow control", () => {
  render(<ul><SessionListRow entry={rowOf({ ...own, status: "ended" })}
    current={false} oneLine onSelect={() => {}} onRename={vi.fn()} onRemove={vi.fn()}
    marks={marks} /></ul>);

  expect(screen.getByRole("button", { name: "Session actions for Load test" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Rename Load test" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Remove Load test" })).toBeNull();
});

it("tells ages in one unit", () => {
  const now = 10_000_000_000;
  expect([undefined, now - 20_000, now - 5 * 60_000, now - 2 * 3_600_000, now - 3 * 86_400_000].map((at) => sessionAge(at, now))).toEqual(["", "now", "5m", "2h", "3d"]);
});

it("waiting: a brass dot and the reason on a second line", () => {
  const { container } = ui(rowOf({ ...own, status: "waiting-approval", waitingFor: "Run npm test" }, "unread"), { oneLine: true });
  expect(container.querySelector(".session-unread--waiting")).not.toBeNull();
  expect(container.querySelector(".session-unread--unread")).toBeNull();
  expect(container.querySelector(".session-row-time")).toBeNull();
  expect(container).toHaveTextContent("Run npm test");
  expect(screen.getByRole("button").getAttribute("aria-label")).toMatch(/Waiting on you/);
});

it("a failed row never shows an unread dot", () => {
  const { container } = ui(networkRow({ state: "ended", endedAt: ago(60_000), endedReason: "login-session-ended" }, "unread"), { oneLine: true });
  expect(container.querySelector(".session-unread")).toBeNull();
  expect(container.querySelector(".session-row-state--failed")).toHaveTextContent("Failed");
});

it("an unread dot sits beside the state word in the sidebar; only the waiting dot replaces it", () => {
  const { container } = ui(networkRow({ state: "ended", endedAt: ago(60_000) }, "unread"), { oneLine: true });
  expect(container.querySelector(".session-unread--unread")).not.toBeNull();
  expect(container.querySelector(".session-row-state")).toHaveTextContent("Ended");
});

it("idle shows the time and no second line; folder and PR are the second line of a live row with news", () => {
  const { container, unmount } = ui(rowOf({ ...own, status: "idle", cwdName: "web", lastEventAt: new Date().toISOString() }), { oneLine: true });
  expect(container.querySelector(".session-row-time")).toBeInTheDocument();
  expect(container.querySelector(".session-row-main > .text-caption")).toBeNull();
  unmount();
  const news = ui(rowOf({ ...own, status: "idle", cwdName: "web", pr: { number: 4527, state: "open", checks: "passing", url: "https://example.com/pr/4527" } }), { oneLine: true });
  expect(news.container.querySelector(".session-row-main > .text-caption")).toHaveTextContent("web · PR #4527 · checks passing");
});

it("done says Done, and a finished dot sits beside it", () => {
  const done = { ...own, status: "idle" as const, done: true };
  const { container, unmount } = ui(rowOf(done), { oneLine: true });
  expect(container.querySelector(".session-row-state")).toHaveTextContent("Done");
  unmount();
  const dotted = ui(rowOf(done, "finished"), { oneLine: true });
  expect(dotted.container.querySelector(".session-row-state")).toHaveTextContent("Done");
  expect(dotted.container.querySelector(".session-unread--finished")).not.toBeNull();
  dotted.unmount();
  const all = ui(rowOf(done, "finished"));
  expect(all.container.querySelector(".session-row-state")).toHaveTextContent("Done");
  expect(all.container.querySelector(".session-unread--finished")).not.toBeNull();
});

it("offline says Offline with its age, is faded, and the label says last seen", () => {
  const { container } = ui(networkRow({ state: "offline", lastSeenAt: ago(2 * 3_600_000) }));
  expect(container.querySelector(".session-row-state")).toHaveTextContent("Offline 2h");
  expect(container.querySelector(".session-row--gone")).not.toBeNull();
  expect(container).toHaveTextContent("Offline, last seen 2h ago");
});

it("parked says Parked and is not faded", () => {
  const { container } = ui(networkRow({ state: "parked", lastSeenAt: ago(60_000) }));
  expect(container.querySelector(".session-row-state")).toHaveTextContent("Parked");
  expect(container.querySelector(".session-row--gone")).toBeNull();
  expect(container).toHaveTextContent("Parked, wakes when messaged");
});

it("ended says Ended with its age and keeps the reason in the hover", () => {
  const { container } = ui(networkRow({ state: "ended", endedAt: ago(3 * 3_600_000), endedReason: "idle-stopped" }), { oneLine: true });
  expect(container.querySelector(".session-row-state")).toHaveTextContent("Ended 3h");
  expect(container.querySelector(".session-row--gone")).not.toBeNull();
  expect(container.querySelector(".session-row-main")?.getAttribute("title")).toMatch(/stopped while idle/);
  expect(container.querySelector(".session-row-main > .text-caption")).toBeNull();
});

it("failed says Failed in the alert colour class, with its reason on the second line", () => {
  const { container, unmount } = ui(networkRow({ state: "ended", endedAt: ago(2 * 3_600_000), endedReason: "login-session-ended" }), { oneLine: true });
  expect(container.querySelector(".session-row-state--failed")).toHaveTextContent("Failed 2h");
  expect(container.querySelector(".session-row-main > .text-caption")).toHaveTextContent("your Mac's login session restarted");
  unmount();
  const job = ui({ ...networkRow({ state: "ended", endedAt: ago(60_000) }), row: { ...networkRow({ state: "ended", endedAt: ago(60_000) }).row, engine: { ...engineRec, status: "ended", job: { state: "failed" } } } }, { oneLine: true });
  expect(job.container.querySelector(".session-row-main > .text-caption")).toHaveTextContent("Job failed");
});

it("a row with no backend is not a button: no Open, no Mark unread, still Pin and Archive", async () => {
  ui(networkRow(), { marks });
  expect(screen.queryByRole("button", { name: /^Open/ })).toBeNull();
  expect(document.querySelector(".session-row-info")).toHaveTextContent("Friday inbox");
  expect(document.querySelector(".session-row--network")).not.toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Session actions for Friday inbox" }));
  expect(await screen.findByRole("menuitem", { name: "Pin" })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "Archive" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: /Mark unread/ })).toBeNull();
});

it("a network row that can resume says lightbulb who in its hover", () => {
  const { container } = ui(networkRow({ state: "ended", endedAt: ago(60_000), canResume: true }));
  expect(container.querySelector(".session-row-main")?.getAttribute("title")).toMatch(/lightbulb who/);
});

describe("Share on a network row", () => {
  const mine = (changes: Partial<SessionRow> = {}) => networkRow({ engine: { own: true } as EngineRecord, ...changes });

  it("offers Share on this Mac's own live Claude session only", async () => {
    const onShare = vi.fn(() => Promise.resolve());
    const { rerender } = ui(mine(), { onShare });
    await userEvent.click(screen.getByRole("button", { name: /Share/ }));
    expect(onShare).toHaveBeenCalledTimes(1);
    for (const entry of [networkRow(), mine({ state: "ended" }), mine({ agent: "codex" }), networkRow({ engine: { own: false } as EngineRecord })]) {
      rerender(<ul><SessionListRow entry={entry} current={false} onShare={onShare} /></ul>);
      expect(screen.queryByRole("button", { name: /Share/ })).toBeNull();
    }
  });

  it("shows a refusal in the row, and says nothing when the person pressed No", async () => {
    const onShare = vi.fn<() => Promise<unknown>>().mockRejectedValueOnce("sharing is off on this Mac");
    const { container } = ui(mine(), { onShare });
    fireEvent.click(screen.getByRole("button", { name: /Share/ }));
    expect(await screen.findByText("sharing is off on this Mac")).toBeInTheDocument();
    onShare.mockRejectedValueOnce("Not changed: declined on this Mac.");
    fireEvent.click(screen.getByRole("button", { name: /Share/ }));
    await waitFor(() => expect(screen.queryByText("sharing is off on this Mac")).toBeNull());
    await act(async () => {});
    expect(container).not.toHaveTextContent("declined");
  });
});
