import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FIXTURES, SAM } from "@lightbulb/shared/tasks/fixtures";
import { SWRConfig } from "swr";
import { fakeTasksClient, type FakeTasksClient } from "@/components/tasks/__tests__/fakeTasks";
import type { RoomMessage, RoomParticipant } from "../types";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent, useOptionalLiveEvents: () => live }));
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
// The About dot's complete Needs you read answers from the room's rows; automations have no route here.
const apiGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: apiGet }) }));

import { RoomPanel, type RoomPanelProps } from "../RoomPanel";

const sam: RoomParticipant = { kind: "human", id: SAM, displayName: "Sam", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-11-04T00:00:00Z", joinedSeq: 0 };
const lightbulb: RoomParticipant = { ...sam, kind: "agent", id: "agent_ceo", displayName: "Lightbulb", role: "member" };
const message: RoomMessage = { messageId: "m1", roomId: "r1", messageSeq: 1, roomRevision: 1, author: { kind: "human", id: SAM, displayName: "Sam" }, text: "hello", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-11-04T14:00:00Z", runId: null };
const props: RoomPanelProps = {
  ownerScopeId: "org", currentUserId: SAM, roomId: "r1", state: "ready", httpReady: true, liveConnected: true,
  room: { roomId: "r1", kind: "channel", name: "workshop-launch", roomRevision: 1, lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0, updatedAt: message.createdAt, topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0 },
  participants: [sam, lightbulb], messages: [message],
  composer: { text: "", onTextChange: vi.fn(), members: [sam, lightbulb], clientMessageId: "draft", sendState: "idle", onSubmit: vi.fn() },
  onLinkToWork: vi.fn(),
};
// Default SWR dedupe here: the button and the overview share one room read, as in the app.
const Fresh = ({ children }: { children: React.ReactNode }) => <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>;
let client: FakeTasksClient;
beforeEach(() => {
  live.superAgent = true; client = fakeTasksClient(FIXTURES.step05.tasks); live.tasks = client;
  apiGet.mockReset();
  apiGet.mockImplementation(async (path: string) => {
    if (!path.includes("needsYou=true")) throw new Error("no route");
    // The workspace-wide read; the dot keeps this room's share (audience.roomId).
    return { tasks: FIXTURES.step05.tasks.filter((t) => t.shownGroup === "waiting_on_you" || t.shownGroup === "ready_for_review")
      .map((t) => ({ ...t, audience: { ...t.audience, roomId: "r1" } })), next: null };
  });
});

describe("RoomPanel Threads view (super-agent)", () => {
  it("the About button carries the needs-you dot and opens on the room's requests", async () => {
    render(<Fresh><RoomPanel {...props} /></Fresh>);
    const button = await screen.findByRole("button", { name: "About, 1 needs you" });
    expect(button).toHaveTextContent("About");
    await userEvent.click(button);
    const aside = await screen.findByRole("complementary", { name: "About" });
    expect(within(aside).getByRole("tab", { name: "Requests" })).toHaveAttribute("aria-selected", "true");
    expect(within(aside).getByRole("heading", { name: "Needs you · 1" })).toBeInTheDocument();
    expect(within(aside).getByRole("heading", { name: "Working · 2" })).toBeInTheDocument();
    // One room read serves the button and the tab.
    expect(client.list).toHaveBeenCalledTimes(1);
    // A row opens its thread in the same column.
    await userEvent.click(within(aside).getByRole("button", { name: "Open Workshop signup app, Needs you" }));
    expect(await screen.findByRole("complementary", { name: "Request" })).toHaveAttribute("data-task-pane", "t-signup");
    // Opened from About, Back returns to its Threads tab; from there, Back returns to the conversation.
    await userEvent.click(screen.getByRole("button", { name: "Back to requests" }));
    expect(within(await screen.findByRole("complementary", { name: "About" })).getByRole("tab", { name: "Requests" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("button", { name: "Back to conversation" }));
    expect(screen.getByRole("button", { name: "About, 1 needs you" })).toHaveFocus();
  });

  it("a message sent while the workspace is still being set up reads Queued until Lightbulb answers", async () => {
    const { rerender } = render(<Fresh><RoomPanel {...props} runtimeStarting /></Fresh>);
    expect(await screen.findByText("Queued · Lightbulb answers as soon as setup finishes")).toBeInTheDocument();
    rerender(<Fresh><RoomPanel {...props} runtimeStarting={false} /></Fresh>);
    expect(screen.queryByText(/^Queued/)).toBeNull();
  });

  it("flag off keeps Tasks in the menu, no Threads tab, and makes no /tasks request", async () => {
    live.superAgent = false;
    render(<Fresh><RoomPanel {...props} /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: "About" }));
    expect(within(screen.getByRole("complementary", { name: "About" })).queryByRole("tab", { name: "Requests" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Conversation options" }));
    expect(await screen.findByRole("menuitem", { name: "Tasks" })).toBeInTheDocument();
    expect(client.list).not.toHaveBeenCalled();
  });
});
