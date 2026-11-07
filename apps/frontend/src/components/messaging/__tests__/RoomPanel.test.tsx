import { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";
import { caretOffset, typeInComposer } from "@/test-utils/composer";
import type { RoomRunView } from "@lightbulb/shared";
import type { RoomMessage, RoomParticipant } from "../types";

// Search results come from the server; the query picks which hit comes back.
const searchHits = vi.hoisted(() => ({ byQuery: {} as Record<string, unknown[]> }));
vi.mock("../useMessageSearch", () => ({
  useMessageSearch: (query: string) => {
    const hits = searchHits.byQuery[query.trim()] ?? [];
    return { hits, state: hits.length ? "done" : "idle" };
  },
}));

afterEach(() => { searchHits.byQuery = {}; });
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));


const owner: RoomParticipant = { kind: "human", id: "owner", displayName: "Owner", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 };
const friday: RoomParticipant = { ...owner, kind: "agent", id: "friday", displayName: "Friday", role: "member", agentActivity: "running" };
const message: RoomMessage = { messageId: "message", roomId: "room", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Old name" }, text: "Hello", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null };
const props: RoomPanelProps = {
  ownerScopeId: "org", currentUserId: "owner", roomId: "room", state: "ready", httpReady: true, liveConnected: true,
  room: { roomId: "room", kind: "dm", name: null, roomRevision: 1, lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0 },
  participants: [owner, friday], messages: [message],
  composer: { text: "", onTextChange: vi.fn(), members: [owner, friday], clientMessageId: "draft", sendState: "idle", onSubmit: vi.fn() },
};

it("excludes only the signed-in human when naming a group DM", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "group-dm" }} participants={[owner, { ...owner, id: "alice", displayName: "Alice" }, { ...friday, id: "owner", displayName: "Atlas" }]} messages={[]} />);
  expect(screen.getByRole("heading", { name: "Alice and Atlas" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Alice and Atlas" })).toBeInTheDocument();
});

it("opens profiles before the real DM action for human and agent authors and displays local time", async () => {
  const onSelectParticipant = vi.fn();
  const colleague = { ...owner, id: "colleague", displayName: "Aden" };
  render(<RoomPanel {...props} onSelectParticipant={onSelectParticipant} participants={[owner, friday, colleague]} messages={[message, { ...message, messageId: "human", author: { kind: "human", id: "colleague", displayName: "Aden" } }]} />);
  for (const participant of [friday, colleague]) {
    await userEvent.click(within(screen.getByRole("article", { name: `Message from ${participant.displayName}` })).getByRole("button", { name: participant.displayName }));
    const profile = screen.getByRole("dialog", { name: participant.displayName });
    await userEvent.click(within(profile).getByRole("button", { name: "Message" }));
    expect(onSelectParticipant).toHaveBeenLastCalledWith(participant);
  }
  const timestamp = within(screen.getByRole("article", { name: "Message from Friday" })).getByText(new Date(message.createdAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
  expect(timestamp).toHaveAttribute("datetime", message.createdAt);
});

it.each(["system", "human"] as const)("keeps unmatched %s authors neutral", (kind) => {
  render(<RoomPanel {...props} messages={[{ ...message, author: { kind, id: "friday", displayName: "Announcement" } }]} />);
  const article = screen.getByRole("article", { name: "Message from Announcement" });
  expect(within(article).getByRole("img", { name: "Announcement" }).parentElement).not.toHaveAttribute("data-agent-state");
  expect(within(article).queryByText(/^Agent /)).not.toBeInTheDocument();
});

it("collapses a finished run with no reply into a muted status line", () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "completed", tools: [] };
  render(<RoomPanel {...props} runs={[run]} />);
  expect(screen.getByText("Friday finished · no reply")).toBeInTheDocument();
  expect(screen.queryByRole("article", { name: "Live run from Friday" })).not.toBeInTheDocument();
});

// What a client that opened the room mid-run holds: a run seeded from the room
// detail's `activeRuns`, with no frame text yet. The no-text collapse must not
// swallow it — that is the "Forge is working" bubble and its Stop.
it("renders a running run that has no text yet as a working bubble with Stop and no steer box", () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "running", tools: [] };
  render(<RoomPanel {...props} runs={[run]} onAbortRun={vi.fn()} />);
  const article = screen.getByRole("article", { name: "Live run from Friday" });
  expect(within(article).getByRole("button", { name: "Stop Friday" })).toHaveTextContent("Stop");
  expect(within(article).getByRole("button", { name: "Stop Friday" })).toHaveAttribute("title", "Stop Friday");
  // In the header row (far right), not on a line of its own.
  expect(within(article).getByRole("button", { name: "Stop Friday" }).closest(".room-message-byline")).not.toBeNull();
  // The composer is the steer: a follow-up is delivered into the live run by the backend.
  expect(within(article).queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.queryByText("Friday finished · no reply")).not.toBeInTheDocument();
});

