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
  return { state, room, post: vi.fn(), patch: vi.fn(), success: vi.fn(), failure: vi.fn(), nav: vi.fn(), client: {} as { post: unknown; patch: unknown } };
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
vi.mock("@/lib/shellNav", () => ({ navigateShell: lk.nav }));
import { HuddleProvider, useHuddle } from "../HuddleProvider";
import { LiveStrip } from "../LiveStrip";

function Probe() {
  const h = useHuddle();
  return <>
    <button onClick={() => void h.join("r1", "general")}>join {h.roomId ?? "none"}</button>
    <button onClick={() => h.talkStart("a1")}>talk a1</button>
    <button onClick={() => h.talkStart()}>talk anyone</button>
    <span data-testid="notice">{h.agentNotice ? `${h.agentNotice.kind}:${h.agentNotice.agentId}` : ""}</span>
  </>;
}
const members = [{ kind: "agent", id: "a1", displayName: "Nova", listenMode: "active" }] as never;
const mount = (here = "r1") => render(<HuddleProvider><Probe /><LiveStrip roomId={here} members={members} /></HuddleProvider>);
const joinNow = async (here?: string) => {
  mount(here);
  fireEvent.click(screen.getByText("join none"));
  await screen.findByRole("group", { name: "Live in general" });
};
const openDetails = () => fireEvent.click(screen.getByRole("button", { name: /Details$/ }));

beforeEach(() => {
  window.matchMedia = (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
  vi.clearAllMocks(); lk.state.participants = [{ identity: "agent-ceo", name: "Lightbulb" }]; lk.state.attrs = {}; lk.room.localParticipant.identity = "u1";
  lk.post.mockResolvedValue({ serverUrl: "wss://lk", roomName: "huddle-r1", token: "tok", lightbulb: true });
  lk.patch.mockResolvedValue({});
});

it("join posts to the API and connects with the returned url and token", async () => {
  await joinNow();
  expect(lk.post).toHaveBeenCalledWith("/huddles/r1/join", {});
  expect(lk.room.connect).toHaveBeenCalledWith("wss://lk", "tok");
});

it("the header strip says Live and carries the mic toggle and Leave", async () => {
  await joinNow();
  expect(screen.getByText("Live")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Microphone" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Leave Live" })).toBeInTheDocument();
});

it("leave disconnects and the strip goes", async () => {
  await joinNow();
  fireEvent.click(screen.getByRole("button", { name: "Leave Live" }));
  expect(lk.room.disconnect).toHaveBeenCalled();
  expect(screen.queryByRole("group")).toBeNull();
});

it("unmount disconnects", async () => {
  const { unmount } = render(<HuddleProvider><Probe /></HuddleProvider>);
  fireEvent.click(screen.getByText("join none"));
  await waitFor(() => expect(lk.room.connect).toHaveBeenCalled());
  unmount();
  expect(lk.room.disconnect).toHaveBeenCalled();
});

const methods = () => (lk.room.localParticipant.performRpc.mock.calls as unknown as { method: string }[][]).map((c) => c[0].method);
const err = (code: unknown, from: string) =>
  act(() => lk.state.handlers.dataReceived(new TextEncoder().encode(JSON.stringify({ code })), { identity: from }, undefined, "huddle.error"));

it("tapping an agent's tile sends talk_start with its id to agent-ceo, once per click, and never talk_end", async () => {
  await joinNow();
  const talk = screen.getByRole("button", { name: "Nova, agent" });
  fireEvent.click(talk);
  expect(lk.room.localParticipant.performRpc).toHaveBeenCalledWith({ destinationIdentity: "agent-ceo", method: "talk_start", payload: '{"agentId":"a1"}' });
  fireEvent.click(talk);
  expect(methods()).toEqual(["talk_start", "talk_start"]);
});

