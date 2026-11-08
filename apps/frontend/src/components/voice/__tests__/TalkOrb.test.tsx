import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const lk = vi.hoisted(() => ({ attrs: {} as Record<string, string>, agent: null as object | null, lines: [] as { text: string }[] }));
vi.mock("@livekit/components-react", () => ({
  RoomContext: { Provider: ({ children }: { children: React.ReactNode }) => <>{children}</> },
  useRemoteParticipant: () => lk.agent,
  useParticipantAttributes: () => ({ attributes: lk.attrs }),
  useTranscriptions: () => lk.lines,
}));
const mic = vi.hoisted(() => vi.fn());
const connectedRoom = vi.hoisted(() => ({ localParticipant: { setMicrophoneEnabled: mic } }));
const huddle = vi.hoisted(() => ({
  roomId: null as string | null, roomName: null as string | null, joining: null as string | null, room: null as object | null,
  error: null as { code: string } | null, join: vi.fn(), leave: vi.fn(),
}));
vi.mock("@/components/huddle/HuddleProvider", () => ({ CEO_IDENTITY: "agent-ceo", useHuddle: () => huddle }));
vi.mock("@/components/huddle/useAssistantDm", () => ({
  useAssistantDm: () => ({ roomId: "dm1", kind: "dm", members: [{ kind: "human", id: "u" }, { kind: "agent", id: "a1" }] }),
}));

import { TalkOrb } from "../TalkOrb";

// The browser's mic permission (jsdom has no Permissions API): "prompt" unless a test grants it.
const perm = { state: "prompt" };
Object.defineProperty(navigator, "permissions", { configurable: true, value: { query: () => Promise.resolve(perm) } });

beforeEach(() => {
  lk.attrs = {}; lk.agent = null; lk.lines = [];
  Object.assign(huddle, { roomId: null, roomName: null, joining: null, room: null, error: null });
  huddle.join.mockReset(); mic.mockClear(); perm.state = "prompt";
});

const live = () => { huddle.roomId = "dm1"; huddle.roomName = "Nova"; huddle.room = connectedRoom; lk.agent = { identity: "agent-ceo" }; };
const settle = () => act(async () => {}); // lets the permission check and anything it started finish

it("compact: a keyboard hold survives the room and the assistant connecting", () => {
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  const orb = screen.getByRole("button", { name: "Hold to talk to Nova" });
  orb.focus();
  fireEvent.keyDown(orb, { key: " " });
  expect(huddle.join).toHaveBeenCalledWith("dm1", "Nova");
  huddle.joining = "dm1";
  rerender(<TalkOrb name="Nova" compact />);
  expect(orb).toHaveAttribute("data-state", "connecting");
  huddle.joining = null; live(); lk.attrs = { "lightbulb.agent.a1.status": "listening" };
  rerender(<TalkOrb name="Nova" compact />);
  expect(screen.getByRole("button", { name: "Hold to talk to Nova" })).toBe(orb); // never remounted
  expect(document.activeElement).toBe(orb);
  expect(orb).toHaveAttribute("data-state", "listening");
  expect(mic).not.toHaveBeenCalledWith(false);
  fireEvent.keyUp(orb, { key: " " });
  expect(mic).toHaveBeenCalledWith(false);
});

it("compact: thinking, then speaking with the last line as a polite caption", () => {
  live(); lk.attrs = { "lightbulb.agent.a1.status": "working" };
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  const orb = screen.getByRole("button", { name: "Hold to talk to Nova" });
  expect(orb).toHaveAttribute("data-state", "thinking");
  expect(screen.getByText("Nova is thinking…")).toBeInTheDocument();
  lk.attrs = { "lightbulb.agent.a1.status": "speaking" };
  lk.lines = [{ text: "Priya moved to 11:30." }, { text: "I'm drafting the holiday post now." }];
  rerender(<TalkOrb name="Nova" compact />);
  expect(orb).toHaveAttribute("data-state", "speaking");
  const caption = screen.getByText("I'm drafting the holiday post now.");
  expect(caption).toHaveAttribute("aria-live", "polite");
  expect(caption.closest("[data-open]")).toHaveAttribute("data-open", "true"); // the bubble shows while talking
  expect(screen.getByRole("button", { name: "End" })).toBeInTheDocument();
});

it("compact: the bubble is closed while idle", () => {
  render(<TalkOrb name="Nova" compact />);
  expect(screen.getByRole("button", { name: "Hold to talk to Nova" })).toHaveAttribute("data-state", "idle");
  expect(screen.getByText("Hold to talk to Nova").closest("[data-open]")).toHaveAttribute("data-open", "false");
});

