import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const lk = vi.hoisted(() => ({ attrs: {} as Record<string, string>, agent: null as object | null, lines: [] as { text: string }[] }));
vi.mock("@livekit/components-react", () => ({
  RoomContext: { Provider: ({ children }: { children: React.ReactNode }) => <>{children}</> },
  useRemoteParticipant: () => lk.agent,
  useParticipantAttributes: () => ({ attributes: lk.attrs }),
  useTranscriptions: () => lk.lines,
  useLocalParticipant: () => ({ localParticipant: {}, microphoneTrack: undefined }),
  useTrackVolume: () => 0,
}));
const mic = vi.hoisted(() => vi.fn());
const rpc = vi.hoisted(() => vi.fn(async () => "ok"));
const connectedRoom = vi.hoisted(() => ({ localParticipant: { setMicrophoneEnabled: mic, performRpc: rpc, isMicrophoneEnabled: false } }));
const huddle = vi.hoisted(() => ({
  roomId: null as string | null, roomName: null as string | null, joining: null as string | null, room: null as object | null,
  error: null as { code: string } | null, join: vi.fn(), leave: vi.fn(), dismissError: vi.fn(),
}));
vi.mock("@/components/huddle/HuddleProvider", () => ({ CEO_IDENTITY: "agent-ceo", useHuddle: () => huddle }));
vi.mock("@/components/huddle/useAssistantDm", () => ({
  useAssistantDm: () => ({ roomId: "dm1", kind: "dm", members: [{ kind: "human", id: "u" }, { kind: "agent", id: "a1" }] }),
}));
const questions = vi.hoisted(() => ({ data: [] as object[] }));
vi.mock("../data", () => ({ useQuestions: () => questions }));

import { HOLD_MS, TALK_KEY, TalkOrb } from "../TalkOrb";
import { TalkSessionProvider } from "../TalkSessionProvider";

// The browser's mic permission (jsdom has no Permissions API): "prompt" unless a test grants it.
const perm = { state: "prompt" };
Object.defineProperty(navigator, "permissions", { configurable: true, value: { query: () => Promise.resolve(perm) } });

let now = 1_000;
beforeEach(() => {
  lk.attrs = {}; lk.agent = null; lk.lines = []; questions.data = [];
  Object.assign(huddle, { roomId: null, roomName: null, joining: null, room: null, error: null });
  huddle.join.mockReset(); huddle.leave.mockClear(); mic.mockClear(); rpc.mockClear(); perm.state = "prompt"; connectedRoom.localParticipant.isMicrophoneEnabled = false;
  vi.spyOn(Date, "now").mockImplementation(() => now);
});
afterEach(() => vi.restoreAllMocks());

const live = () => { huddle.roomId = "dm1"; huddle.roomName = "Nova"; huddle.room = connectedRoom; lk.agent = { identity: "agent-ceo" }; };
const listening = () => { live(); lk.attrs = { "lightbulb.agent.a1.status": "listening" }; };
const settle = () => act(async () => {}); // lets the permission check and anything it started finish
const orb = () => screen.getByRole("button", { name: "Talk to Nova" });
const tap = (el = orb()) => { fireEvent.keyDown(el, { key: " " }); now += 120; fireEvent.keyUp(el, { key: " " }); };
const hold = (el = orb()) => { fireEvent.keyDown(el, { key: " " }); now += HOLD_MS + 50; fireEvent.keyUp(el, { key: " " }); };
const status = () => screen.getByRole("status");

it("a tap starts a hands-free talk: the mic opens and stays open after the release", () => {
  listening();
  render(<TalkOrb name="Nova" />);
  expect(orb()).toHaveAttribute("aria-pressed", "false");
  tap();
  expect(mic).toHaveBeenCalledWith(true);
  expect(mic).not.toHaveBeenCalledWith(false);
  expect(orb()).toHaveAttribute("aria-pressed", "true");
  expect(orb()).toHaveAttribute("data-state", "listening");
  expect(status()).toHaveTextContent("Listening");
  expect(status()).toHaveAttribute("aria-live", "polite");
});