it("says a stop the runtime never confirmed may still be finishing its step", () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "aborted", forcedStop: true, tools: [] };
  render(<RoomPanel {...props} runs={[run]} onAbortRun={vi.fn()} />);
  expect(screen.getByText("Friday stopped (the agent may finish its current step)")).toBeInTheDocument();
});

it("shows Stopping… until the run ends, and gives Stop back if the request fails", async () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "running", tools: [] };
  let fail: (error: Error) => void = () => {};
  const onAbortRun = vi.fn(() => new Promise((_, reject) => { fail = reject; }));
  render(<RoomPanel {...props} runs={[run]} onAbortRun={onAbortRun} />);
  fireEvent.click(screen.getByRole("button", { name: "Stop Friday" }));
  expect(onAbortRun).toHaveBeenCalledWith("run-1");
  expect(screen.getByRole("button", { name: "Stopping Friday…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Stopping Friday…" })).toHaveTextContent("Stopping…");
  await act(async () => fail(new Error("503")));
  expect(screen.getByRole("button", { name: "Stop Friday" })).toBeEnabled();
});

it("shows one working card per agent, a waiting note on the queued follow-up and a receipt on a steered one", () => {
  const queued: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-2", triggerMessageId: "m2", text: "", thinking: "", state: "running", tools: [] };
  const live: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", triggerMessageId: "m1", text: "Looking", thinking: "", state: "running", tools: [], steeredMessageIds: ["m3"] };
  const messages = ["m1", "m2", "m3"].map((messageId, index) => ({ ...props.messages[0], messageId, messageSeq: 100 + index, text: `ask ${index}` }));
  render(<RoomPanel {...props} messages={messages} runs={[queued, live]} onAbortRun={vi.fn()} />);
  expect(screen.getAllByRole("article", { name: "Live run from Friday" })).toHaveLength(1);
  expect(screen.getByText("Friday will read this after the current reply")).toBeInTheDocument();
  expect(screen.getByText("Friday saw this")).toBeInTheDocument();
});

// Prod 2026-09-25: Friday's stopped run (its text never saved) stayed pinned below every later
// message, so the owner's answer rendered ABOVE Friday's question and read as missing.
it("renders a message sent after a run ended below that run, with the new working card last", () => {
  const ask = { ...message, messageId: "ask", messageSeq: 129, author: { kind: "human" as const, id: "user", displayName: "You" }, text: "can we get back to the demos?" };
  const answer = { ...ask, messageId: "answer", messageSeq: 130, text: "it should be admin@thelightbulb.company" };
  const stopped: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", afterSeq: 129, text: "Which email should I use?", thinking: "", state: "aborted", tools: [] };
  const live: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-2", afterSeq: 130, text: "", thinking: "", state: "running", tools: [] };
  render(<RoomPanel {...props} messages={[ask, answer]} runs={[stopped, live]} />);
  const order = within(screen.getByRole("region", { name: "Messages" })).getAllByRole("article").map((node) => node.textContent ?? "");
  expect(order.findIndex((text) => text.includes("can we get back"))).toBeLessThan(order.findIndex((text) => text.includes("Which email")));
  expect(order.findIndex((text) => text.includes("Which email"))).toBeLessThan(order.findIndex((text) => text.includes("admin@thelightbulb.company")));
  expect(order.at(-1)).toContain("Working");
});

// The browser's native <details>/<summary> marker — a large unstyled triangle
// that ignored the design system — was the loudest thing in the transcript.
// Thinking is now the same quiet chevron button the tool trace uses.
it("puts a run's thinking behind a quiet chevron button, not a native disclosure", async () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "Weighing two options", state: "running", tools: [] };
  const { container } = render(<RoomPanel {...props} runs={[run]} />);
  expect(container.querySelector("details")).toBeNull();
  const toggle = screen.getByRole("button", { name: "Thinking" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText("Weighing two options")).not.toBeInTheDocument();
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("Weighing two options")).toBeInTheDocument();
});

// The trace folds itself now, so wrapping it in a second "What it did"
// disclosure buried one summary line behind two clicks.
it("shows a finished no-reply run's tool activity as one folded line, not a nested disclosure", () => {
  const tools = [1, 2, 3].map((i) => ({ id: `t${i}`, name: "read", args: { path: `f${i}.ts` }, result: "ok", status: "done" as const }));
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "completed", tools };
  const { container } = render(<RoomPanel {...props} runs={[run]} />);
  expect(container.querySelector("details")).toBeNull();
  expect(screen.queryByText("What it did")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Read 3 files/ })).toBeInTheDocument();
});

