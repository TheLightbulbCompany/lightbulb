import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const lk = vi.hoisted(() => {
  const state = {
    handlers: {} as Record<string, (...a: unknown[]) => void>, voiceActive: false,
    participants: [] as { identity: string; name?: string }[], attrs: {} as Record<string, string>,
  };
  const room = {
    connect: vi.fn(async () => {}), disconnect: vi.fn(async () => {}),
    on: vi.fn((ev: string, fn: (...a: unknown[]) => void) => { state.handlers[ev] = fn; }),
    localParticipant: { performRpc: vi.fn(async () => ""), setMicrophoneEnabled: vi.fn(async () => {}) },
  };
  return { state, room, post: vi.fn(), client: {} as { post: unknown } };
});
vi.mock("livekit-client", () => ({ Room: function Room() { return lk.room; }, Track: { Source: { Microphone: "microphone" } }, RoomEvent: { DataReceived: "dataReceived", Disconnected: "disconnected" } }));
vi.mock("@livekit/components-react", () => ({
  RoomAudioRenderer: () => null, RoomContext: { Provider: ({ children }: { children: React.ReactNode }) => <>{children}</> },
  useParticipants: () => lk.state.participants, useIsSpeaking: (p?: unknown) => { if (!p) throw new Error("No participant provided"); return false; }, useTracks: () => [], useMultibandTrackVolume: () => [],
  useParticipantAttributes: () => ({ attributes: lk.state.attrs }),
}));
vi.mock("@/lib/api", () => { lk.client = { post: lk.post }; return { useApi: () => lk.client }; });
vi.mock("@/components/voice/VoiceCallProvider", () => ({ useVoiceCall: () => ({ active: lk.state.voiceActive }) }));
import { HuddleProvider, useHuddle } from "../HuddleProvider";
import { HuddleBar, huddleStatus } from "../HuddleBar";

function Probe() {
  const h = useHuddle();
  return <button onClick={() => void h.join("r1", "general")}>join {h.roomId ?? "none"}</button>;
}
const mount = () => render(<HuddleProvider><Probe /><HuddleBar /></HuddleProvider>);
const joinNow = async () => {
  mount();
  fireEvent.click(screen.getByText("join none"));
  await screen.findByRole("region", { name: /Live in general/ });
};

