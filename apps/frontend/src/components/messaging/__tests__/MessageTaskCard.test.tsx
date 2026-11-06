import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";
import type { Issue } from "@/components/teams/shared/types";
import { MessageTaskCard, taskIdentifiers } from "../MessageTaskCard";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";
import { RoomTasks } from "../RoomTasks";
import type { RoomMessage, RoomParticipant } from "../types";

vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));

const teams = vi.hoisted(() => ({
  issues: undefined as Issue[] | undefined,
  error: undefined as Error | undefined,
  paths: [] as (string | null)[],
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => {
      teams.paths.push(path);
      if (path === "/issues?excludeRoutineExecutions=true") {
        return { data: teams.issues && { issues: teams.issues }, error: teams.error };
      }
      if (path === "/agents") return { data: { agents: [{ id: "a-fin", name: "Finance" }] } };
      return { data: undefined };
    },
  }),
}));
const live = vi.hoisted(() => ({ runs: [] as unknown[] }));
vi.mock("@/components/teams/dashboard/hooks/useLiveRuns", async (orig) => ({
  ...(await orig<typeof import("@/components/teams/dashboard/hooks/useLiveRuns")>()),
  useLiveRuns: () => ({ runs: live.runs, isLoading: false, isError: false }),
}));

const issue = (n: number, extra: Partial<Issue> = {}): Issue => ({
  id: `id-${n}`, identifier: `ISO-${n}`, title: `Task ${n}`, status: "todo", ...extra,
});

beforeEach(() => {
  teams.issues = [issue(14, { status: "in_progress", assigneeAgentId: "a-fin" }), issue(15), issue(16), issue(17)];
  teams.error = undefined;
  teams.paths = [];
  live.runs = [];
});

describe("taskIdentifiers", () => {
  it("finds every identifier once, in order", () => {
    expect(taskIdentifiers("Put ISO-14 and ENG2-7 on it; ISO-14 again.")).toEqual(["ISO-14", "ENG2-7"]);
  });
  it("ignores URLs, code, lowercase and embedded tokens", () => {
    expect(taskIdentifiers(
      "see https://x.dev/ISO-1 and `ISO-2`\n```\nISO-3\n```\nutf-8 abcISO-4 [ISO-5](https://x.dev/ISO-6)",
    )).toEqual(["ISO-5"]);
  });
});