it("keeps rendering a finished run's text instead of collapsing it", () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "Partial answer", thinking: "", state: "completed", tools: [] };
  render(<RoomPanel {...props} runs={[run]} />);
  expect(within(screen.getByRole("article", { name: "Live run from Friday" })).getByText("Partial answer")).toBeInTheDocument();
  expect(screen.queryByText("Friday finished · no reply")).not.toBeInTheDocument();
});

it.each([
  ["an ordinary member", { participants: [{ ...owner, role: "member" as const }, friday] }],
  ["a shared channel", { room: { ...props.room!, kind: "channel" as const, shared: true } }],
  ["a group DM", { room: { ...props.room!, kind: "group-dm" as const, name: "Launch" } }],
])("hides the direct Add people header action for %s", (_label, overrides) => {
  render(<RoomPanel {...props} {...overrides} addMemberCandidates={[]} onAddMember={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "Add people" })).not.toBeInTheDocument();
});

it("collapses consecutive messages from the same author and reveals the time in the gutter", () => {
  const second: RoomMessage = { ...message, messageId: "message-2", messageSeq: 2, createdAt: "2026-09-10T00:01:00Z", text: "Again" };
  render(<RoomPanel {...props} messages={[message, second]} />);
  const articles = screen.getAllByRole("article", { name: "Message from Friday" });
  expect(articles).toHaveLength(2);
  expect(within(articles[0]).getByText("Friday")).toBeInTheDocument();
  expect(within(articles[1]).queryByText("Friday")).not.toBeInTheDocument();
  const time = new Date(second.createdAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  expect(within(articles[1]).getByText(time, { selector: ".room-message-gutter" })).toBeInTheDocument();
  expect(within(articles[1]).getByText(time, { selector: "time.sr-only" })).toBeInTheDocument();
});

it("keeps every action reachable on a collapsed message", async () => {
  const user = userEvent.setup();
  const second: RoomMessage = { ...message, messageId: "message-2", messageSeq: 2, createdAt: "2026-09-10T00:01:00Z", text: "Again" };
  render(<RoomPanel {...props} messages={[message, second]} onMarkUnreadFrom={vi.fn(async () => {})} />);
  const collapsed = screen.getAllByRole("article", { name: "Message from Friday" })[1];
  expect(within(collapsed).getByRole("button", { name: "Copy link" })).toBeInTheDocument();
  await user.click(within(collapsed).getByRole("button", { name: "Message actions" }));
  expect(await screen.findByRole("menuitem", { name: "Mark unread from here" })).toBeInTheDocument();
});

it("renders @mentions as chips", () => {
  render(<RoomPanel {...props} participants={[owner, { ...friday, mentionHandle: "friday" }]} messages={[{ ...message, text: "ping @friday" }]} />);
  expect(screen.getByText("@friday")).toHaveClass("room-mention");
});

it("renders an agent-pasted attachment link as inert text, not a broken link", () => {
  // /api/attachments/{id}/content only resolves through the issue-scoped
  // Teams BFF — a room message has no owning issue to fetch it through, so
  // it must not render as a link to this app's own (404ing) host.
  render(<RoomPanel {...props} messages={[{ ...message, text: "See [the report](/api/attachments/att-1/content)" }]} />);
  expect(screen.getByText("the report")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "the report" })).not.toBeInTheDocument();
});

it("marks a message that mentions the viewer for highlighting", () => {
  render(<RoomPanel {...props} messages={[{ ...message, mentions: [{ kind: "human", id: "owner" }] }]} />);
  expect(screen.getByRole("article", { name: "Message from Friday" })).toHaveClass("room-message-mentions-me");
});

