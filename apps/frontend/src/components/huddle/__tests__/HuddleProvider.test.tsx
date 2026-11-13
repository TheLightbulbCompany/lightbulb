import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const lk = vi.hoisted(() => {
  const state = {
    handlers: {} as Record<string, (...a: unknown[]) => void>,
    participants: [] as { identity: string; name?: string; isLocal?: boolean }[], attrs: {} as Record<string, string>,
  };
  const room = {
    connect: vi.fn(async () => {}), disconnect: vi.fn(async () => {}),
    on: vi.fn((ev: string, fn: (...a: unknown[]) => void) => { state.handlers[ev] = fn; }),
    localParticipant: { identity: "u1", performRpc: vi.fn(async () => ""), setMicrophoneEnabled: vi.fn(async () => {}) },
  };
  return { state, room, post: vi.fn(), patch: vi.fn(), success: vi.fn(), failure: vi.fn(), client: {} as { post: unknown; patch: unknown } };
});
vi.mock("livekit-client", () => ({ Room: function Room() { return lk.room; }, Track: { Source: { Microphone: "microphone" } }, RoomEvent: { DataReceived: "dataReceived", Disconnected: "disconnected" } }));
vi.mock("@livekit/components-react", () => ({
  RoomAudioRenderer: () => null, RoomContext: { Provider: ({ children }: { children: React.ReactNode }) => <>{children}</> },
  BarVisualizer: ({ state }: { state?: string }) => <span data-testid="bars" data-state={state ?? "all"} />,
  StartAudio: () => null,
  TrackToggle: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) => <button type="button" aria-pressed="true" {...props} />,
  useParticipants: () => lk.state.participants, useIsSpeaking: (p?: unknown) => { if (!p) throw new Error("No participant provided"); return false; }, useTracks: () => [], useMultibandTrackVolume: () => [],
  useParticipantAttributes: () => ({ attributes: lk.state.attrs }),
}));
vi.mock("@/lib/api", () => { lk.client = { post: lk.post, patch: lk.patch }; return { useApi: () => lk.client }; });
vi.mock("@/lib/notify", () => ({ notifySuccess: lk.success, notifyFailure: lk.failure }));
import { HuddleProvider, useHuddle } from "../HuddleProvider";
function Probe() {
  const h = useHuddle();
  return <>
    <button onClick={() => void h.join("r1", "general")}>join {h.roomId ?? "none"}</button>
    <button onClick={() => h.talkStart("a1")}>talk a1</button>
    <button onClick={() => h.talkStart()}>talk anyone</button>
    <button onClick={h.leave}>leave</button>
    <button onClick={h.dismissError}>dismiss</button>
    <span data-testid="error">{h.error?.code ?? ""}</span>
    <span data-testid="notice">{h.agentNotice ? `${h.agentNotice.kind}:${h.agentNotice.agentId}` : ""}</span>
  </>;
}
const mount = () => render(<HuddleProvider><Probe /></HuddleProvider>);
const joinNow = async () => {
  mount();
  fireEvent.click(screen.getByText("join none"));
  await screen.findByText("join r1");
};
const errorCode = () => screen.getByTestId("error").textContent;

beforeEach(() => {
  vi.clearAllMocks(); lk.state.participants = [{ identity: "agent-ceo", name: "Lightbulb" }]; lk.state.attrs = {}; lk.room.localParticipant.identity = "u1";
  lk.post.mockResolvedValue({ serverUrl: "wss://lk", roomName: "huddle-r1", token: "tok", lightbulb: true });
  lk.patch.mockResolvedValue({});
});

it("join posts to the API and connects with the returned url and token", async () => {
  await joinNow();
  expect(lk.post).toHaveBeenCalledWith("/huddles/r1/join", {});
  expect(lk.room.connect).toHaveBeenCalledWith("wss://lk", "tok");
});

it("leave disconnects", async () => {
  await joinNow();
  fireEvent.click(screen.getByText("leave"));
  expect(lk.room.disconnect).toHaveBeenCalled();
  expect(screen.getByText("join none")).toBeInTheDocument();
});

it("unmount disconnects", async () => {
  const { unmount } = mount();
  fireEvent.click(screen.getByText("join none"));
  await waitFor(() => expect(lk.room.connect).toHaveBeenCalled());
  unmount();
  expect(lk.room.disconnect).toHaveBeenCalled();
});

const err = (code: unknown, from: string) =>
  act(() => lk.state.handlers.dataReceived(new TextEncoder().encode(JSON.stringify({ code })), { identity: from }, undefined, "huddle.error"));

it("keeps a huddle.error from agent-ceo until dismissed, and ignores anyone else's", async () => {
  await joinNow();
  err("voice_busy", "user_evil");
  expect(errorCode()).toBe("");
  err("voice_busy", "agent-ceo");
  expect(errorCode()).toBe("voice_busy");
  fireEvent.click(screen.getByText("dismiss"));
  expect(errorCode()).toBe("");
});

it("a failed talk_start says the agent couldn't be reached", async () => {
  await joinNow();
  lk.room.localParticipant.performRpc.mockRejectedValueOnce(new Error("x"));
  fireEvent.click(screen.getByText("talk a1"));
  await waitFor(() => expect(errorCode()).toBe("lightbulb_unreachable"));
});

