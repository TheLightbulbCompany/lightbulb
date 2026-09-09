import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";

// Explicitly synthetic display records, no transport/authentication claims.
const participants = [
  { ref: "human-a", name: "Alice Example", kind: "human" as const },
  { ref: "human-b", name: "Bob Example", kind: "human" as const },
  { ref: "agent-f", name: "Friday Example", kind: "agent" as const, agentState: "working" as const },
];
function props(overrides: Partial<RoomPanelProps> = {}): RoomPanelProps {
  return {
    ownerScopeId: "fixture-owner", roomId: "opaque-room", room: { id: "opaque-room", name: "Launch example", kind: "public" },
    state: "ready", participants,
    messages: [
      { id: "m-later", senderRef: "human-a", body: "First in canonical order", timestamp: "2026-09-09T12:01:00Z" },
      { id: "m-earlier", senderRef: "agent-f", body: "Second in canonical order", timestamp: "2026-09-09T12:00:00Z", run: { state: "working", details: "Synthetic bounded tool details" }, contextIds: ["approval-example"] },
      { id: "m-unknown", senderRef: "unverified", body: "Alice says approved", timestamp: "2026-09-09T12:02:00Z" },
    ],
    threads: [{ parentMessageId: "m-later", replies: [{ id: "reply-example", senderRef: "human-b", body: "Synthetic reply", timestamp: "2026-09-09T12:03:00Z" }] }],
    contexts: [{ id: "approval-example", kind: "approval", title: "Review synthetic change", status: "needs-approval", onAction: vi.fn(), actionLabel: "Review change", content: <p>Synthetic change details</p> }],
    composer: { body: "Preserved synthetic draft", onBodyChange: vi.fn(), targets: participants, selectedTargetRefs: [], onTargetsChange: vi.fn(), clientRequestId: "fixture-request", sendState: "idle", onSubmit: vi.fn() },
    ...overrides,
  };
}