it("keeps the New line when a send fails and clears it after a successful send", async () => {
  const onSubmit = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(undefined);
  const reply = { ...message, messageId: "reply", messageSeq: 2, text: "Hi", author: { kind: "human" as const, id: "owner", displayName: "Owner" } };

  function Harness() {
    const [text, setText] = useState("");
    const [draftId, setDraftId] = useState(0);
    return <RoomPanel {...props} readSeqAtOpen={1} messages={[message, reply]}
      composer={{
        ...props.composer,
        text,
        onTextChange: setText,
        clientMessageId: `draft-${draftId}`,
        onSubmit: async (request) => {
          try {
            await onSubmit(request);
          } finally {
            setText("");
            setDraftId((value) => value + 1);
          }
        },
      }} />;
  }

  render(<Harness />);
  expect(screen.getByRole("separator", { name: "New messages" })).toBeInTheDocument();

  typeInComposer("First try");
  // Synchronous dispatch (no await before the assertion): the send is still in flight,
  // so a fix that waits for it to resolve must not have flipped sentSinceOpen yet.
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  expect(screen.getByRole("separator", { name: "New messages" })).toBeInTheDocument();

  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveAttribute("contenteditable", "true"));
  expect(onSubmit).toHaveBeenCalledTimes(1);
  // The failed send has now fully settled (composer re-enabled): the New line must
  // still be here, not just synchronously right after the keydown.
  expect(screen.getByRole("separator", { name: "New messages" })).toBeInTheDocument();

  typeInComposer("Second try");
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByRole("separator", { name: "New messages" })).not.toBeInTheDocument());
});

it("copies a message permalink from the toolbar", async () => {
  const user = userEvent.setup();
  const writeText = vi.fn(async () => {});
  // userEvent.setup() installs its own clipboard stub (getter-only); override
  // it after setup so the handler writes through our spy (see
  // IssueDetailPage.description.test.tsx for the same pattern).
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(<RoomPanel {...props} />);
  await user.click(screen.getByRole("button", { name: "Copy link" }));
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining("panel=chat&room=room&message=message"));
  expect(await screen.findByRole("status", { name: "Link copied" })).toBeInTheDocument();
});

