import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ApiError, type LiveEvent, type LocalSession, type LocalSessionCommand, type LocalSessionCommandReceipt, type LocalSessionEventPage, type RoomResources } from "@lightbulb/shared";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";
import { useRoomResources } from "@/hooks/useRoomResources";
import { useState } from "react";

const session: LocalSession = {
  sessionId: "session-123456", deviceId: "mac", humanUserId: "alice", provider: "codex",
  providerVersion: "0.149.0", generation: "g1", status: "unknown", activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  controlUnavailableReason: null, latestEventSeq: 250,
};
const resources: RoomResources = {
  devices: [{ deviceId: "mac", humanUserId: "alice", cliDeviceId: "cli" }], sessions: [session],
  pairedNodes: [
    { deviceId: "mac", humanUserId: "alice", clientId: "openclaw-macos", macDeviceId: "mac" },
    { deviceId: "cli", humanUserId: "alice", clientId: "node-host", macDeviceId: "mac" },
    { deviceId: "unassociated", humanUserId: "bob", clientId: "node-host", macDeviceId: null },
  ],
};
function page(after: number, limit: number, last = 250): LocalSessionEventPage {
  const end = Math.min(last, after + limit);
  return {
    events: Array.from({ length: Math.max(0, end - after) }, (_, i) => ({ seq: after + i + 1, kind: "text", role: "assistant", text: `Native event ${after + i + 1}`, truncated: false })),
    nextAfterSeq: end < last ? end : null,
  };
}
function setup(roomId = "room") {
  const handlers = new Set<(event: LiveEvent) => void>();
  const receipt = (request: LocalSessionCommand): LocalSessionCommandReceipt => ({
    ...request, text: request.text ?? null, expectedTurnId: request.expectedTurnId ?? null,
    sessionId: session.sessionId, roomId, actorUserId: "bob", commandSeq: 1, state: "pending",
    createdAt: 1, deliverBy: 31, held: false, providerMessageId: null,
  });
  const client = {
    listResources: vi.fn(async () => resources),
    listLocalSessionEvents: vi.fn(async (_room: string, _session: string, after: number, limit = 50) => page(after, limit)),
    submitLocalSessionCommand: vi.fn(async (_room: string, _session: string, request: LocalSessionCommand) => receipt(request)),
    getLocalSessionCommand: vi.fn<(_room: string, _session: string, _id: string) => Promise<LocalSessionCommandReceipt>>(),
  };
  const source = { client, connectionRevision: 1, onEvent: (handler: (event: LiveEvent) => void) => { handlers.add(handler); return () => { handlers.delete(handler); }; } };
  const people = ["alice", "bob", "outsider"].map((id) => ({ kind: "human" as const, id, displayName: id[0].toUpperCase() + id.slice(1), mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "now", joinedSeq: 0 }));
  const props: RoomPanelProps = {
    ownerScopeId: "org", currentUserId: "bob", roomId, room: { roomId, kind: "dm", name: null, roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0, updatedAt: "now", topic: null, starred: false, muted: false, sectionId: null },
    state: "ready", httpReady: true, liveConnected: true, participants: people, messages: [], resourceSource: source,
    composer: { text: "Room draft stays here", onTextChange: vi.fn(), members: people, clientMessageId: "room-draft", sendState: "idle", onSubmit: vi.fn() },
  };
  const emit = (type: "room.resources.changed" | "room.metadata.changed" | "room.access.removed") => {
    for (const h of handlers) h({ type, payload: { roomId }, roomId, schemaVersion: 1, eventId: type, occurredAt: "now" } as LiveEvent);
  };
  return { props, source, client, receipt, emit };
}
async function openSession() {
  await userEvent.click(screen.getByRole("button", { name: "Devices & sessions" }));
  await userEvent.click(await screen.findByRole("button", { name: /Open Codex/ }));
}
afterEach(() => vi.useRealTimers());

