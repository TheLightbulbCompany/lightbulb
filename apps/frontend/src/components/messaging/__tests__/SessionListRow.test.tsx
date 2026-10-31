import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { SessionListRow, sessionListTime } from "../SessionListRow";

const session: LocalSession = { sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2", generation: "g", status: "busy", activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0, title: "Load test" };

const owner = { kind: "human" as const, id: "alice", displayName: "Alice Able", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 };

it("shows a session's unread chat on its row, and nothing when there is none", () => {
  const { rerender } = render(<ul><SessionListRow entry={{ session: { ...session, chatUnread: 3 }, ownerName: "Alice", own: false, owner }} current={false} /></ul>);
  expect(screen.getByRole("button", { name: /, 3 unread in chat$/ })).toBeInTheDocument();
  expect(document.querySelector(".session-chat-unread")).toHaveTextContent("3");
  rerender(<ul><SessionListRow entry={{ session: { ...session, chatUnread: 0 }, ownerName: "Alice", own: false, owner }} current={false} /></ul>);
  expect(screen.getByRole("button").getAttribute("aria-label")).not.toMatch(/unread in chat/);
  expect(document.querySelector(".session-chat-unread")).toBeNull();
});

it("shows no status words: a shimmering title while working, an orange dot while waiting, the time otherwise, faded once gone", () => {
  const at = new Date().toISOString();
  const row = (changes: Partial<LocalSession>) => render(<ul><SessionListRow entry={{ session: { ...session, cwdName: "web", lastEventAt: at, ...changes }, ownerName: "Alice", own: false, owner }} current={false} /></ul>);
  const { container, unmount } = row({ status: "busy" });
  expect(container.querySelector(".session-title--working")).toHaveTextContent("Load test");
  expect(container).toHaveTextContent("Alice · web");
  expect(container).not.toHaveTextContent(/Working|Idle|Online|unavailable/);
  expect(container.querySelector(".session-row-time")).toBeInTheDocument();
  unmount();
  const waiting = row({ status: "waiting-approval" });
  expect(waiting.container.querySelector(".session-unread--waiting")).not.toBeNull();
  expect(waiting.container.querySelector(".session-row-time")).toBeNull();
  waiting.unmount();
  const unknown = row({ status: "unknown" });
  expect(unknown.container.querySelector(".session-title--working")).toBeNull();
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
  const { container } = render(<ul><SessionListRow entry={{ session: { ...session, cwdName: "web" }, ownerName: "Alice", own: false, unread: "unread", owner }} current={false} oneLine /></ul>);
  const row = screen.getByRole("button");
  expect(row).toHaveAccessibleName(/^Open .*Load test, Alice, in web, Working, new output$/);
  expect(row).not.toHaveTextContent("Alice");
  expect(row).not.toHaveTextContent("web");
  expect(container.querySelector(".session-tool-badge")).not.toBeNull();
  expect(container.querySelector(".session-unread--unread")).not.toBeNull();
});

it("shows the owner's face with the tool icon as a corner badge, and the label still names the owner", () => {
  const { container } = render(<ul><SessionListRow entry={{ session, ownerName: "Alice Able", own: false, owner }} current={false} /></ul>);
  const avatar = container.querySelector(".participant-avatar");
  expect(avatar).toHaveTextContent("AA");
  expect(avatar?.parentElement?.querySelector(".session-tool-badge--corner")).not.toBeNull();
  expect(screen.getByRole("button").getAttribute("aria-label")).toMatch(/Alice Able/);
});

it("keeps sidebar session actions in one compact overflow control", () => {
  render(<ul><SessionListRow entry={{ session: { ...session, status: "ended" }, ownerName: "Alice", own: true, owner }}
    current={false} oneLine onSelect={() => {}} onRename={vi.fn()} onRemove={vi.fn()}
    marks={{ pinned: false, archived: false, onTogglePin: vi.fn(), onToggleArchive: vi.fn(), onMarkUnread: vi.fn() }} /></ul>);

  expect(screen.getByRole("button", { name: "Session actions for Load test" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Rename Load test" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Remove Load test" })).toBeNull();
});