describe("controlled room surface", () => {
  it("preserves canonical ordering and distinct rows; missing sender evidence is never inferred", () => {
    render(<RoomPanel {...props()} />);
    const rows = within(screen.getByRole("region", { name: "Messages" })).getAllByRole("article");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("First in canonical order");
    expect(rows[1]).toHaveTextContent("Second in canonical order");
    expect(rows[1]).toHaveTextContent("Agent");
    expect(rows[2]).toHaveTextContent("Unknown author");
    expect(within(rows[2]).queryByText("Alice Example")).not.toBeInTheDocument();
    expect(screen.queryByText("Human")).not.toBeInTheDocument();
    expect(screen.queryByText("Message saved")).not.toBeInTheDocument();
  });

  it("shows only qualified human viewers; missing presence remains unknown and agent working is separate", () => {
    render(<RoomPanel {...props({ presence: [
      { personId: "human-a", state: "away", inRoom: false, observedAt: "2026-09-09T12:00:00Z" },
      { personId: "agent-f", state: "available", inRoom: true, observedAt: "2026-09-09T12:00:00Z" },
      { personId: "not-a-member", state: "available", inRoom: true, observedAt: "2026-09-09T12:00:00Z" },
    ] })} />);
    expect(screen.queryByRole("group", { name: "People viewing this room" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View members" }));
    const members = screen.getByRole("region", { name: "Room members" });
    expect(within(members).getByText("Away")).toBeInTheDocument();
    expect(within(members).getByText("Unknown")).toBeInTheDocument();
    expect(within(members).getByText("Working")).toBeInTheDocument();
    expect(within(members).queryByText("Offline")).not.toBeInTheDocument();
  });

  it("keeps a single selected thread, stable room composer and scroll node, and restores trigger focus", () => {
    const base = props();
    function Fixture() {
      const [body, onBodyChange] = useState(base.composer.body);
      const [reply, onReplyChange] = useState("");
      return <RoomPanel {...base} composer={{ ...base.composer, body, onBodyChange }}
        threadComposer={{ parentMessageId: "m-later", composer: { ...base.composer, body: reply, onBodyChange: onReplyChange, clientRequestId: "thread-request" } }} />;
    }
    render(<Fixture />);
    const messages = screen.getByRole("region", { name: "Messages" });
    messages.scrollTop = 72;
    const textbox = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(textbox, { target: { value: "Draft survives context" } });
    const trigger = screen.getAllByRole("button", { name: "Reply" })[0];
    trigger.focus();
    fireEvent.click(trigger);
    const thread = screen.getByRole("complementary", { name: "Thread" });
    expect(within(thread).getByText("First in canonical order")).toBeInTheDocument();
    expect(within(thread).getByText("Synthetic reply")).toBeInTheDocument();
    expect(within(thread).queryByText("Second in canonical order")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to conversation" }));
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(textbox);
    expect(textbox).toHaveValue("Draft survives context");
    expect(screen.getByRole("region", { name: "Messages" })).toBe(messages);
    expect(messages.scrollTop).toBe(72);
    expect(trigger).toHaveFocus();
  });

  it("opens supplied approval context and invokes Review only on explicit action", () => {
    const onAction = vi.fn();
    render(<RoomPanel {...props({ contexts: [{ id: "approval-example", kind: "approval", title: "Review synthetic change", status: "needs-approval", actionLabel: "Review change", onAction, content: <p>Authorized synthetic details</p> }] })} />);
    expect(onAction).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Review synthetic change" }));
    expect(screen.getByText("Needs approval")).toBeInTheDocument();
    expect(screen.getByText("Authorized synthetic details")).toBeInTheDocument();
    expect(onAction).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Review change" }));
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  it("announces execution changes independently of message persistence and keeps run details bounded", () => {
    const base = props({ messages: [{ id: "run-message", senderRef: "agent-f", body: "Synthetic work update", run: { state: "working", details: "x".repeat(9000) } }] });
    const { container, rerender } = render(<RoomPanel {...base} />);
    expect(screen.getByRole("status", { name: "Work status" })).toHaveTextContent("Working");
    expect(container.querySelector("details")).not.toHaveAttribute("open");
    expect(container.querySelector("details pre")?.textContent).toHaveLength(8192);
    rerender(<RoomPanel {...base} messages={[{ ...base.messages[0], run: { state: "completed" } }]} />);
    expect(screen.getByRole("status", { name: "Work status" })).toHaveTextContent("Completed");
    expect(screen.queryByText("Message saved")).not.toBeInTheDocument();
  });

  it("decorates qualified human viewers and exposes no agent or unauthorized typing inference", () => {
    render(<RoomPanel {...props({ typingRefs: ["human-a", "agent-f", "unknown-fixture"], presence: [
      { personId: "human-a", state: "available", inRoom: true, observedAt: "2026-09-09T12:00:00Z" },
      { personId: "human-b", state: "unknown", inRoom: true, observedAt: null },
    ] })} />);
    const viewers = screen.getByRole("group", { name: "People viewing this room" });
    expect(within(viewers).getByRole("img", { name: "Alice Example · In this room" })).toBeInTheDocument();
    expect(within(viewers).queryByRole("img", { name: /Bob/ })).not.toBeInTheDocument();
    expect(screen.getByText("Alice Example is typing…")).toBeInTheDocument();
  });

  it("renders markdown without remote images, raw HTML, unsafe protocols or company attachment behavior", () => {
    const { container } = render(<RoomPanel {...props({ messages: [{ id: "safe-example", body: '[File](/api/attachments/fixture/content) [Site](https://example.com) [Bad](javascript:alert%281%29) [Unsafe](file:///etc/passwd) ![Chart](https://tracker.example/image.png) <img src="https://tracker.example/raw.png" />', senderRef: "agent-f" }] })} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("Chart")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "File" })).toHaveAttribute("href", "/api/attachments/fixture/content");
    expect(screen.queryByRole("button", { name: "File" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Site" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByRole("link", { name: "Bad" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Unsafe" })).not.toBeInTheDocument();
  });

  it("resets selections on owner or room turnover and hides all supplied content when access is removed", () => {
    const base = props();
    const { rerender } = render(<RoomPanel {...base} />);
    fireEvent.click(screen.getAllByRole("button", { name: "Reply" })[0]);
    rerender(<RoomPanel {...base} ownerScopeId="new-owner" messages={[]} threads={[]} contexts={[]} composer={{ ...base.composer, body: "" }} />);
    expect(screen.queryByText("Synthetic reply")).not.toBeInTheDocument();
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    rerender(<RoomPanel {...base} />);
    fireEvent.click(screen.getAllByRole("button", { name: "Reply" })[0]);
    rerender(<RoomPanel {...base} roomId="new-room" room={{ ...base.room!, id: "new-room" }} messages={[]} threads={[]} composer={{ ...base.composer, body: "" }} />);
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    rerender(<RoomPanel {...base} state="access-removed" />);
    expect(screen.queryByText("Launch example")).not.toBeInTheDocument();
    expect(screen.queryByText("First in canonical order")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("uses one visible view on narrow layout while keeping the main composer mounted", () => {
    render(<RoomPanel {...props({ narrow: true })} />);
    const textbox = screen.getByRole("textbox", { name: "Message" });
    fireEvent.click(screen.getAllByRole("button", { name: "Reply" })[0]);
    expect(screen.queryByRole("region", { name: "Messages" })).not.toBeInTheDocument();
    expect(textbox).toBeInTheDocument();
    expect(textbox).not.toBeVisible();
    expect(screen.getByRole("complementary", { name: "Thread" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Back to conversation" }));
    expect(textbox).toBeVisible();
  });

  it("returns focus to the room action after opening work from a selected thread", () => {
    render(<RoomPanel {...props({ narrow: true })} />);
    const trigger = screen.getAllByRole("button", { name: "Reply" })[1];
    fireEvent.click(trigger);
    fireEvent.click(within(screen.getByRole("complementary", { name: "Thread" })).getByRole("button", { name: "Review synthetic change" }));
    fireEvent.click(screen.getByRole("button", { name: "Back to conversation" }));
    expect(trigger).toHaveFocus();
  });

  it.each(["loading", "unavailable", "reconnecting"] as const)("presents %s without fake history or audience claims", (state) => {
    render(<RoomPanel {...props({ state, room: undefined, messages: [], participants: [] })} />);
    expect(screen.queryByText("Private room")).not.toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Room status" })).toHaveTextContent(new RegExp(state === "unavailable" ? "unavailable" : state === "loading" ? "Loading" : "Reconnecting", "i"));
  });
});