it("a hold past 350 ms closes only input on release and keeps the conversation open", () => {
  listening();
  render(<TalkOrb name="Nova" />);
  fireEvent.keyDown(orb(), { key: " " });
  expect(mic).toHaveBeenCalledWith(true); // open on the press, so the first word is kept
  now += HOLD_MS + 50;
  fireEvent.keyUp(orb(), { key: " " });
  expect(mic).toHaveBeenLastCalledWith(false);
  expect(orb()).toHaveAttribute("aria-pressed", "false");
  expect(orb()).toHaveAttribute("data-state", "muted");
  expect(screen.getByRole("button", { name: "End" })).toBeInTheDocument();
  expect(huddle.leave).not.toHaveBeenCalled();
});

it("a tap while the voice speaks toggles only input, preserving playback and work", () => {
  listening();
  const { rerender } = render(<TalkOrb name="Nova" />);
  tap();
  lk.attrs = { "lightbulb.agent.a1.status": "speaking" };
  lk.lines = [{ text: "The complete answer keeps playing." }];
  rerender(<TalkOrb name="Nova" />);
  tap();
  expect(mic).toHaveBeenLastCalledWith(false);
  expect(rpc).not.toHaveBeenCalled();
  expect(huddle.leave).not.toHaveBeenCalled();
  expect(status()).toHaveTextContent("Nova is speaking");
  expect(screen.getByText("The complete answer keeps playing.")).toBeInTheDocument();
  tap();
  expect(mic).toHaveBeenLastCalledWith(true);
  expect(rpc).not.toHaveBeenCalled();
  expect(huddle.leave).not.toHaveBeenCalled();
});


it("Mute and End are their own controls, and Esc ends", () => {
  listening();
  render(<TalkOrb name="Nova" />);
  tap();
  fireEvent.click(screen.getByRole("button", { name: "Mute" }));
  expect(mic).toHaveBeenLastCalledWith(false);
  expect(orb()).toHaveAttribute("data-state", "muted");
  expect(status()).toHaveTextContent("Muted");
  expect(orb()).toHaveAttribute("aria-pressed", "false"); // input is off; the conversation remains open
  fireEvent.click(screen.getByRole("button", { name: "Unmute" }));
  expect(mic).toHaveBeenLastCalledWith(true);
  fireEvent.click(screen.getByRole("button", { name: "End" }));
  expect(orb()).toHaveAttribute("data-state", "idle");
  expect(huddle.leave).toHaveBeenCalledTimes(1);
  tap();
  huddle.leave.mockClear();
  mic.mockClear();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(mic).toHaveBeenCalledWith(false);
  expect(orb()).toHaveAttribute("aria-pressed", "false");
  expect(huddle.leave).toHaveBeenCalledTimes(1);
});

it("each state has its own words: thinking, working names the tool, needs you", () => {
  listening();
  const { rerender } = render(<TalkOrb name="Nova" />);
  tap();
  lk.attrs = { "lightbulb.agent.a1.status": "working" };
  rerender(<TalkOrb name="Nova" />);
  expect(orb()).toHaveAttribute("data-state", "thinking");
  expect(status()).toHaveTextContent("Thinking…");
  lk.attrs = { "lightbulb.agent.a1.status": "working", "lightbulb.agent.a1.task": "Checking gmail" };
  rerender(<TalkOrb name="Nova" />);
  expect(orb()).toHaveAttribute("data-state", "working");
  expect(status()).toHaveTextContent("Checking gmail");
  lk.attrs = { "lightbulb.agent.a1.status": "listening" }; questions.data = [{}, {}];
  rerender(<TalkOrb name="Nova" />);
  expect(orb()).toHaveAttribute("data-state", "needs");
  expect(status()).toHaveTextContent("Listening · 2 need you on screen");
  lk.lines = [{ text: "Priya moved to 11:30." }];
  rerender(<TalkOrb name="Nova" />);
  expect(screen.getByText("Priya moved to 11:30.")).toHaveAttribute("aria-live", "polite"); // captions on
});

it("quiet_ended from the worker ends the talk with a polite line, not an alert", () => {
  listening();
  const { rerender } = render(<TalkOrb name="Nova" />);
  tap();
  huddle.error = { code: "quiet_ended" };
  rerender(<TalkOrb name="Nova" />);
  expect(orb()).toHaveAttribute("data-state", "idle");
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText(/Ended after a quiet moment. Tasks keep running/)).toBeInTheDocument();
  huddle.error = null;
  tap();
  expect(huddle.dismissError).toHaveBeenCalled();
});

it("the Mac app's talk key taps and holds like the orb", () => {
  listening();
  render(<TalkOrb name="Nova" />);
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  now += 100;
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "up" })); });
  expect(orb()).toHaveAttribute("aria-pressed", "true");
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  now += 100;
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "up" })); });
  expect(orb()).toHaveAttribute("aria-pressed", "false");
});