it("consumes authorized session navigation once, allows Back and explicit repeat, and never sends on open", async () => {
  const f = setup();
  const view = render(<RoomPanel {...f.props} targetSession={{ sessionId: session.sessionId, nonce: 1 }} />);
  expect(await screen.findByRole("region", { name: "Native session history" })).toBeInTheDocument();
  expect(f.client.submitLocalSessionCommand).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Back to devices & sessions" }));
  expect(screen.queryByRole("region", { name: "Native session history" })).not.toBeInTheDocument();
  view.rerender(<RoomPanel {...f.props} targetSession={{ sessionId: session.sessionId, nonce: 1 }} />);
  expect(screen.queryByRole("region", { name: "Native session history" })).not.toBeInTheDocument();
  view.rerender(<RoomPanel {...f.props} targetSession={{ sessionId: session.sessionId, nonce: 2 }} />);
  expect(await screen.findByRole("region", { name: "Native session history" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Add link to conversation draft" }));
  expect(f.props.composer.onTextChange).toHaveBeenCalledWith(expect.stringContaining("Room draft stays here\n"));
  expect(f.props.composer.onSubmit).not.toHaveBeenCalled();
  expect(f.client.submitLocalSessionCommand).not.toHaveBeenCalled();
});

it("cannot select a session absent from the current authorized directory", async () => {
  const f = setup();
  render(<RoomPanel {...f.props} targetSession={{ sessionId: "not-authorized", nonce: 1 }} />);
  expect(await screen.findByText("Session unavailable in this conversation.")).toBeInTheDocument();
  expect(f.client.listLocalSessionEvents).not.toHaveBeenCalled();
});

it("does not replay a consumed URL selection after resource access is revoked and the pane reopened", async () => {
  const f = setup();
  render(<RoomPanel {...f.props} targetSession={{ sessionId: session.sessionId, nonce: 1 }} />);
  expect(await screen.findByRole("region", { name: "Native session history" })).toBeInTheDocument();
  act(() => f.emit("room.access.removed"));
  expect(screen.queryByRole("region", { name: "Native session history" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Back to conversation" }));
  await userEvent.click(screen.getByRole("button", { name: "Devices & sessions" }));
  expect(await screen.findByRole("button", { name: /Open Codex/ })).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Native session history" })).not.toBeInTheDocument();
});

it("keeps optional linked work collapsed while native history remains usable", async () => {
  const f = setup();
  render(<RoomPanel {...f.props} />);
  await openSession();
  expect(screen.getByRole("button", { name: "Linked work", expanded: false })).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Issue ID" })).not.toBeInTheDocument();
  expect(await screen.findByRole("region", { name: "Native session history" })).toBeInTheDocument();
});

it("opens resources without changing the controlled message-navigation thread target", async () => {
  const f = setup();
  const targets = vi.fn();
  function ControlledRoom() {
    const [thread, setThread] = useState<string | null>(null);
    return <RoomPanel {...f.props} selectedThreadId={thread} onSelectThread={id => { targets(id); setThread(id); }} />;
  }
  render(<ControlledRoom />);
  await openSession();
  expect(await screen.findByRole("textbox", { name: /Send to Codex/ })).toBeInTheDocument();
  expect(targets).not.toHaveBeenCalledWith("resources");
  expect(f.props.composer.onSubmit).not.toHaveBeenCalled();
});

it("does not grant room mention identity to native text resembling canonical room markup", async () => {
  const f = setup();
  f.client.listLocalSessionEvents.mockResolvedValue({ events: [{ seq: 250, kind: "text", role: "assistant", text: "[@Alice](mention:human:alice)", truncated: false }], nextAfterSeq: null });
  render(<RoomPanel {...f.props} />);
  await openSession();
  const history = screen.getByRole("region", { name: "Native session history" });
  expect(await within(history).findByText("@Alice")).toBeInTheDocument();
  expect(history.querySelector(".room-mention")).toBeNull();
  expect(within(history).queryByRole("link")).not.toBeInTheDocument();
});

it("shows advisory workspace changes, incomplete coverage and overlapping sessions without claiming refresh", async () => {
  const f = setup();
  f.client.listResources.mockResolvedValue({ ...resources, sessions: [{ ...session,
    workspace: { revision: 3, coverage: "bounded", changed: true, overlap: true } }] });
  render(<RoomPanel {...f.props} />);
  await openSession();
  expect(screen.getByText(/Checkout changes detected/)).toBeTruthy();
  expect(screen.getByText(/Multiple sessions may overlap/)).toBeTruthy();
  expect(screen.getByText(/Ignored, sensitive, oversized/)).toBeTruthy();
});

it("applies continuing directory snapshots under frequent hints with bounded request concurrency", async () => {
  const f = setup();
  const pending: Array<(value: RoomResources) => void> = [];
  f.client.listResources.mockImplementation(() => new Promise(resolve => pending.push(resolve)));
  const { result } = renderHook(() => useRoomResources("room", f.source));
  for (let round = 0; round < 4; round++) {
    act(() => { for (let hint = 0; hint < 20; hint++) f.emit("room.resources.changed"); });
    expect(f.client.listResources).toHaveBeenCalledTimes(round + 1);
    await act(async () => pending[round]({ ...resources, sessions: [{ ...session, providerVersion: `snapshot-${round}` }] }));
    expect(result.current.data?.sessions[0].providerVersion).toBe(`snapshot-${round}`);
    expect(result.current.revision).toBe(round + 1);
    expect(f.client.listResources).toHaveBeenCalledTimes(round + 2);
  }
  await act(async () => pending[4](resources));
  expect(result.current.data?.sessions[0].providerVersion).toBe("0.149.0");
  expect(result.current.loading).toBe(false);
  expect(f.client.listResources).toHaveBeenCalledTimes(5);
});

it.each(["old-first", "new-first"])("starts a new authorized epoch without waiting for the old fetch (%s)", async order => {
  const f = setup();
  const pending: Array<(value: RoomResources) => void> = [];
  f.client.listResources.mockImplementation(() => new Promise(resolve => pending.push(resolve)));
  const { result } = renderHook(() => useRoomResources("room", f.source));
  act(() => { f.emit("room.resources.changed"); f.emit("room.metadata.changed"); });
  expect(result.current.data).toBeNull();
  expect(f.client.listResources).toHaveBeenCalledTimes(2);
  act(() => f.emit("room.resources.changed"));
  const authorized = { devices: [], sessions: [], pairedNodes: [] };
  if (order === "old-first") {
    await act(async () => pending[0](resources));
    expect(result.current.data).toBeNull();
    expect(f.client.listResources).toHaveBeenCalledTimes(2);
  }
  await act(async () => pending[1](authorized));
  expect(result.current.data).toEqual(authorized);
  expect(f.client.listResources).toHaveBeenCalledTimes(3);
  if (order === "new-first") {
    await act(async () => pending[0](resources));
    expect(result.current.data).toEqual(authorized);
    expect(result.current.loading).toBe(true);
  }
  await act(async () => pending[2](authorized));
  expect(result.current.loading).toBe(false);
  expect(result.current.revision).toBe(2);
});

it("drops queued refreshes on revocation and unmount without blocking a reopened pane", async () => {
  const f = setup();
  const pending: Array<(value: RoomResources) => void> = [];
  f.client.listResources.mockImplementation(() => new Promise(resolve => pending.push(resolve)));
  const first = renderHook(() => useRoomResources("room", f.source));
  act(() => { f.emit("room.resources.changed"); f.emit("room.access.removed"); });
  await act(async () => pending[0](resources));
  expect(first.result.current.data).toBeNull();
  expect(first.result.current.loading).toBe(false);
  expect(f.client.listResources).toHaveBeenCalledTimes(1);
  first.unmount();
  const second = renderHook(() => useRoomResources("room", f.source));
  act(() => f.emit("room.resources.changed"));
  second.unmount();
  const reopened = renderHook(() => useRoomResources("room", f.source));
  await act(async () => pending[1](resources));
  expect(reopened.result.current.data).toBeNull();
  expect(f.client.listResources).toHaveBeenCalledTimes(3);
  await act(async () => pending[2]({ devices: [], sessions: [], pairedNodes: [] }));
  expect(reopened.result.current.data?.sessions).toEqual([]);
  expect(reopened.result.current.loading).toBe(false);
});

it("groups only authorized resources, targets session input separately, and restores keyboard focus", async () => {
  const f = setup();
  render(<RoomPanel {...f.props} />);
  const trigger = screen.getByRole("button", { name: "Devices & sessions" });
  await userEvent.click(trigger);
  const pane = screen.getByRole("complementary", { name: "Devices & sessions" });
  await within(pane).findByRole("button", { name: /Open Codex/ });
  expect(within(pane).getAllByText("Mac · mac")).toHaveLength(1);
  expect(within(pane).getByText(/Paired CLI endpoint/)).toBeInTheDocument();
  expect(within(pane).queryByText("Outsider")).not.toBeInTheDocument();
  const target = within(pane).getByRole("button", { name: /Open Codex/ });
  act(() => target.focus());
  await userEvent.keyboard("{Enter}");
  expect(await screen.findByText("Native event 250")).toBeInTheDocument();
  expect(within(pane).getByText(/Activity unknown/)).toBeInTheDocument();
  expect(within(pane).getByText(/other eligible conversations/)).toBeInTheDocument();
  await userEvent.type(within(pane).getByRole("textbox", { name: /Send to Codex/ }), "Check concurrency 50");
  await userEvent.click(within(pane).getByRole("button", { name: "Send follow-up" }));
  expect(f.client.submitLocalSessionCommand).toHaveBeenCalledWith("room", "session-123456", expect.objectContaining({ expectedGeneration: "g1", text: "Check concurrency 50", kind: "send" }));
  expect(f.props.composer.onSubmit).not.toHaveBeenCalled();
  expect(screen.getByDisplayValue("Room draft stays here")).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  expect(await screen.findByRole("button", { name: /Open Codex/ })).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(trigger).toHaveFocus();
});

it.each([403, 404])("revokes a stale session tab after a denied follow-up (%s)", async status => {
  const f = setup();
  render(<RoomPanel {...f.props} />);
  await openSession();
  expect(await screen.findByText("Native event 250")).toBeInTheDocument();
  const input = screen.getByRole("textbox", { name: /Send to Codex/ });
  await userEvent.type(input, "Stale tab follow-up");
  expect(input).toHaveValue("Stale tab follow-up");
  f.client.submitLocalSessionCommand.mockRejectedValueOnce(new ApiError(status, {}, "access removed"));

  await userEvent.click(screen.getByRole("button", { name: "Send follow-up" }));
  expect(await screen.findByText(/Access to these resources was removed/)).toBeInTheDocument();
  expect(f.client.submitLocalSessionCommand).toHaveBeenCalledTimes(1);
  expect(f.client.submitLocalSessionCommand).toHaveBeenCalledWith("room", session.sessionId,
    expect.objectContaining({ kind: "send", text: "Stale tab follow-up", expectedGeneration: "g1" }));
  expect(f.client.getLocalSessionCommand).not.toHaveBeenCalled();
  expect(f.client.listLocalSessionEvents).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("region", { name: "Native session history" })).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /Send to Codex/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Open Codex/ })).not.toBeInTheDocument();
  expect(screen.getByDisplayValue("Room draft stays here")).toBeInTheDocument();
});

it("opens recent work, bounds retained history, and makes older and newer regions recoverable", async () => {
  const f = setup();
  render(<RoomPanel {...f.props} />);
  await openSession();
  expect(await screen.findByText("Native event 250")).toBeInTheDocument();
  expect(f.client.listLocalSessionEvents).toHaveBeenCalledWith("room", session.sessionId, 200, 50);
  expect(screen.queryByText("Native event 1")).not.toBeInTheDocument();
  for (const oldest of [151, 101, 51, 1]) {
    await userEvent.click(screen.getByRole("button", { name: "Load older" }));
    expect(await screen.findByText(`Native event ${oldest}`)).toBeInTheDocument();
  }
  expect(screen.getAllByRole("article", { name: /Native event/ })).toHaveLength(200);
  expect(screen.queryByText("Native event 250")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
  expect(await screen.findByText("Native event 250")).toBeInTheDocument();
  expect(screen.getAllByRole("article", { name: /Native event/ })).toHaveLength(50);
});

it("drops a previous room's delayed transcript on room switch", async () => {
  const f = setup();
  let finish!: (p: LocalSessionEventPage) => void;
  f.client.listLocalSessionEvents.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<RoomPanel {...f.props} />);
  await openSession();
  const next = setup("other-room");
  next.client.listResources.mockResolvedValue({ devices: [], sessions: [], pairedNodes: [] });
  view.rerender(<RoomPanel {...next.props} />);
  await act(async () => finish(page(249, 1)));
  expect(screen.queryByText("Native event 250")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Devices & sessions" }));
  expect(await screen.findByText(/No paired devices or shared sessions/)).toBeInTheDocument();
});

it("clears a removed owner's selection, pending input and late history before revalidation", async () => {
  const f = setup();
  render(<RoomPanel {...f.props} />);
  await openSession();
  await screen.findByText("Native event 250");
  await userEvent.type(screen.getByRole("textbox", { name: /Send to Codex/ }), "Do not retain this draft");
  let finish!: (p: RoomResources) => void;
  f.client.listResources.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  act(() => f.emit("room.metadata.changed"));
  expect(screen.queryByText("Native event 250")).not.toBeInTheDocument();
  expect(screen.queryByDisplayValue("Do not retain this draft")).not.toBeInTheDocument();
  await act(async () => finish({ devices: [], sessions: [], pairedNodes: resources.pairedNodes.filter(n => n.humanUserId === "bob") }));
  expect(screen.queryByRole("button", { name: /Open Codex/ })).not.toBeInTheDocument();
  expect(within(screen.getByRole("complementary")).queryByText("Alice")).not.toBeInTheDocument();
});

it("retains immutable command identity for uncertain retries and distinguishes admission from completion", async () => {
  const f = setup();
  f.client.submitLocalSessionCommand.mockRejectedValueOnce(new Error("network"));
  render(<RoomPanel {...f.props} />);
  await openSession();
  const input = screen.getByRole("textbox", { name: /Send to Codex/ });
  await userEvent.type(input, "Original follow-up");
  await userEvent.click(screen.getByRole("button", { name: "Send follow-up" }));
  expect(await screen.findByText(/Delivery unknown/)).toBeInTheDocument();
  expect(input).toBeDisabled();
  const original = f.client.submitLocalSessionCommand.mock.calls[0][2];
  f.client.getLocalSessionCommand.mockResolvedValue({ ...f.receipt(original), state: "applied", held: true });
  await userEvent.click(screen.getByRole("button", { name: "Check delivery" }));
  expect(f.client.submitLocalSessionCommand).toHaveBeenCalledTimes(1);
  expect(await screen.findByText(/Queued or held by the native session/)).toBeInTheDocument();
  expect(screen.getByText(/not completed work/)).toBeInTheDocument();
});

it("retries an uncertain submission with the original request ID and payload", async () => {
  const f = setup();
  f.client.submitLocalSessionCommand.mockRejectedValueOnce(new Error("network"));
  render(<RoomPanel {...f.props} />);
  await openSession();
  await userEvent.type(screen.getByRole("textbox", { name: /Send to Codex/ }), "Repeat safely");
  await userEvent.click(screen.getByRole("button", { name: "Send follow-up" }));
  await userEvent.click(await screen.findByRole("button", { name: "Retry same request" }));
  expect(f.client.submitLocalSessionCommand.mock.calls[1]).toEqual(f.client.submitLocalSessionCommand.mock.calls[0]);
});

it.each([new Error("network"), new ApiError(503, {}, "unavailable")])("preserves an uncertain send through a failing resources poll and recovery (%s)", async error => {
  vi.useFakeTimers();
  const f = setup();
  f.client.submitLocalSessionCommand.mockRejectedValueOnce(new Error("delivery timeout"));
  render(<RoomPanel {...f.props} />);
  await act(async () => fireEvent.click(screen.getByRole("button", {name:"Devices & sessions"})));
  await act(async () => fireEvent.click(screen.getByRole("button", {name:/Open Codex/})));
  const input = screen.getByRole("textbox", {name:/Send to Codex/});
  fireEvent.change(input, {target:{value:"Do not duplicate this follow-up"}});
  await act(async () => fireEvent.click(screen.getByRole("button", {name:"Send follow-up"})));
  const original = f.client.submitLocalSessionCommand.mock.calls[0];
  f.client.listResources.mockRejectedValueOnce(error);
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(screen.getByRole("region", {name:"Native session history"})).toBeInTheDocument();
  expect(screen.getByText(/Could not refresh devices and sessions/)).toBeInTheDocument();
  expect(screen.getByText(/last successful snapshot/i)).toBeInTheDocument();
  expect(input).toHaveValue("Do not duplicate this follow-up");
  expect(screen.getByRole("button", {name:"Retry same request"})).toBeEnabled();
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(screen.queryByText(/Could not refresh devices and sessions/)).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", {name:/Send to Codex/})).toBe(input);
  await act(async () => fireEvent.click(screen.getByRole("button", {name:"Retry same request"})));
  expect(f.client.submitLocalSessionCommand.mock.calls[1]).toEqual(original);
  expect(f.client.submitLocalSessionCommand).toHaveBeenCalledTimes(2);
});

it.each([403,404])("still clears selected uncertain input when resources return %s", async status => {
  const f = setup();
  f.client.submitLocalSessionCommand.mockRejectedValueOnce(new Error("network"));
  render(<RoomPanel {...f.props} />);
  await openSession();
  await userEvent.type(screen.getByRole("textbox", {name:/Send to Codex/}), "Private follow-up");
  await userEvent.click(screen.getByRole("button", {name:"Send follow-up"}));
  f.client.listResources.mockRejectedValueOnce(new ApiError(status, {}, "access removed"));
  act(() => f.emit("room.resources.changed"));
  expect(await screen.findByText(/Access to these resources was removed/)).toBeInTheDocument();
  expect(screen.queryByRole("button", {name:"Retry same request"})).not.toBeInTheDocument();
  expect(screen.queryByDisplayValue("Private follow-up")).not.toBeInTheDocument();
});

it("does not infer approval waiting from a queued native receipt", async () => {
  const f = setup();
  f.client.submitLocalSessionCommand.mockImplementation(async (_room, _session, request) => ({...f.receipt(request), state:"applied", held:true}));
  render(<RoomPanel {...f.props} />);
  await openSession();
  await userEvent.type(screen.getByRole("textbox", {name:/Send to Codex/}), "Queued follow-up");
  await userEvent.click(screen.getByRole("button", {name:"Send follow-up"}));
  expect(await screen.findByText(/Queued or held by the native session/)).toBeInTheDocument();
  expect(screen.queryByText(/approval remains on/)).not.toBeInTheDocument();
});

it("explains and pauses further input for an unknown current-generation outcome", async () => {
  const f = setup();
  f.client.submitLocalSessionCommand.mockImplementation(async (_room, _session, request) => ({...f.receipt(request), state:"unknown", held:true}));
  render(<RoomPanel {...f.props} />);
  await openSession();
  await userEvent.type(screen.getByRole("textbox", {name:/Send to Codex/}), "Possibly admitted");
  await userEvent.click(screen.getByRole("button", {name:"Send follow-up"}));
  expect(await screen.findByText(/further remote input remains paused.*native session restarts/i)).toBeInTheDocument();
  expect(screen.getByText(/restart does not undo/i)).toBeInTheDocument();
  expect(screen.getByRole("textbox", {name:/Send to Codex/})).toBeDisabled();
  expect(screen.getByRole("button", {name:"Check delivery"})).toBeEnabled();
});

it("renders only safe public text and labels native approval and capability limits", async () => {
  const f = setup();
  f.client.listResources.mockResolvedValue({ ...resources, sessions: [{ ...session, status: "waiting-approval", controlUnavailableReason: "unsupported-version", capabilitiesVerified: false }] });
  f.client.listLocalSessionEvents.mockResolvedValue({ events: [{ seq: 250, kind: "tool", role: "tool", text: '<script>alert(1)</script> ![private](https://example.com/private.png) [bad](javascript:alert(1))', truncated: true }], nextAfterSeq: null });
  const { container } = render(<RoomPanel {...f.props} />);
  await openSession();
  expect(await screen.findByText(/Output truncated/)).toBeInTheDocument();
  expect(screen.getByText(/Waiting for approval on/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeDisabled();
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector('img[src="https://example.com/private.png"]')).toBeNull();
  expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
});

it("repairs missed hints by polling only while open and ignores repeated events", async () => {
  vi.useFakeTimers();
  const f = setup();
  render(<RoomPanel {...f.props} />);
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(f.client.listResources).not.toHaveBeenCalled();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Devices & sessions" })));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /Open Codex/ })));
  f.client.listLocalSessionEvents.mockResolvedValue({ events: page(249, 1).events, nextAfterSeq: null });
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(f.client.listResources).toHaveBeenCalledTimes(2);
  expect(screen.getAllByText("Native event 250")).toHaveLength(1);
  await act(async () => { f.emit("room.access.removed"); });
  expect(screen.queryByText("Native event 250")).not.toBeInTheDocument();
  const count = f.client.listResources.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(f.client.listResources).toHaveBeenCalledTimes(count);
});

it("fences late selected-session history and command receipts after access removal", async () => {
  const f = setup();
  let finishHistory!: (p: LocalSessionEventPage) => void;
  let finishSend!: (r: LocalSessionCommandReceipt) => void;
  f.client.listLocalSessionEvents.mockImplementationOnce(() => new Promise(resolve => { finishHistory = resolve; }));
  f.client.submitLocalSessionCommand.mockImplementationOnce(() => new Promise(resolve => { finishSend = resolve; }));
  render(<RoomPanel {...f.props} />);
  await openSession();
  await userEvent.type(screen.getByRole("textbox", { name: /Send to Codex/ }), "Pending instruction");
  await userEvent.click(screen.getByRole("button", { name: "Send follow-up" }));
  const request = f.client.submitLocalSessionCommand.mock.calls[0][2];
  act(() => f.emit("room.access.removed"));
  await act(async () => { finishHistory(page(249, 1)); finishSend({ ...f.receipt(request), state: "applied" }); });
  expect(screen.queryByText("Native event 250")).not.toBeInTheDocument();
  expect(screen.queryByText(/Admitted to the native session/)).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /Send to Codex/ })).not.toBeInTheDocument();
});

it("fails closed on a history permission error and clears selection across removal and re-addition", async () => {
  const f = setup();
  f.client.listLocalSessionEvents.mockRejectedValueOnce(new ApiError(403, {}, "Revoked"));
  render(<RoomPanel {...f.props} />);
  await openSession();
  expect(await screen.findByText(/Access to these resources was removed/)).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /Send to Codex/ })).not.toBeInTheDocument();
});