describe("MessageTaskCard", () => {
  it("renders rows from the shared issues list with assignee and status", () => {
    render(<MessageTaskCard text="Finance has ISO-14." />);
    const row = screen.getByRole("link", { name: /Task 14/ });
    expect(row).toHaveTextContent("ISO-14");
    expect(row).toHaveTextContent("Finance");
    expect(row).toHaveAttribute("href", "/chat?panel=teams-issue-detail&detailId=id-14");
    expect(teams.paths).toContain("/issues?excludeRoutineExecutions=true");
  });

  it("shows a live dot for a task with an active run", () => {
    live.runs = [{ id: "r", issueId: "id-14", status: "running" }];
    render(<MessageTaskCard text="ISO-14" />);
    expect(screen.getAllByRole("img", { name: "Running now" }).length).toBeGreaterThan(0);
  });

  it("caps at three rows and links the rest to Tasks", () => {
    render(<MessageTaskCard text="ISO-14 ISO-15 ISO-16 ISO-17 ISO-99" />);
    expect(screen.getAllByRole("link", { name: /Task \d+/ })).toHaveLength(3);
    expect(screen.getByRole("link", { name: "+1 more" })).toHaveAttribute("href", "/chat?panel=teams-issues");
  });

  it("gives a needs-you row exactly one action that opens the task", () => {
    teams.issues = [issue(14, { status: "in_review" }), issue(15, { status: "blocked" }), issue(16)];
    const openDetail = vi.fn();
    render(<InlineTeamsNavProvider value={{ openDetail, leaveToList: vi.fn() }}>
      <MessageTaskCard text="ISO-14 ISO-15 ISO-16" />
    </InlineTeamsNavProvider>);
    const [review, unblock] = [screen.getByRole("button", { name: "Review ISO-14" }), screen.getByRole("button", { name: "Unblock ISO-15" })];
    expect(screen.getAllByRole("button", { name: /^(Review|Unblock) / })).toHaveLength(2);
    fireEvent.click(review);
    expect(openDetail).toHaveBeenLastCalledWith("issue-detail", "id-14");
    fireEvent.click(unblock);
    expect(openDetail).toHaveBeenLastCalledWith("issue-detail", "id-15");
    fireEvent.click(screen.getByRole("link", { name: /Task 16/ }));
    expect(openDetail).toHaveBeenLastCalledWith("issue-detail", "id-16");
  });

  it("renders nothing for unknown ids, a failed read, or no data yet", () => {
    const { container, rerender } = render(<MessageTaskCard text="ISO-99" />);
    expect(container).toBeEmptyDOMElement();
    teams.error = new Error("no company");
    teams.issues = undefined;
    rerender(<MessageTaskCard text="ISO-14" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("does not read Teams when the text names no task", () => {
    render(<MessageTaskCard text="no tasks here" />);
    expect(teams.paths.every((p) => p === null)).toBe(true);
  });
});

describe("RoomPanel", () => {
  const owner: RoomParticipant = { kind: "human", id: "owner", displayName: "Owner", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 };
  const agent: RoomParticipant = { ...owner, kind: "agent", id: "agent", displayName: "Agent", role: "member" };
  const base: RoomMessage = { messageId: "m1", roomId: "room", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "agent", displayName: "Agent" }, text: "Filed ISO-14", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null };
  const human: RoomMessage = { ...base, messageId: "m2", messageSeq: 2, author: { kind: "human", id: "owner", displayName: "Owner" }, text: "What about ISO-15?" };
  const props: RoomPanelProps = {
    ownerScopeId: "org", currentUserId: "owner", roomId: "room", state: "ready", httpReady: true, liveConnected: true,
    room: { roomId: "room", kind: "dm", name: null, roomRevision: 1, lastMessageSeq: 2, lastMessageAt: null, unreadCount: 0, updatedAt: base.createdAt, topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0 },
    participants: [owner, agent], messages: [base, human],
    composer: { text: "", onTextChange: vi.fn(), members: [owner, agent], clientMessageId: "draft", sendState: "idle", onSubmit: vi.fn() },
  };

  it("shows the card under agent messages only", () => {
    render(<RoomPanel {...props} />);
    const agentMsg = screen.getByRole("article", { name: "Message from Agent" });
    expect(within(agentMsg).getByRole("link", { name: /Task 14/ })).toBeInTheDocument();
    const humanMsg = screen.getByRole("article", { name: "Message from Owner" });
    expect(within(humanMsg).queryByRole("link", { name: /Task 15/ })).not.toBeInTheDocument();
  });

  describe("Tasks tab", () => {
    const withTasks = { ...props, onLinkToWork: vi.fn() };
    // The off-switch Tasks view sits in the header's ··· menu, its count in the item's name.
    const openMenu = () => userEvent.click(screen.getByRole("button", { name: "Options" }));
    const openTab = async () => { await openMenu(); await userEvent.click(await screen.findByRole("menuitem", { name: /^Tasks/ })); };

    it("counts tasks named by agent and human messages, ignoring unknown ids", async () => {
      render(<RoomPanel {...withTasks} messages={[base, { ...human, text: "What about ISO-15 and ISO-99?" }]} />);
      await openMenu();
      expect(await screen.findByRole("menuitem", { name: "Tasks · 2" })).toBeInTheDocument();
      await userEvent.keyboard("{Escape}");
      await openTab();
      const panel = screen.getByRole("complementary", { name: "Tasks" });
      expect(within(panel).getByRole("link", { name: /Task 14/ })).toBeInTheDocument();
      expect(within(panel).getByRole("link", { name: /Task 15/ })).toBeInTheDocument();
      expect(within(panel).queryByText("ISO-99")).not.toBeInTheDocument();
    });

    it("groups by what each task needs, with the needs-you action", async () => {
      teams.issues = [issue(1, { status: "in_review" }), issue(2, { status: "in_progress" }), issue(3, { status: "backlog" }), issue(4, { status: "done" })];
      render(<RoomPanel {...withTasks} messages={[{ ...base, text: "ISO-4 ISO-3 ISO-2 ISO-1" }]} />);
      await openTab();
      const panel = screen.getByRole("complementary", { name: "Tasks" });
      expect(within(panel).getAllByRole("heading", { level: 4 }).map((h) => h.textContent)).toEqual(["Needs you", "In progress", "Up next", "Done"]);
      for (const [group, n] of [["Needs you", 1], ["In progress", 2], ["Up next", 3], ["Done", 4]] as const) {
        expect(within(screen.getByRole("list", { name: group })).getByRole("link", { name: new RegExp(`Task ${n}`) })).toBeInTheDocument();
      }
      expect(within(panel).getAllByRole("button", { name: /^(Review|Unblock) / }).map((b) => b.textContent)).toEqual(["Review"]);
    });

    it("shows the empty state without a count when nothing matches or Teams is unavailable", async () => {
      teams.issues = undefined;
      teams.error = new Error("no company");
      render(<RoomPanel {...withTasks} />);
      await openMenu();
      expect(await screen.findByRole("menuitem", { name: "Tasks" })).toBeInTheDocument();
      await userEvent.keyboard("{Escape}");
      await openTab();
      expect(screen.getByText("No tasks mentioned in this room yet.")).toBeInTheDocument();
    });

    it("opens the global Tasks list from the footer", async () => {
      const onLinkToWork = vi.fn();
      render(<RoomPanel {...props} onLinkToWork={onLinkToWork} />);
      await openTab();
      fireEvent.click(screen.getByRole("button", { name: /All tasks in Tasks/ }));
      expect(onLinkToWork).toHaveBeenCalledOnce();
    });
  });
});

describe("RoomTasks grouping", () => {
  it("files every finished status under Done and open under Up next", () => {
    const tasks = [
      issue(1, { status: "closed" }), issue(2, { status: "won_t_do" }), issue(3, { status: "duplicate" }),
      issue(4, { status: "open" }),
    ];
    teams.issues = tasks;
    render(<RoomTasks tasks={tasks} onOpenAll={vi.fn()} />);
    expect(screen.getByRole("list", { name: "Done" })).toHaveTextContent(/Task 1.*Task 2.*Task 3/);
    expect(screen.getByRole("list", { name: "Up next" })).toHaveTextContent("Task 4");
    expect(screen.queryByRole("list", { name: "In progress" })).not.toBeInTheDocument();
  });
});