it("never mutes, nor shows the error of, a Live it did not start", () => {
  huddle.roomId = "c1"; huddle.roomName = "#general"; huddle.room = connectedRoom; huddle.error = { code: "connection_lost" };
  render(<TalkOrb name="Nova" compact />);
  expect(mic).not.toHaveBeenCalled();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("a failed orb join does not mute a later room Live", () => {
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  const orb = screen.getByRole("button", { name: "Hold to talk to Nova" });
  fireEvent.keyDown(orb, { key: " " });
  fireEvent.keyUp(orb, { key: " " });
  huddle.error = { code: "voice_unavailable" }; // join failed: room stays null
  rerender(<TalkOrb name="Nova" compact />);
  huddle.error = null; huddle.roomId = "c1"; huddle.roomName = "#general"; huddle.room = connectedRoom; // a room header's Live
  rerender(<TalkOrb name="Nova" compact />);
  expect(mic).not.toHaveBeenCalled();
});

it("compact: End stays visible while Live, also when quiet", () => {
  live(); lk.attrs = { "lightbulb.agent.a1.status": "idle" };
  render(<TalkOrb name="Nova" compact />);
  expect(screen.getByRole("button", { name: "Hold to talk to Nova" })).toHaveAttribute("data-state", "idle");
  expect(screen.getByRole("button", { name: "End" }).closest("[data-open]")).toHaveAttribute("data-open", "true");
});

it("a talk that ended for quiet is not reported as unreachable", () => {
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  const orb = screen.getByRole("button", { name: "Hold to talk to Nova" });
  fireEvent.keyDown(orb, { key: " " });
  fireEvent.keyUp(orb, { key: " " });
  huddle.error = { code: "idle_ended" }; // owner.py left after AWAY_S of quiet
  rerender(<TalkOrb name="Nova" compact />);
  expect(screen.getByRole("alert")).toHaveTextContent("left after a few minutes of quiet");
  expect(screen.getByRole("alert")).not.toHaveTextContent("Couldn't reach");
});

// Pre-connect (8 Nov: the agent took 21 s to join on the first press; by then the hold was over and the mic off).

it("pre-connects to the DM with the mic off once the DM is known, once per mount, and looks idle meanwhile", async () => {
  perm.state = "granted";
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  await waitFor(() => expect(huddle.join).toHaveBeenCalledWith("dm1", "Nova"));
  huddle.joining = "dm1";
  rerender(<TalkOrb name="Nova" compact />);
  const orb = screen.getByRole("button", { name: "Hold to talk to Nova" });
  expect(orb).toHaveAttribute("data-state", "idle"); // nothing was asked: no "connecting", no bubble
  huddle.joining = null; live();
  rerender(<TalkOrb name="Nova" compact />);
  expect(mic).toHaveBeenCalledWith(false); // the join turned the mic on; nobody is holding
  expect(orb).toHaveAttribute("data-state", "idle");
  expect(screen.getByText("Hold to talk to Nova").closest("[data-open]")).toHaveAttribute("data-open", "false");
  expect(screen.queryByRole("button", { name: "End" })).toBeNull();
  rerender(<TalkOrb name="Nova Prime" compact />);
  await settle();
  expect(huddle.join).toHaveBeenCalledTimes(1);
  // the first press on a pre-connected room only unmutes
  mic.mockClear();
  fireEvent.keyDown(orb, { key: " " });
  expect(huddle.join).toHaveBeenCalledTimes(1);
  expect(mic).toHaveBeenCalledWith(true);
});

it("does not pre-connect while the browser has not granted the mic: the first press asks", async () => {
  render(<TalkOrb name="Nova" compact />);
  await settle();
  expect(huddle.join).not.toHaveBeenCalled();
  fireEvent.keyDown(screen.getByRole("button", { name: "Hold to talk to Nova" }), { key: " " });
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

it("a press while the pre-connect is still connecting arms the mic and shows connecting until the agent listens", async () => {
  perm.state = "granted";
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  await waitFor(() => expect(huddle.join).toHaveBeenCalledTimes(1));
  huddle.joining = "dm1";
  rerender(<TalkOrb name="Nova" compact />);
  const orb = screen.getByRole("button", { name: "Hold to talk to Nova" });
  fireEvent.keyDown(orb, { key: " " });
  expect(huddle.join).toHaveBeenCalledTimes(1); // no second join over the one in flight
  expect(orb).toHaveAttribute("data-state", "connecting");
  huddle.joining = null; live(); // the room is connected, the agent is in it but has not set its status yet
  rerender(<TalkOrb name="Nova" compact />);
  expect(mic).not.toHaveBeenCalledWith(false); // held: the mic the join turned on stays on
  expect(orb).toHaveAttribute("data-state", "connecting");
  lk.attrs = { "lightbulb.agent.a1.status": "listening" };
  rerender(<TalkOrb name="Nova" compact />);
  expect(orb).toHaveAttribute("data-state", "listening");
  expect(screen.getByRole("button", { name: "End" })).toBeInTheDocument();
  fireEvent.keyUp(orb, { key: " " });
  expect(mic).toHaveBeenCalledWith(false);
});

it("after the agent left for quiet, the pre-connect does not rejoin; the next press does", async () => {
  perm.state = "granted";
  const { rerender } = render(<TalkOrb name="Nova" compact />);
  await waitFor(() => expect(huddle.join).toHaveBeenCalledTimes(1));
  live();
  rerender(<TalkOrb name="Nova" compact />);
  Object.assign(huddle, { roomId: null, roomName: null, room: null, error: { code: "idle_ended" } }); // HuddleProvider left on idle_ended
  lk.agent = null;
  rerender(<TalkOrb name="Nova" compact />);
  await settle();
  expect(huddle.join).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("alert")).toBeNull(); // nobody held: nothing to report
  fireEvent.keyDown(screen.getByRole("button", { name: "Hold to talk to Nova" }), { key: " " });
  expect(huddle.join).toHaveBeenCalledTimes(2);
});

it("in the desktop app (wry: no Permissions API, getUserMedia without a prompt) it pre-connects without asking", async () => {
  (window as Window & { __TAURI__?: unknown }).__TAURI__ = { core: { invoke: vi.fn() } };
  try {
    render(<TalkOrb name="Nova" compact />); // perm.state is "prompt": a browser would wait for the first press
    await waitFor(() => expect(huddle.join).toHaveBeenCalledWith("dm1", "Nova"));
  } finally { delete (window as Window & { __TAURI__?: unknown }).__TAURI__; }
});

it("unmounted before the permission check answers, it does not join", async () => {
  perm.state = "granted";
  const { unmount } = render(<TalkOrb name="Nova" compact />);
  unmount();
  await settle();
  expect(huddle.join).not.toHaveBeenCalled();
});