it("a talk key cancel (Fn was part of a chord) undoes the press", () => {
  listening();
  render(<TalkOrb name="Nova" />);
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  expect(mic).toHaveBeenCalledWith(true);
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "cancel" })); });
  expect(mic).toHaveBeenLastCalledWith(false);
  expect(orb()).toHaveAttribute("aria-pressed", "false");
});

it("mounted into an open mic (moving between places), the talk is still on", () => {
  listening(); connectedRoom.localParticipant.isMicrophoneEnabled = true;
  render(<TalkOrb name="Nova" compact />);
  expect(orb()).toHaveAttribute("data-state", "listening");
  expect(screen.getByRole("button", { name: "End" })).toBeInTheDocument();
});

it("never mutes, nor shows the error of, a Live it did not start", () => {
  huddle.roomId = "c1"; huddle.roomName = "#general"; huddle.room = connectedRoom; huddle.error = { code: "connection_lost" };
  render(<TalkOrb name="Nova" compact />);
  expect(mic).not.toHaveBeenCalled();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(orb()).toBeDisabled();
});

it("a cold tap joins with the mic and shows connecting until the agent listens", () => {
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  tap();
  expect(huddle.join).toHaveBeenCalledWith("dm1", "Nova");
  huddle.joining = "dm1";
  rerender(<TalkOrb name="Nova" compact />);
  expect(orb()).toHaveAttribute("data-state", "connecting");
  huddle.joining = null; live(); connectedRoom.localParticipant.isMicrophoneEnabled = true; // the join opened it
  rerender(<TalkOrb name="Nova" compact />);
  expect(mic).not.toHaveBeenCalledWith(false); // a tap: the mic stays open
  lk.attrs = { "lightbulb.agent.a1.status": "listening" };
  rerender(<TalkOrb name="Nova" compact />);
  expect(orb()).toHaveAttribute("data-state", "listening");
});

it("a cold hold released before the room connects closes the mic the join opened", () => {
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  hold();
  huddle.joining = null; live(); connectedRoom.localParticipant.isMicrophoneEnabled = true;
  rerender(<TalkOrb name="Nova" compact />);
  expect(mic).toHaveBeenCalledWith(false);
});

// Privacy (8 Nov): the pre-connected room publishes NO audio before the first press. The join asks for no mic
// (HuddleProvider then never calls setMicrophoneEnabled, see its test), and the orb never opens it on connect.
it("pre-connects with the mic never opened, once per mount, looking idle, until a tap opens it", async () => {
  perm.state = "granted";
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  await waitFor(() => expect(huddle.join).toHaveBeenCalledWith("dm1", "Nova", false));
  huddle.joining = "dm1";
  rerender(<TalkOrb name="Nova" compact />);
  expect(orb()).toHaveAttribute("data-state", "idle");
  huddle.joining = null; live();
  rerender(<TalkOrb name="Nova" compact />);
  expect(mic).not.toHaveBeenCalled(); // no capture, no publication
  expect(orb()).toHaveAttribute("data-state", "idle");
  expect(screen.queryByRole("button", { name: "End" })).toBeNull();
  rerender(<TalkOrb name="Nova Prime" compact />);
  await settle();
  expect(huddle.join).toHaveBeenCalledTimes(1);
  lk.attrs = { "lightbulb.agent.a1.status": "listening" };
  tap(screen.getByRole("button", { name: "Talk to Nova Prime" }));
  expect(huddle.join).toHaveBeenCalledTimes(1); // the first tap on a warm room only opens the mic
  expect(mic).toHaveBeenCalledWith(true);
});

it("a tap while the pre-connect is still connecting opens the mic when it lands", async () => {
  perm.state = "granted";
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  await waitFor(() => expect(huddle.join).toHaveBeenCalledTimes(1));
  huddle.joining = "dm1";
  rerender(<TalkOrb name="Nova" compact />);
  tap();
  expect(huddle.join).toHaveBeenCalledTimes(1);
  expect(orb()).toHaveAttribute("data-state", "connecting");
  huddle.joining = null; live();
  rerender(<TalkOrb name="Nova" compact />);
  expect(mic).toHaveBeenCalledWith(true);
});

