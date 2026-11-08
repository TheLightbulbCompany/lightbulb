import { fireEvent, render, screen } from "@testing-library/react";
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

beforeEach(() => {
  lk.attrs = {}; lk.agent = null; lk.lines = [];
  Object.assign(huddle, { roomId: null, roomName: null, joining: null, room: null, error: null });
  huddle.join.mockReset(); mic.mockClear();
});

const live = () => { huddle.roomId = "dm1"; huddle.roomName = "Nova"; huddle.room = connectedRoom; lk.agent = { identity: "agent-ceo" }; };

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