it("an idle room ends Live and says why", async () => {
  await joinNow();
  err("idle_ended", "agent-ceo");
  expect(lk.room.disconnect).toHaveBeenCalled();
  expect(screen.getByText("join none")).toBeInTheDocument();
  expect(errorCode()).toBe("idle_ended");
});

it.each([
  [{ status: 409, body: { detail: { code: "workspace_not_ready" } } }, "workspace_not_ready"],
  [{ status: 404, body: { code: "ROOM_NOT_FOUND", message: "x" } }, "room_not_found"],
  [Object.assign(new Error("none"), { name: "NotFoundError" }), "no_mic"],
])("a failed join maps %j to %s", async (error, code) => {
  lk.post.mockRejectedValueOnce(error);
  mount();
  fireEvent.click(screen.getByText("join none"));
  await waitFor(() => expect(errorCode()).toBe(code));
});

const notice = (body: unknown, from = "agent-ceo") =>
  act(() => lk.state.handlers.dataReceived(new TextEncoder().encode(typeof body === "string" ? body : JSON.stringify(body)), { identity: from }, undefined, "live.notice"));

it("talkStart sends the agent id as JSON, or an empty payload", async () => {
  await joinNow();
  fireEvent.click(screen.getByText("talk a1"));
  expect(lk.room.localParticipant.performRpc).toHaveBeenCalledWith({ destinationIdentity: "agent-ceo", method: "talk_start", payload: '{"agentId":"a1"}' });
  fireEvent.click(screen.getByText("talk anyone"));
  expect(lk.room.localParticipant.performRpc).toHaveBeenLastCalledWith({ destinationIdentity: "agent-ceo", method: "talk_start", payload: "" });
});

it("a rename by me offers Undo, which PATCHes the old name; by someone else offers none", async () => {
  await joinNow();
  notice({ kind: "renamed", agentId: "a1", from: "CEO", to: "Nova", by: "u1", collides: true });
  const mine = lk.success.mock.calls[0];
  expect(mine[0]).toBe("CEO is now Nova");
  expect(mine[1]).toMatchObject({ dedupeKey: "live-rename-a1", description: "Say 'hey Nova' in Live.", durationMs: 10_000 });
  mine[1].action.onClick();
  expect(lk.patch).toHaveBeenCalledWith("/teams/agents/a1", { name: "CEO" });
  notice({ kind: "renamed", agentId: "a1", from: "CEO", to: "Nova", by: "u2", collides: false });
  expect(lk.success.mock.calls[1][1].action).toBeUndefined();
  expect(lk.success.mock.calls[1][1].description).toBeUndefined();
});

it("a failed Undo says so", async () => {
  lk.patch.mockRejectedValueOnce(new Error("x"));
  await joinNow();
  notice({ kind: "renamed", agentId: "a1", from: "CEO", to: "Nova", by: "u1", collides: false });
  lk.success.mock.calls[0][1].action.onClick();
  await waitFor(() => expect(lk.failure).toHaveBeenCalledWith("Couldn't undo the rename."));
});

it("a saved voice note toasts what the agent will remember", async () => {
  await joinNow();
  notice({ kind: "voice_note_saved", agentId: "a1", name: "Nova", text: "be more blunt", by: "u2" });
  expect(lk.success).toHaveBeenCalledWith("Nova will remember: be more blunt");
});

it("ignores notices from anyone but agent-ceo, and bad JSON", async () => {
  await joinNow();
  notice({ kind: "renamed", agentId: "a1", from: "A", to: "B", by: "u1", collides: false }, "user_evil");
  notice("not json");
  expect(lk.success).not.toHaveBeenCalled();
  expect(screen.getByTestId("notice")).toHaveTextContent("");
});

it("ambiguous_name sets agentNotice and leaving clears it", async () => {
  await joinNow();
  notice({ kind: "ambiguous_name", agentId: "a1" });
  expect(screen.getByTestId("notice")).toHaveTextContent("ambiguous_name:a1");
  fireEvent.click(screen.getByText("leave"));
  expect(screen.getByTestId("notice")).toHaveTextContent("");
});

// Privacy (tap to talk, 8 Nov): the orb's pre-connect joins with { mic: false } and must publish no audio at all,
// not a published track that the UI only hides. No setMicrophoneEnabled(true) means no capture and no publication.
it("a join with the mic off never opens or publishes the microphone", async () => {
  function Pre() { const h = useHuddle(); return <button onClick={() => void h.join("r1", "general", false)}>pre {h.roomId ?? "none"}</button>; }
  render(<HuddleProvider><Pre /></HuddleProvider>);
  fireEvent.click(screen.getByText("pre none"));
  await screen.findByText("pre r1");
  expect(lk.room.connect).toHaveBeenCalled();
  expect(lk.room.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
});

it("quiet_ended from the worker closes the mic and keeps the room", async () => {
  await joinNow();
  lk.room.localParticipant.setMicrophoneEnabled.mockClear();
  act(() => lk.state.handlers.dataReceived(new TextEncoder().encode(JSON.stringify({ code: "quiet_ended" })), { identity: "agent-ceo" }, undefined, "huddle.error"));
  expect(lk.room.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(false);
  expect(lk.room.disconnect).not.toHaveBeenCalled();
});