it("shows the plain-word message for a huddle.error, opening the details", async () => {
  await joinNow();
  act(() => lk.state.handlers.dataReceived(new TextEncoder().encode(JSON.stringify({ code: "voice_busy", reason: "x" })), { identity: "agent-ceo" }, undefined, "huddle.error"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Someone in your workspace is already on a free call.");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByRole("alert")).toBeNull();
});

it("ignores a huddle.error from anyone but agent-ceo, and a __proto__ code falls back", async () => {
  await joinNow();
  err("voice_busy", "user_evil");
  expect(screen.queryByRole("alert")).toBeNull();
  err("__proto__", "agent-ceo");
  expect(await screen.findByRole("alert")).toHaveTextContent("Live couldn't start.");
});

it("a failed talk_start says the agent couldn't be reached", async () => {
  await joinNow();
  lk.room.localParticipant.performRpc.mockRejectedValueOnce(new Error("x"));
  fireEvent.click(screen.getByRole("button", { name: "Nova, agent" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/Couldn't reach/);
});

it("a join refused with workspace_not_ready says so in the header", async () => {
  lk.post.mockRejectedValueOnce({ status: 409, body: { detail: { code: "workspace_not_ready" } } });
  mount();
  fireEvent.click(screen.getByText("join none"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Your workspace is still getting ready.");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByRole("alert")).toBeNull();
});

it("the details list everyone here", async () => {
  lk.state.participants = [{ identity: "agent-ceo", name: "Lightbulb" }, { identity: "u1", name: "Ana", isLocal: true }, { identity: "u2", name: "Ben" }];
  await joinNow();
  openDetails();
  const list = await screen.findByRole("list", { name: "People in Live" });
  expect(list).toHaveTextContent("Ana (you)");
  expect(list).toHaveTextContent("Ben");
});

it("more than three people collapse into +N", async () => {
  lk.state.participants = [{ identity: "agent-ceo" }, ...["a", "b", "c", "d", "e"].map((n) => ({ identity: n, name: n.toUpperCase() }))];
  await joinNow();
  expect(screen.getByText("+2")).toBeInTheDocument();
});

it("a people-only room shows no Lightbulb: no bars, no joining, no 45 s Retry", async () => {
  lk.state.participants = [{ identity: "u1", name: "Ana", isLocal: true }];
  lk.post.mockResolvedValue({ serverUrl: "wss://lk", roomName: "huddle-r1", token: "tok", lightbulb: false });
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    await joinNow();
    expect(screen.queryByTestId("bars")).toBeNull();
    expect(screen.queryByRole("group", { name: "Agents in Live" })).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(45_000); });
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    openDetails();
    expect(await screen.findByRole("list", { name: "People in Live" })).toHaveTextContent("Ana (you)");
    expect(screen.queryByText(/tap an agent/)).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

it("agents can't be tapped until the worker joins, then it gives up after 45 s with a Retry that joins again", async () => {
  lk.state.participants = [];
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    await joinNow();
    expect(screen.getByRole("button", { name: "Nova, agent" })).toBeDisabled();
    await act(async () => { await vi.advanceTimersByTimeAsync(45_000); });
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(lk.post).toHaveBeenCalledTimes(2));
    expect(lk.post).toHaveBeenLastCalledWith("/huddles/r1/join", {});
  } finally {
    vi.useRealTimers();
  }
});

it("an idle room ends Live and says why", async () => {
  await joinNow();
  err("idle_ended", "agent-ceo");
  expect(lk.room.disconnect).toHaveBeenCalled();
  expect(screen.queryByRole("group")).toBeNull();
  expect(await screen.findByRole("alert")).toHaveTextContent("Live ended — no one was talking.");
});

it("in another room's header it is a compact Live in general with Return and Leave", async () => {
  const seen: unknown[] = [];
  const onOpen = (e: Event) => seen.push((e as CustomEvent).detail);
  window.addEventListener("openRoom", onOpen);
  try {
    await joinNow("r2");
    expect(screen.queryByRole("group", { name: "Agents in Live" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Return/ }));
    expect(seen).toEqual([{ roomId: "r1", threadId: null }]);
    fireEvent.click(screen.getByRole("button", { name: "Leave Live" }));
    expect(lk.room.disconnect).toHaveBeenCalled();
  } finally {
    window.removeEventListener("openRoom", onOpen);
  }
});

it.each([
  [{ status: 404, body: { code: "ROOM_NOT_FOUND", message: "x" } }, "This channel no longer exists."],
  [Object.assign(new Error("none"), { name: "NotFoundError" }), "No microphone found."],
])("a failed join maps %j to plain words", async (error, text) => {
  lk.post.mockRejectedValueOnce(error);
  mount();
  fireEvent.click(screen.getByText("join none"));
  expect(await screen.findByRole("alert")).toHaveTextContent(text);
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

it("a saved voice note toasts with Edit, which opens the agent inspector", async () => {
  await joinNow();
  notice({ kind: "voice_note_saved", agentId: "a1", name: "Nova", text: "be more blunt", by: "u2" });
  expect(lk.success.mock.calls[0][0]).toBe("Nova will remember: be more blunt");
  lk.success.mock.calls[0][1].action.onClick();
  expect(lk.nav).toHaveBeenCalledWith("/chat?panel=agent-inspector&agent=a1");
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
  fireEvent.click(screen.getByRole("button", { name: "Leave Live" }));
  expect(screen.getByTestId("notice")).toHaveTextContent("");
});