it("does not pre-connect while the browser has not granted the mic: the first press asks", async () => {
  render(<TalkOrb name="Nova" compact />);
  await settle();
  expect(huddle.join).not.toHaveBeenCalled();
  fireEvent.keyDown(orb(), { key: " " });
  expect(huddle.join).toHaveBeenCalledWith("dm1", "Nova");
});

it("does not pre-connect into a Live that is already on", async () => {
  perm.state = "granted";
  huddle.roomId = "c1"; huddle.roomName = "#general"; huddle.room = connectedRoom;
  render(<TalkOrb name="Nova" compact />);
  await settle();
  expect(huddle.join).not.toHaveBeenCalled();
  expect(mic).not.toHaveBeenCalled();
});

it("after the agent left for quiet, the pre-connect does not rejoin; the next tap does", async () => {
  perm.state = "granted";
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  await waitFor(() => expect(huddle.join).toHaveBeenCalledTimes(1));
  live();
  rerender(<TalkOrb name="Nova" compact />);
  Object.assign(huddle, { roomId: null, roomName: null, room: null, error: { code: "idle_ended" } });
  lk.agent = null;
  rerender(<TalkOrb name="Nova" compact />);
  await settle();
  expect(huddle.join).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("alert")).toBeNull();
  tap();
  expect(huddle.join).toHaveBeenCalledTimes(2);
});

it("in the desktop app it pre-connects without asking and tells the app when the mic is live", async () => {
  const invoke = vi.fn(async () => {});
  (window as Window & { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };
  try {
    const { rerender } = render(<TalkOrb name="Nova" compact />);
    await waitFor(() => expect(huddle.join).toHaveBeenCalledWith("dm1", "Nova", false));
    listening();
    rerender(<TalkOrb name="Nova" compact />);
    tap();
    expect(invoke).toHaveBeenLastCalledWith("voice_mic_live", { live: true });
  } finally { delete (window as Window & { __TAURI__?: unknown }).__TAURI__; }
});

it("unmounted before the permission check answers, it does not join", async () => {
  perm.state = "granted";
  const { unmount } = render(<TalkOrb name="Nova" compact />);
  unmount();
  await settle();
  expect(huddle.join).not.toHaveBeenCalled();
});


it("the persistent owner receives Fn in Settings without rendering an orb", async () => {
  const settings = () => <TalkSessionProvider name="Nova"><h1>Settings</h1></TalkSessionProvider>;
  const { rerender } = render(settings());
  expect(screen.queryByRole("button", { name: "Talk to Nova" })).not.toBeInTheDocument();
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  now += 120;
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "up" })); });
  expect(huddle.join).toHaveBeenCalledTimes(1);
  listening();
  rerender(settings());
  expect(mic).toHaveBeenCalledWith(true);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(mic).toHaveBeenLastCalledWith(false);
  await settle();
});

it("a held Fn release survives Home-to-Settings navigation", async () => {
  listening();
  const view = (home: boolean) => <TalkSessionProvider name="Nova">{home ? <TalkOrb name="Nova" /> : <h1>Settings</h1>}</TalkSessionProvider>;
  const { rerender } = render(view(true));
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  expect(mic).toHaveBeenLastCalledWith(true);
  rerender(view(false));
  expect(screen.queryByRole("button", { name: "Talk to Nova" })).not.toBeInTheDocument();
  now += HOLD_MS + 50;
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "up" })); });
  expect(mic).toHaveBeenLastCalledWith(false);
  expect(huddle.join).not.toHaveBeenCalled();
  await settle();
});

it("a chord cancellation reaches the owner after the visible orb unmounts", () => {
  listening();
  const view = (home: boolean) => <TalkSessionProvider name="Nova">{home ? <TalkOrb name="Nova" /> : <h1>Agent</h1>}</TalkSessionProvider>;
  const { rerender } = render(view(true));
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  rerender(view(false));
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "cancel" })); });
  expect(mic).toHaveBeenLastCalledWith(false);
  mic.mockClear();
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "up" })); });
  expect(mic).not.toHaveBeenCalled();
});

it("multiple visible controls share one owner and one native-key join", () => {
  render(<TalkSessionProvider name="Nova"><TalkOrb name="Nova" /><TalkOrb name="Nova" compact /></TalkSessionProvider>);
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  expect(huddle.join).toHaveBeenCalledTimes(1);
});