it("shows an alert when copying the link fails, and clears it on the next successful copy", async () => {
  const user = userEvent.setup();
  const writeText = vi.fn().mockRejectedValueOnce(new Error("denied")).mockResolvedValueOnce(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(<RoomPanel {...props} />);
  await user.click(screen.getByRole("button", { name: "Copy link" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not copy the link.");
  await user.click(screen.getByRole("button", { name: "Copy link" }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});

it("opens the edit form for your last message when ArrowUp lands in an empty composer", () => {
  const own = { ...message, messageId: "own", messageSeq: 2, author: { kind: "human" as const, id: props.currentUserId, displayName: "Owner" } };
  render(<RoomPanel {...props} messages={[message, own]} onUpdateMessage={vi.fn()} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  const edit = screen.getByRole("textbox", { name: "Edit message" });
  // The caret lands after the text (W18 walk: it opened at the start).
  expect(edit).toHaveFocus();
  expect(edit.textContent!.length).toBeGreaterThan(0);
  expect(caretOffset(edit)).toBe(edit.textContent!.length);
});

it("lets a second ArrowUp reopen the edit form for the same message after Cancel", async () => {
  const user = userEvent.setup();
  const own = { ...message, messageId: "own", messageSeq: 2, author: { kind: "human" as const, id: props.currentUserId, displayName: "Owner" } };
  render(<RoomPanel {...props} messages={[message, own]} onUpdateMessage={vi.fn()} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(screen.getByRole("textbox", { name: "Edit message" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Cancel edit" }));
  expect(screen.queryByRole("textbox", { name: "Edit message" })).not.toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(screen.getByRole("textbox", { name: "Edit message" })).toBeInTheDocument();
});

it("marks unread from a message using that message's sequence", async () => {
  const onMarkUnreadFrom = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} onMarkUnreadFrom={onMarkUnreadFrom} messages={[{ ...message, messageSeq: 7 }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Mark unread from here" }));
  expect(onMarkUnreadFrom).toHaveBeenCalledWith(7);
});

it("replaces the composer in an archived channel with an Unarchive action for admins only", async () => {
  const archived = { ...props.room!, kind: "channel" as const, name: "Launch", archivedAt: "2026-09-23T00:00:00Z" };
  const onSetArchived = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomPanel {...props} room={archived} onSetArchived={onSetArchived} />);
  expect(screen.getByText("This channel is archived. Unarchive it to post.")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Message" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Unarchive channel" }));
  await waitFor(() => expect(onSetArchived).toHaveBeenCalledWith(false));

  rerender(<RoomPanel {...props} room={archived} participants={[{ ...owner, role: "member" }, friday]} onSetArchived={onSetArchived} />);
  expect(screen.getByText("This channel is archived.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Unarchive channel" })).not.toBeInTheDocument();
});

it("offers no top-level composer in the workspace feed", () => {
  const feed = { ...props.room!, kind: "channel" as const, name: "feed", feed: true };
  render(<RoomPanel {...props} room={feed} />);
  expect(screen.getByText("Only agents post to the feed.")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /message/i })).not.toBeInTheDocument();
});

it("lets a plain member unarchive an archived group chat", async () => {
  const archived = { ...props.room!, kind: "group-dm" as const, name: "Trio", archivedAt: "2026-09-23T00:00:00Z" };
  const onSetArchived = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} room={archived} participants={[{ ...owner, role: "member" }, friday]} onSetArchived={onSetArchived} />);
  expect(screen.getByText("This group chat is archived. Unarchive it to post.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Unarchive group chat" }));
  await waitFor(() => expect(onSetArchived).toHaveBeenCalledWith(false));
});

it("shows the pre-typed replies in the starter channel until a human has written", async () => {
  const onTextChange = vi.fn();
  const panelProps = { ...props, room: { ...props.room!, isDefault: true }, composer: { ...props.composer, onTextChange } };
  const view = render(<RoomPanel {...panelProps} messages={[message]} />);
  await userEvent.click(screen.getByRole("button", { name: "I'm starting" }));
  expect(onTextChange).toHaveBeenCalledWith("I'm starting ");
  const human = { ...message, messageId: "h1", messageSeq: 2, author: { kind: "human" as const, id: "owner", displayName: "Owner" } };
  view.rerender(<RoomPanel {...panelProps} messages={[message, human]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("hides the pre-typed replies when the welcome is not the oldest loaded message", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} messages={[{ ...message, messageSeq: 101 }]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("hides the pre-typed replies while a human message is pending", () => {
  const pending = { clientMessageId: "c1", roomId: props.roomId, threadRootMessageId: null, text: "hi", state: "sending" } as never;
  render(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} messages={[message]} pendingMessages={[pending]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("keeps a draft the person already typed when a pre-typed reply is tapped", async () => {
  const onTextChange = vi.fn();
  const panelProps = { ...props, room: { ...props.room!, isDefault: true }, composer: { ...props.composer, text: "hello", onTextChange } };
  render(<RoomPanel {...panelProps} messages={[message]} />);
  await userEvent.click(screen.getByRole("button", { name: "I'm starting" }));
  expect(onTextChange).not.toHaveBeenCalled();
});

it("never shows the pre-typed replies outside the starter channel", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, isDefault: false }} messages={[message]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("with the switch, shows the pre-typed replies after a message the owner sent before setup finished", () => {
  const early = { ...message, messageId: "h0", author: { kind: "human" as const, id: "owner", displayName: "Owner" } };
  const welcome = { ...message, messageSeq: 2, kind: "first_run" };
  const room = { ...props.room!, kind: "dm" as const, firstRun: true };
  const view = render(<RoomPanel {...props} room={room} messages={[early, welcome]} />);
  expect(screen.getByRole("button", { name: "I'm starting" })).toBeInTheDocument();
  const answer = { ...early, messageId: "h1", messageSeq: 3 };
  view.rerender(<RoomPanel {...props} room={{ ...room, firstRun: false }} messages={[early, welcome, answer]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("with the switch, shows the pre-typed replies in the first-run Lightbulb DM and not under #general's team line", () => {
  const view = render(<RoomPanel {...props} room={{ ...props.room!, kind: "dm", firstRun: true }} messages={[{ ...message, kind: "first_run" }]} />);
  expect(screen.getByRole("button", { name: "I'm starting" })).toBeInTheDocument();
  view.rerender(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} messages={[{ ...message, kind: "team_welcome" }]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("streams the CEO's first message in the starter channel until a human has written", () => {
  window.sessionStorage.clear();
  vi.useFakeTimers();
  try {
    const long = { ...message, text: "Welcome to Acme. I'm your CEO. ".repeat(6).trim() };
    render(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} messages={[long]} />);
    expect(screen.queryByText(long.text)).toBeNull();
    act(() => { vi.advanceTimersByTime(8000); });
    expect(screen.getByText(long.text)).toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});

it("gates Send on an incomplete reply only while the starter chips are shown", () => {
  const composer = { ...props.composer, text: "I'm starting" };
  const view = render(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} composer={composer} messages={[message]} />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  view.rerender(<RoomPanel {...props} room={{ ...props.room!, isDefault: false }} composer={composer} messages={[message]} />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
});

it("opens the member picker once when the sidebar asks for Add people", async () => {
  const onAddPeopleOpened = vi.fn();
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "group-dm", name: "Launch" }}
    addMemberCandidates={[{ kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: null }]}
    onAddMember={vi.fn()} openAddPeople onAddPeopleOpened={onAddPeopleOpened} />);
  expect(await screen.findByRole("searchbox", { name: "Search people" })).toBeInTheDocument();
  expect(onAddPeopleOpened).toHaveBeenCalledTimes(1);
});