it("does not reopen a removed session automatically when later hints restore it", async () => {
  const f = setup();
  render(<RoomPanel {...f.props} />);
  await openSession();
  await screen.findByText("Native event 250");
  f.client.listResources.mockResolvedValueOnce({ ...resources, sessions: [] });
  act(() => f.emit("room.resources.changed"));
  await waitFor(() => expect(screen.queryByText("Native event 250")).not.toBeInTheDocument());
  act(() => f.emit("room.resources.changed"));
  expect(await screen.findByRole("button", { name: /Open Codex/ })).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /Send to Codex/ })).not.toBeInTheDocument();
});

it("retries the failed older page rather than silently refreshing a different region", async () => {
  const f = setup();
  render(<RoomPanel {...f.props} />);
  await openSession();
  await screen.findByText("Native event 250");
  f.client.listLocalSessionEvents.mockRejectedValueOnce(new Error("network"));
  await userEvent.click(screen.getByRole("button", { name: "Load older" }));
  await userEvent.click(await screen.findByRole("button", { name: "Retry history" }));
  expect(await screen.findByText("Native event 151")).toBeInTheDocument();
});

it("shows the harness title, keeps the composer, and renders tool rows in the terminal grammar", async () => {
  const f = setup();
  f.client.listResources.mockResolvedValue({ ...resources, sessions: [{ ...session, title: "Retry fix", status: "busy", latestEventSeq: 2 }] });
  f.client.listLocalSessionEvents.mockResolvedValue({ nextAfterSeq: null, events: [
    { seq: 1, kind: "text", role: "user", text: "fix the retry", truncated: false },
    { seq: 2, kind: "tool", role: "assistant", text: "pnpm test", truncated: false, itemId: "e1", tool: { name: "CommandExecution", status: "done", result: { output: "1 passed" }, lines: 1 } },
  ] });
  render(<RoomPanel {...f.props} />);
  await userEvent.click(screen.getByRole("button", { name: "Devices & sessions" }));
  await userEvent.click(await screen.findByRole("button", { name: /Open Codex · Retry fix/ }));
  expect(await screen.findByRole("heading", { name: /Codex · Retry fix/ })).toBeInTheDocument();
  expect(screen.getByText("Ran pnpm test")).toBeInTheDocument();
  expect(screen.getByRole("status", { name: "Working" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Copy session link" })).toBeInTheDocument();
});

it("cannot race a new send with a receipt check or erase the next draft", async () => {
  const f = setup();
  render(<RoomPanel {...f.props} />);
  await openSession();
  const input = screen.getByRole("textbox", { name: /Send to Codex/ });
  await userEvent.type(input, "First follow-up");
  await userEvent.click(screen.getByRole("button", { name: "Send follow-up" }));
  await userEvent.type(input, "Next follow-up");
  let finish!: (r: LocalSessionCommandReceipt) => void;
  f.client.getLocalSessionCommand.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  await userEvent.click(screen.getByRole("button", { name: "Check delivery" }));
  expect(input).toBeDisabled();
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeDisabled();
  await act(async () => finish({ ...f.receipt(f.client.submitLocalSessionCommand.mock.calls[0][2]), state: "applied" }));
  expect(input).toHaveValue("Next follow-up");
  expect(input).toBeEnabled();
});