it("true owner teardown closes its assistant mic, but leaves another room's mic alone", () => {
  listening();
  const { unmount } = render(<TalkSessionProvider name="Nova"><h1>Settings</h1></TalkSessionProvider>);
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  mic.mockClear();
  unmount();
  expect(mic).toHaveBeenCalledWith(false);
  huddle.roomId = "other-room";
  mic.mockClear();
  const other = render(<TalkSessionProvider name="Nova"><h1>Settings</h1></TalkSessionProvider>);
  other.unmount();
  expect(mic).not.toHaveBeenCalled();
});


it("a cold held Fn release remains closed when the join lands after navigating away", async () => {
  const view = (home: boolean) => <TalkSessionProvider name="Nova">{home ? <TalkOrb name="Nova" /> : <h1>Settings</h1>}</TalkSessionProvider>;
  const { rerender } = render(view(true));
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  huddle.joining = "dm1";
  rerender(view(false));
  now += HOLD_MS + 50;
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "up" })); });
  connectedRoom.localParticipant.isMicrophoneEnabled = true;
  listening(); huddle.joining = null;
  rerender(view(false));
  expect(mic).toHaveBeenLastCalledWith(false);
  expect(huddle.join).toHaveBeenCalledTimes(1);
  await settle();
});

it("a microphone enable finishing after the owner unmounts cannot reopen capture", async () => {
  listening();
  let enabled!: () => void;
  mic.mockImplementationOnce(() => new Promise<void>((resolve) => { enabled = resolve; }));
  const { unmount } = render(<TalkSessionProvider name="Nova"><h1>Settings</h1></TalkSessionProvider>);
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  expect(mic).toHaveBeenLastCalledWith(true);
  unmount();
  await act(async () => { enabled(); });
  expect(mic).toHaveBeenLastCalledWith(false);
});


it("a cancelled chord restores a previously closed input without ending its conversation", () => {
  listening();
  render(<TalkSessionProvider name="Nova"><TalkOrb name="Nova" /></TalkSessionProvider>);
  hold();
  expect(orb()).toHaveAttribute("aria-pressed", "false");
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "down" })); });
  expect(mic).toHaveBeenLastCalledWith(true);
  act(() => { window.dispatchEvent(new CustomEvent(TALK_KEY, { detail: "cancel" })); });
  expect(mic).toHaveBeenLastCalledWith(false);
  expect(orb()).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByRole("button", { name: "End" })).toBeInTheDocument();
  expect(huddle.leave).not.toHaveBeenCalled();
});


it("Escape before a cold join lands permits the next tap to start a fresh join", () => {
  const { rerender } = render(<TalkOrb name="Nova" />);
  tap();
  expect(huddle.join).toHaveBeenCalledTimes(1);
  huddle.joining = "dm1";
  rerender(<TalkOrb name="Nova" />);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(huddle.leave).toHaveBeenCalledTimes(1);
  // The real HuddleProvider fences the old connection attempt and clears joining on leave.
  huddle.joining = null;
  rerender(<TalkOrb name="Nova" />);
  tap();
  expect(huddle.join).toHaveBeenCalledTimes(2);
});


it("a model-start error closes the owned mic and the next press can retry", async () => {
  listening();
  const { rerender } = render(<TalkOrb name="Nova" />);
  tap();
  await settle();
  mic.mockClear();
  huddle.error = { code: "connection_lost" };
  rerender(<TalkOrb name="Nova" />);
  expect(mic).toHaveBeenCalledWith(false);
  expect(orb()).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByRole("alert")).toHaveTextContent("The connection dropped");
  huddle.error = null; // dismissError clears the real provider's error before retry.
  rerender(<TalkOrb name="Nova" />);
  tap();
  expect(mic).toHaveBeenLastCalledWith(true);
  expect(huddle.leave).not.toHaveBeenCalled();
});


it("assistive activation clicks toggle the microphone without pointer events", () => {
  listening();
  render(<TalkOrb name="Nova" />);
  fireEvent.click(orb(), { detail: 0 });
  expect(mic).toHaveBeenLastCalledWith(true);
  expect(orb()).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(orb(), { detail: 0 });
  expect(mic).toHaveBeenLastCalledWith(false);
  expect(orb()).toHaveAttribute("aria-pressed", "false");
});

it("a physical pointer click is not counted again after its release", () => {
  listening();
  render(<TalkOrb name="Nova" />);
  tap();
  mic.mockClear();
  fireEvent.click(orb(), { detail: 1 });
  expect(mic).not.toHaveBeenCalled();
  expect(orb()).toHaveAttribute("aria-pressed", "true");
});
