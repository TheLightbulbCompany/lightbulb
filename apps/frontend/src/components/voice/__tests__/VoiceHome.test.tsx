import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { TimelineItem } from "../data";

const lk = vi.hoisted(() => ({ attrs: {} as Record<string, string>, agent: null as object | null, lines: [] as { text: string }[] }));
vi.mock("@livekit/components-react", () => ({
  RoomContext: { Provider: ({ children }: { children: React.ReactNode }) => <>{children}</> },
  useRemoteParticipant: () => lk.agent,
  useParticipantAttributes: () => ({ attributes: lk.attrs }),
  useTranscriptions: () => lk.lines,
}));
const mic = vi.hoisted(() => vi.fn());
const connectedRoom = vi.hoisted(() => ({ localParticipant: { setMicrophoneEnabled: mic } }));
const huddle = vi.hoisted(() => ({ roomId: "dm1" as string | null, joining: null, room: connectedRoom as object | null, error: null, join: vi.fn(), leave: vi.fn() }));
vi.mock("@/components/huddle/HuddleProvider", () => ({ CEO_IDENTITY: "agent-ceo", useHuddle: () => huddle }));
vi.mock("@/components/huddle/TalkButton", () => ({
  useAssistantDm: () => ({ roomId: "dm1", kind: "dm", members: [{ kind: "human", id: "u" }, { kind: "agent", id: "a1" }] }),
}));
vi.mock("../VoiceShell", () => ({ useNeedsYouTotal: () => 0 }));
const items = vi.hoisted(() => ({ list: [] as TimelineItem[] }));
vi.mock("../data", async (orig) => ({
  ...(await orig<typeof import("../data")>()),
  useTimeline: () => ({ data: items.list, isLoading: false }),
  useAgentConfig: () => ({ data: { name: "Nova" }, isNew: false, save: vi.fn() }),
  needsOnboarding: () => false,
}));

import { VoiceHome } from "../VoiceHome";

beforeEach(() => {
  lk.attrs = {}; lk.agent = { identity: "agent-ceo" }; lk.lines = []; items.list = [];
  huddle.roomId = "dm1"; huddle.room = connectedRoom; mic.mockClear();
});

it("a keyboard hold survives the room and the assistant connecting", () => {
  huddle.roomId = null; huddle.room = null; lk.agent = null;
  const { rerender } = render(<VoiceHome />);
  const orb = screen.getByRole("button", { name: "Hold to talk to Nova" });
  orb.focus();
  fireEvent.keyDown(orb, { key: " " });
  expect(huddle.join).toHaveBeenCalledWith("dm1", "Nova");
  huddle.roomId = "dm1"; huddle.room = connectedRoom;
  rerender(<VoiceHome />);
  lk.agent = { identity: "agent-ceo" }; lk.attrs = { "lightbulb.agent.a1.status": "listening" };
  rerender(<VoiceHome />);
  expect(screen.getByRole("button", { name: "Hold to talk to Nova" })).toBe(orb); // the same element, never remounted
  expect(document.activeElement).toBe(orb);
  expect(mic).not.toHaveBeenCalledWith(false);
  fireEvent.keyUp(orb, { key: " " });
  expect(mic).toHaveBeenCalledWith(false);
});

it("orb shows thinking then speaking from the agent attribute", () => {
  lk.attrs = { "lightbulb.agent.a1.status": "working" };
  const { rerender } = render(<VoiceHome />);
  const orb = screen.getByRole("button", { name: "Hold to talk to Nova" });
  expect(orb).toHaveAttribute("data-state", "thinking");
  expect(screen.getByText("Nova is thinking…")).toBeInTheDocument();
  lk.attrs = { "lightbulb.agent.a1.status": "speaking" };
  lk.lines = [{ text: "Priya moved to 11:30." }, { text: "I'm drafting the holiday post now." }];
  rerender(<VoiceHome />);
  expect(orb).toHaveAttribute("data-state", "speaking");
  expect(screen.getByText("Nova is speaking")).toBeInTheDocument();
  expect(screen.getByText("I'm drafting the holiday post now.")).toBeInTheDocument(); // the last line, as a caption
});

it("orb is connecting until the assistant joins", () => {
  lk.agent = null;
  render(<VoiceHome />);
  expect(screen.getByRole("button", { name: "Hold to talk to Nova" })).toHaveAttribute("data-state", "connecting");
});

it("Yours chip lists the in-app talk", () => {
  items.list = [
    { type: "call", roomId: "t1", at: Date.now(), ownerMode: true, summary: "“Anything I need to handle?” → Draft the post", durationSec: 42,
      transcript: [{ who: "caller", text: "Anything I need to handle?" }, { who: "assistant", text: "One thing: the post." }] },
    { type: "call", roomId: "c1", at: Date.now() - 1000, caller: "+14155550100", summary: "Booked a manicure" },
  ];
  render(<VoiceHome />);
  fireEvent.click(screen.getByRole("button", { name: /^Yours/ }));
  expect(screen.queryByText("Booked a manicure")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /You and Nova/ }));
  const pane = screen.getByRole("complementary", { name: "Details" });
  expect(within(pane).getByText("Anything I need to handle?").closest("p")).toHaveTextContent("You: Anything I need to handle?");
  expect(within(pane).getByText("One thing: the post.").closest("p")).toHaveTextContent("Nova: One thing: the post.");
});
