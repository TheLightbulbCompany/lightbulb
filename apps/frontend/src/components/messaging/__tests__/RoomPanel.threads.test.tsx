import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FIXTURES, SAM } from "@lightbulb/shared/tasks/fixtures";
import { SWRConfig } from "swr";
import { fakeTasksClient, type FakeTasksClient } from "@/components/tasks/__tests__/fakeTasks";
import type { RoomMessage, RoomParticipant } from "../types";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown, onEvent: () => () => undefined }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent, useOptionalLiveEvents: () => live }));
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
// The About dot's complete Needs you read answers from the room's rows; automations have no route here.
const apiGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: apiGet }) }));

import { RoomPanel, namesAgent, type RoomPanelProps } from "../RoomPanel";
import { act } from "@testing-library/react";
import { _resetControllerForTests, attachShell, closePane, getWorkspaceSnapshot, openBrowserPane, openPanel, setThreadPaneHost } from "@/components/workspace/workspaceController";
vi.mock("@/lib/shellNav", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()), pushSearchParams: vi.fn(), replaceSearchParams: vi.fn(),
}));

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
  it("the Requests column is open beside the conversation; a row opens the request there, Back returns, Close is remembered", async () => {
    localStorage.clear();
    const { unmount } = render(<Fresh><RoomPanel {...props} /></Fresh>);
    // Open by default, with the toggle pressed and carrying the needs-you dot.
    const toggle = await screen.findByRole("button", { name: "Requests, 1 needs you" });
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    const aside = await screen.findByRole("complementary", { name: "Requests" });
    expect(await within(aside).findByRole("heading", { name: "Needs you · 1" })).toBeInTheDocument();
    expect(within(aside).getByRole("heading", { name: "Working · 2" })).toBeInTheDocument();
    // No second tab strip in the column: Scheduled lives in the ··· menu.
    expect(within(aside).queryByRole("tab")).toBeNull();
    // One room read serves the button and the column.
    expect(client.list).toHaveBeenCalledTimes(1);
    // A row opens the request in the same column; Back returns to the list.
    await userEvent.click(within(aside).getByRole("button", { name: "Open Workshop signup app, Needs you" }));
    expect(await screen.findByRole("complementary", { name: "Request" })).toHaveAttribute("data-task-pane", "t-signup");
    expect(screen.queryByRole("complementary", { name: "Requests" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Back to requests" }));
    const back = await screen.findByRole("complementary", { name: "Requests" });
    // Closed, it stays closed for this person, in this room and the next.
    await userEvent.click(within(back).getByRole("button", { name: "Close requests" }));
    expect(screen.queryByRole("complementary", { name: "Requests" })).toBeNull();
    expect(screen.getByRole("button", { name: "Requests, 1 needs you" })).toHaveAttribute("aria-pressed", "false");
    unmount();
    render(<Fresh><RoomPanel {...props} /></Fresh>);
    await screen.findByRole("button", { name: "Requests, 1 needs you" });
    expect(screen.queryByRole("complementary", { name: "Requests" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Requests, 1 needs you" }));
    expect(await screen.findByRole("complementary", { name: "Requests" })).toBeInTheDocument();
    localStorage.clear();
  });

  it("a request card in the conversation opens in the column, not a separate pane", async () => {
    localStorage.clear();
    const reply: RoomMessage = { ...message, messageId: "m2", messageSeq: 2, author: { kind: "agent", id: lightbulb.id, displayName: "Lightbulb" }, text: "On it.", taskRefs: [{ taskId: "t-signup" }] } as RoomMessage;
    render(<Fresh><RoomPanel {...props} messages={[message, reply]} /></Fresh>);
    const card = (await screen.findAllByRole("button", { name: "Open Workshop signup app, Needs you" })).find((b) => b.closest("[data-task-card]"))!;
    await userEvent.click(card);
    expect(await screen.findByRole("complementary", { name: "Request" })).toHaveAttribute("data-task-pane", "t-signup");
    expect(screen.getByRole("button", { name: "Back to requests" })).toBeInTheDocument();
  });

  it("in the shell the column takes the right pane, folds while Browser has it, and comes back when Browser closes", async () => {
    localStorage.clear();
    _resetControllerForTests();
    attachShell(() => undefined);
    const host = document.createElement("div");
    document.body.appendChild(host);
    act(() => setThreadPaneHost(host));
    try {
      render(<Fresh><RoomPanel {...props} /></Fresh>);
      expect(await within(host).findByRole("complementary", { name: "Requests" })).toBeInTheDocument();
      expect(getWorkspaceSnapshot().pane?.component).toBe("thread");
      act(() => { openBrowserPane(); });
      expect(getWorkspaceSnapshot().pane?.panelId).toBe("browser");
      expect(screen.queryByRole("complementary", { name: "Requests" })).toBeNull();
      act(() => closePane());
      expect(await within(host).findByRole("complementary", { name: "Requests" })).toBeInTheDocument();
    } finally { host.remove(); _resetControllerForTests(); localStorage.clear(); }
  });

  it("namesAgent: the name as a whole word, any case", () => {
    expect(namesAgent("hi lightbulb, help", "Lightbulb")).toBe(true);
    expect(namesAgent("Lightbulb?", "Lightbulb")).toBe(true);
    expect(namesAgent("lightbulbs", "Lightbulb")).toBe(false);
  });

  it("Escape puts the column away in this room only; the X closes it everywhere; focus returns to the toggle", async () => {
    localStorage.clear();
    const { unmount } = render(<Fresh><RoomPanel {...props} /></Fresh>);
    const aside = await screen.findByRole("complementary", { name: "Requests" });
    await userEvent.click(within(aside).getByRole("button", { name: "Close requests" }));
    expect(screen.getByRole("button", { name: /^Requests/ })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: /^Requests/ }));
    const again = await screen.findByRole("complementary", { name: "Requests" });
    within(again).getByRole("button", { name: "Close requests" }).focus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("complementary", { name: "Requests" })).toBeNull();
    expect(screen.getByRole("button", { name: /^Requests/ })).toHaveFocus();
    unmount();
    // Escape remembered nothing: the next visit (this room or any other) opens with the column.
    expect(localStorage.getItem(`lightbulb.room.requestsOpen:${SAM}`)).toBe("1");
    render(<Fresh><RoomPanel {...props} /></Fresh>);
    expect(await screen.findByRole("complementary", { name: "Requests" })).toBeInTheDocument();
    localStorage.clear();
  });

  it("closing About returns focus to the Requests toggle", async () => {
    localStorage.clear();
    render(<Fresh><RoomPanel {...props} /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: /^People/ }));
    const about = await screen.findByRole("complementary", { name: "About" });
    await userEvent.click(within(about).getByRole("button", { name: "Back to conversation" }));
    expect(screen.getByRole("button", { name: /^Requests/ })).toHaveFocus();
  });

  it("W7-2: About never rides on the Requests column; its Back closes it and the column comes back", async () => {
    localStorage.clear();
    render(<Fresh><RoomPanel {...props} /></Fresh>);
    await screen.findByRole("complementary", { name: "Requests" });
    expect(screen.queryByRole("complementary", { name: "About" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /^People/ }));
    const about = await screen.findByRole("complementary", { name: "About" });
    expect(screen.queryByRole("complementary", { name: "Requests" })).toBeNull();
    await userEvent.click(within(about).getByRole("button", { name: "Back to conversation" }));
    expect(screen.queryByRole("complementary", { name: "About" })).toBeNull();
    expect(await screen.findByRole("complementary", { name: "Requests" })).toBeInTheDocument();
  });

  it("W7-2: on a hard load at Home the hidden room claims no pane; the remembered column comes back in the room", async () => {
    localStorage.clear();
    _resetControllerForTests();
    attachShell(() => undefined);
    const host = document.createElement("div");
    document.body.appendChild(host);
    act(() => setThreadPaneHost(host));
    act(() => { openPanel("activity"); });
    try {
      render(<Fresh><RoomPanel {...props} /></Fresh>);
      await screen.findByRole("button", { name: /^Requests/ });
      expect(getWorkspaceSnapshot().pane).toBeNull();
      expect(screen.queryByRole("complementary", { name: /Requests|About/ })).toBeNull();
    } finally { host.remove(); _resetControllerForTests(); localStorage.clear(); }
  });

  it("W7-8: Files lists the room's attachments and what its requests made, never the runtime's disk", async () => {
    localStorage.clear();
    _resetControllerForTests();
    attachShell(() => undefined);
    const host = document.createElement("div");
    document.body.appendChild(host);
    act(() => setThreadPaneHost(host));
    const made = { ...FIXTURES.step05.tasks[0], taskId: "t-made", title: "Write the welcome", output: { idemKey: "o-1", kind: "document" as const, title: "Welcome note" } };
    client = fakeTasksClient([...FIXTURES.step05.tasks, made]); live.tasks = client;
    try {
      render(<Fresh><RoomPanel {...props} /></Fresh>);
      const files = await screen.findByRole("button", { name: "Files" });
      await userEvent.click(files);
      expect(getWorkspaceSnapshot().pane?.panelId).not.toBe("file-viewer");
      expect(files).toHaveAttribute("aria-pressed", "true");
      const about = await within(host).findByRole("complementary", { name: "About" });
      expect(within(about).getByRole("tab", { name: "Files" })).toHaveAttribute("aria-selected", "true");
      const list = await within(about).findByRole("list", { name: "Made by requests" });
      expect(within(list).getAllByRole("listitem")).toHaveLength(1);
      expect(list).toHaveTextContent("Welcome noteWrite the welcome");
      await userEvent.click(within(list).getByRole("button", { name: "Open Write the welcome" }));
      expect(await within(host).findByRole("complementary", { name: "Request" })).toHaveAttribute("data-task-pane", "t-made");
    } finally { host.remove(); _resetControllerForTests(); localStorage.clear(); }
  });

  it("W7 keyboard: a skip link jumps to the Requests column", async () => {
    localStorage.clear();
    render(<Fresh><RoomPanel {...props} /></Fresh>);
    await screen.findByRole("complementary", { name: "Requests" });
    await userEvent.click(screen.getByRole("button", { name: "Skip to requests" }));
    expect(screen.getByRole("heading", { name: "Requests" })).toHaveFocus();
  });

  it("W8-6: Skip to requests is the room's first Tab stop even with the column closed; it opens the column and lands there", async () => {
    localStorage.clear();
    localStorage.setItem(`lightbulb.room.requestsOpen:${SAM}`, "0");
    try {
      render(<Fresh><RoomPanel {...props} /></Fresh>);
      await screen.findByRole("button", { name: /^Requests/ });
      expect(screen.queryByRole("complementary", { name: "Requests" })).toBeNull();
      await userEvent.tab();
      const skip = screen.getByRole("button", { name: "Skip to requests" });
      expect(skip).toHaveFocus();
      await userEvent.keyboard("{Enter}");
      await waitFor(() => expect(screen.getByRole("heading", { name: "Requests" })).toHaveFocus());
    } finally { localStorage.clear(); }
  });

  it("W8-1: in the shell on a window where the pane is a sheet, an open request carries its own steer line", async () => {
    localStorage.clear();
    _resetControllerForTests();
    attachShell(() => undefined);
    const host = document.createElement("div");
    document.body.appendChild(host);
    act(() => setThreadPaneHost(host));
    const wide = window.matchMedia;
    window.matchMedia = ((query: string) => ({ matches: query === "(max-width: 735px)", media: query, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })) as unknown as typeof window.matchMedia;
    try {
      const reply: RoomMessage = { ...message, messageId: "m2", messageSeq: 2, author: { kind: "agent", id: lightbulb.id, displayName: "Lightbulb" }, text: "On it.", taskRefs: [{ taskId: "t-signup" }] } as RoomMessage;
      // A working request with its detail (step 8): steering is what it takes.
      client = fakeTasksClient(FIXTURES.step08.tasks, FIXTURES.step08.details); live.tasks = client;
      render(<Fresh><RoomPanel {...props} messages={[message, reply]} /></Fresh>);
      // A sheet would cover the conversation: closed until asked for.
      expect(screen.queryByRole("complementary", { name: "Requests" })).toBeNull();
      const card = (await screen.findAllByRole("button", { name: /^Open Workshop signup app/ })).find((b) => b.closest("[data-task-card]"))!;
      await userEvent.click(card);
      const pane = await within(host).findByRole("complementary", { name: "Request" });
      expect(await within(pane).findByRole("textbox", { name: "Steer this request" })).toBeInTheDocument();
      expect(screen.queryByText(/Replying to:/)).toBeNull();
    } finally { window.matchMedia = wide; host.remove(); _resetControllerForTests(); localStorage.clear(); }
  });

  it("on a narrow screen the toggle opens the Requests as a sheet over the conversation, and closes it", async () => {
    localStorage.clear();
    const wide = window.matchMedia;
    window.matchMedia = ((query: string) => ({ matches: query === "(max-width: 859px)", media: query, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })) as unknown as typeof window.matchMedia;
    try {
      render(<Fresh><RoomPanel {...props} /></Fresh>);
      const toggle = await screen.findByRole("button", { name: /^Requests/ });
      // Closed by default here, whatever the wide-screen choice: it would cover the conversation.
      expect(screen.queryByRole("complementary", { name: "Requests" })).toBeNull();
      expect(toggle).toHaveAttribute("aria-pressed", "false");
      await userEvent.click(toggle);
      const sheet = await screen.findByRole("complementary", { name: "Requests" });
      expect(screen.getByRole("button", { name: /^Requests/, hidden: true })).toHaveAttribute("aria-pressed", "true");
      await userEvent.click(within(sheet).getByRole("button", { name: "Close requests" }));
      expect(screen.queryByRole("complementary", { name: "Requests" })).toBeNull();
      // The sheet never touched the wide-screen choice.
      expect(localStorage.getItem(`lightbulb.room.requestsOpen:${SAM}`)).toBeNull();
    } finally { window.matchMedia = wide; localStorage.clear(); }
  });

  it("Scheduled is in the ··· menu and opens in the column with Back", async () => {
    localStorage.clear();
    render(<Fresh><RoomPanel {...props} /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: "Conversation options" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Scheduled" }));
    expect(await screen.findByRole("complementary", { name: "Scheduled" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Back to requests" }));
    expect(await screen.findByRole("complementary", { name: "Requests" })).toBeInTheDocument();
  });

  it("Lightbulb's messages read as Lightbulb, with no Coordinator or Agent tag (W8-7)", async () => {
    const front = { ...lightbulb, mentionHandle: "lightbulb" };
    const reply: RoomMessage = { ...message, messageId: "m2", messageSeq: 2, author: { kind: "agent", id: front.id, displayName: "Lightbulb" }, text: "On it." };
    render(<Fresh><RoomPanel {...props} participants={[sam, front]} messages={[message, reply]} /></Fresh>);
    const article = await screen.findByRole("article", { name: "Message from Lightbulb" });
    expect(within(article).queryByText("Coordinator")).toBeNull();
    expect(within(article).queryByText("Agent")).toBeNull();
  });

  it("naming Lightbulb in plain words shows on it… in a DM or where it listens to everything (dev walk R8)", async () => {
    const mentionsOnly = { ...lightbulb, mentionHandle: "lightbulb", listenMode: "mentions" as const };
    const ask = { ...message, text: "Hi Lightbulb, can you find a venue?", createdAt: new Date().toISOString() };
    const dm = { ...props.room!, kind: "dm" as const, name: null };
    const { rerender } = render(<Fresh><RoomPanel {...props} room={dm} participants={[sam, mentionsOnly]} messages={[ask]} /></Fresh>);
    expect(await screen.findByText("Lightbulb is on it…")).toBeInTheDocument();
    // A channel where Lightbulb only answers mentions: a plain name dispatches nothing, so no promise.
    rerender(<Fresh><RoomPanel {...props} participants={[sam, mentionsOnly, { ...sam, id: "priya", displayName: "Priya" }]} messages={[ask]} /></Fresh>);
    expect(screen.queryByText("Lightbulb is on it…")).toBeNull();
    // A word that only contains the name is not the name.
    rerender(<Fresh><RoomPanel {...props} room={dm} participants={[sam, mentionsOnly]} messages={[{ ...ask, text: "the lightbulbs are out" }]} /></Fresh>);
    expect(screen.queryByText("Lightbulb is on it…")).toBeNull();
  });

  it("a message sent while the workspace is still being set up reads Queued until Lightbulb answers", async () => {
    const { rerender } = render(<Fresh><RoomPanel {...props} runtimeStarting /></Fresh>);
    expect(await screen.findByText("Queued · Lightbulb answers as soon as setup finishes")).toBeInTheDocument();
    rerender(<Fresh><RoomPanel {...props} runtimeStarting={false} /></Fresh>);
    expect(screen.queryByText(/^Queued/)).toBeNull();
  });

  it("after an ask, Lightbulb is on it… until its reply lands; never for an old ask", async () => {
    const front = { ...lightbulb, mentionHandle: "lightbulb", listenMode: "active" as const };
    const ask = { ...message, createdAt: new Date().toISOString() };
    const { rerender } = render(<Fresh><RoomPanel {...props} participants={[sam, front]} messages={[ask]} /></Fresh>);
    expect(await screen.findByText("Lightbulb is on it…")).toBeInTheDocument();
    const reply: RoomMessage = { ...ask, messageId: "m2", messageSeq: 2, author: { kind: "agent", id: front.id, displayName: "Lightbulb" }, text: "Done." };
    rerender(<Fresh><RoomPanel {...props} participants={[sam, front]} messages={[ask, reply]} /></Fresh>);
    expect(screen.queryByText("Lightbulb is on it…")).toBeNull();
    rerender(<Fresh><RoomPanel {...props} participants={[sam, front]} messages={[{ ...ask, createdAt: "2026-11-04T14:00:00Z" }]} /></Fresh>);
    await waitFor(() => expect(screen.queryByText("Lightbulb is on it…")).toBeNull());
  });

  it("no on it… once the ask's run ended (failed or stopped), or when Lightbulb only listens for mentions", async () => {
    const front = { ...lightbulb, mentionHandle: "lightbulb", listenMode: "active" as const };
    const ask = { ...message, messageId: "m-ask", createdAt: new Date().toISOString() };
    const { rerender } = render(<Fresh><RoomPanel {...props} participants={[sam, front]} messages={[ask]} /></Fresh>);
    expect(await screen.findByText("Lightbulb is on it…")).toBeInTheDocument();
    const failed = { roomId: "r1", agentId: front.id, runId: "run-1", text: "", thinking: "", state: "aborted", tools: [], triggerMessageId: "m-ask" } as NonNullable<RoomPanelProps["runs"]>[number];
    rerender(<Fresh><RoomPanel {...props} participants={[sam, front]} messages={[ask]} runs={[failed]} /></Fresh>);
    expect(screen.queryByText("Lightbulb is on it…")).toBeNull();
    rerender(<Fresh><RoomPanel {...props} participants={[sam, { ...front, listenMode: "mentions" }]} messages={[ask]} /></Fresh>);
    expect(screen.queryByText("Lightbulb is on it…")).toBeNull();
  });

  it("About › Members lists people and Lightbulb only: no specialists, no Agent tag, no listen mode", async () => {
    const front = { ...lightbulb, mentionHandle: "lightbulb" };
    const finance: RoomParticipant = { ...lightbulb, id: "agent_fin", displayName: "Finance", mentionHandle: "finance", listenMode: "mentions" };
    render(<Fresh><RoomPanel {...props} participants={[sam, front, finance]} onRemoveMember={vi.fn()} onListenModeChange={vi.fn()} /></Fresh>);
    // About opens from the people in the header (and ··· › Details); with the super-agent it has no Requests tab.
    await userEvent.click(await screen.findByRole("button", { name: /^People/ }));
    expect(within(screen.getByRole("complementary", { name: "About" })).queryByRole("tab", { name: "Requests" })).toBeNull();
    const list = within(screen.getByRole("complementary", { name: "About" })).getByRole("heading", { name: "Members" }).closest("section")!;
    expect(within(list).getByText("Lightbulb")).toBeInTheDocument();
    expect(within(list).queryByText("Finance")).toBeNull();
    expect(within(list).queryByText("Agent")).toBeNull();
    expect(within(list).queryByRole("combobox")).toBeNull();
    expect(within(list).queryByRole("button", { name: "Remove Lightbulb" })).toBeNull();
    // No "Active work: No active runs" beside Working requests (N-F5): the Requests column has them.
    expect(screen.queryByText("No active runs.")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Active work" })).toBeNull();
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