beforeEach(() => {
  window.matchMedia = (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
  vi.clearAllMocks(); lk.state.voiceActive = false; lk.state.participants = [{ identity: "agent-ceo", name: "Lightbulb" }]; lk.state.attrs = {};
  lk.post.mockResolvedValue({ serverUrl: "wss://lk", roomName: "huddle-r1", token: "tok" });
});

it("join posts to the API and connects with the returned url and token", async () => {
  await joinNow();
  expect(lk.post).toHaveBeenCalledWith("/huddles/r1/join", {});
  expect(lk.room.connect).toHaveBeenCalledWith("wss://lk", "tok");
});

it("the bar says it is a live voice call, with a labelled mute control", async () => {
  await joinNow();
  expect(screen.getByText("🔴 Live")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Mute" })).toHaveTextContent("Mute");
});

it("leave disconnects", async () => {
  await joinNow();
  fireEvent.click(screen.getByRole("button", { name: "Leave Live" }));
  expect(lk.room.disconnect).toHaveBeenCalled();
  expect(screen.queryByRole("region")).toBeNull();
});

it("unmount disconnects", async () => {
  const { unmount } = render(<HuddleProvider><Probe /></HuddleProvider>);
  fireEvent.click(screen.getByText("join none"));
  await waitFor(() => expect(lk.room.connect).toHaveBeenCalled());
  unmount();
  expect(lk.room.disconnect).toHaveBeenCalled();
});

it("holding Talk sends talk_start and talk_end to agent-ceo", async () => {
  await joinNow();
  const talk = screen.getByRole("button", { name: "Talk to Lightbulb" });
  const now = vi.spyOn(Date, "now").mockReturnValue(1000);
  fireEvent.pointerDown(talk, { button: 0 });
  expect(lk.room.localParticipant.performRpc).toHaveBeenCalledWith({ destinationIdentity: "agent-ceo", method: "talk_start", payload: "" });
  now.mockReturnValue(1600); // held past the tap window
  fireEvent.pointerUp(talk);
  now.mockRestore();
  expect(lk.room.localParticipant.performRpc).toHaveBeenLastCalledWith({ destinationIdentity: "agent-ceo", method: "talk_end", payload: "" });
});

it("Space does the same, once despite key repeat", async () => {
  await joinNow();
  const talk = screen.getByRole("button", { name: "Talk to Lightbulb" });
  fireEvent.keyDown(talk, { key: " " });
  fireEvent.keyDown(talk, { key: " ", repeat: true });
  fireEvent.keyUp(talk, { key: " " });
  const methods = (lk.room.localParticipant.performRpc.mock.calls as unknown as { method: string }[][]).map((c) => c[0].method);
  expect(methods).toEqual(["talk_start", "talk_end"]);
});

it("shows the plain-word message for a huddle.error", async () => {
  await joinNow();
  act(() => lk.state.handlers.dataReceived(new TextEncoder().encode(JSON.stringify({ code: "voice_busy", reason: "x" })), { identity: "agent-ceo" }, undefined, "huddle.error"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Someone in your workspace is already on a free call.");
});

it("a call starting ends the huddle", async () => {
  const { rerender } = render(<HuddleProvider><Probe /></HuddleProvider>);
  fireEvent.click(screen.getByText("join none"));
  await screen.findByText("join r1");
  lk.state.voiceActive = true;
  rerender(<HuddleProvider><Probe /></HuddleProvider>);
  await screen.findByText("join none");
  expect(lk.room.disconnect).toHaveBeenCalled();
});

const methods = () => (lk.room.localParticipant.performRpc.mock.calls as unknown as { method: string }[][]).map((c) => c[0].method);
const err = (code: unknown, from: string) =>
  act(() => lk.state.handlers.dataReceived(new TextEncoder().encode(JSON.stringify({ code })), { identity: from }, undefined, "huddle.error"));

it("ignores a huddle.error from anyone but agent-ceo, and a __proto__ code falls back", async () => {
  await joinNow();
  err("voice_busy", "user_evil");
  expect(screen.queryByRole("alert")).toBeNull();
  err("__proto__", "agent-ceo");
  expect(await screen.findByRole("alert")).toHaveTextContent("Live couldn't start.");
});

it("a failed talk_start resets talking and says Lightbulb couldn't be reached", async () => {
  await joinNow();
  lk.room.localParticipant.performRpc.mockRejectedValueOnce(new Error("x"));
  const talk = screen.getByRole("button", { name: "Talk to Lightbulb" });
  fireEvent.pointerDown(talk, { button: 0 });
  await waitFor(() => expect(talk).toHaveTextContent("Talk to Lightbulb"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't reach Lightbulb — try again.");
});

it("a join refused with workspace_not_ready says so", async () => {
  lk.post.mockRejectedValueOnce({ status: 409, body: { detail: { code: "workspace_not_ready" } } });
  mount();
  fireEvent.click(screen.getByText("join none"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Your workspace is still getting ready.");
});

it("the Lightbulb tile's status comes from the worker's attributes", async () => {
  lk.state.participants = [{ identity: "agent-ceo", name: "Lightbulb" }];
  lk.state.attrs = { "lightbulb.status": "working", "lightbulb.working": "Gauge — checking Stripe" };
  await joinNow();
  expect(screen.getByText("Gauge is working: checking Stripe")).toBeInTheDocument();
});

it.each([
  [undefined, "Following along"],
  [{ "lightbulb.status": "following" }, "Following along"],
  [{ "lightbulb.status": "listening", "lightbulb.speaker": "Aden" }, "Listening to Aden"],
  [{ "lightbulb.status": "thinking" }, "Thinking…"],
  [{ "lightbulb.status": "speaking" }, "Speaking"],
  [{ "lightbulb.status": "working", "lightbulb.working": "Gauge — checking Stripe" }, "Gauge is working: checking Stripe"],
])("huddleStatus(%j)", (attrs, text) => {
  expect(huddleStatus(attrs)).toBe(text);
});

it("join does nothing while a call is active", async () => {
  lk.state.voiceActive = true;
  render(<HuddleProvider><Probe /></HuddleProvider>);
  fireEvent.click(screen.getByText("join none"));
  expect(lk.post).not.toHaveBeenCalled();
});

it("ignores a right-click, and pointer cancel sends talk_end", async () => {
  await joinNow();
  const talk = screen.getByRole("button", { name: "Talk to Lightbulb" });
  fireEvent.pointerDown(talk, { button: 2 });
  expect(methods()).toEqual([]);
  fireEvent.pointerDown(talk, { button: 0 });
  fireEvent.pointerCancel(talk);
  expect(methods()).toEqual(["talk_start", "talk_end"]);
});

it("a quick click toggles talking on, the next click off", async () => {
  await joinNow();
  const talk = screen.getByRole("button", { name: "Talk to Lightbulb" });
  fireEvent.pointerDown(talk, { button: 0 }); fireEvent.pointerUp(talk);
  expect(methods()).toEqual(["talk_start"]);
  fireEvent.pointerDown(talk, { button: 0 }); fireEvent.pointerUp(talk);
  expect(methods()).toEqual(["talk_start", "talk_end"]);
});

it("a tap toggles: the label says Stop talking until the next tap", async () => {
  await joinNow();
  const talk = screen.getByRole("button", { name: "Talk to Lightbulb" });
  fireEvent.pointerDown(talk, { button: 0 }); fireEvent.pointerUp(talk);
  expect(talk).toHaveTextContent("Stop talking");
  fireEvent.pointerDown(talk, { button: 0 }); fireEvent.pointerUp(talk);
  expect(talk).toHaveTextContent("Talk to Lightbulb");
});

it("Talk is disabled until Lightbulb has joined, then gives up after 45 s with a Retry that joins again", async () => {
  lk.state.participants = [];
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    await joinNow();
    expect(screen.getByRole("button", { name: "Talk to Lightbulb" })).toBeDisabled();
    expect(screen.getByText("Lightbulb is joining…")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(45_000); });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(lk.post).toHaveBeenCalledTimes(2));
    expect(lk.post).toHaveBeenLastCalledWith("/huddles/r1/join", {});
  } finally {
    vi.useRealTimers();
  }
});

it("a refused summon turns a tapped-on Talk back off", async () => {
  await joinNow();
  const talk = screen.getByRole("button", { name: "Talk to Lightbulb" });
  fireEvent.pointerDown(talk, { button: 0 }); fireEvent.pointerUp(talk);
  expect(talk).toHaveTextContent("Stop talking");
  err("voice_busy", "agent-ceo");
  expect(talk).toHaveTextContent("Talk to Lightbulb");
});

it("an idle room ends Live and says why", async () => {
  await joinNow();
  err("idle_ended", "agent-ceo");
  expect(lk.room.disconnect).toHaveBeenCalled();
  expect(screen.queryByRole("region")).toBeNull();
  expect(await screen.findByRole("alert")).toHaveTextContent("Live ended — no one was talking.");
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
