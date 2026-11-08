import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

type Stream = { text: string; streamInfo: { id: string; attributes?: Record<string, string> } };
const lk = vi.hoisted(() => ({ streams: [] as Stream[], asked: [] as string[][] }));
vi.mock("@livekit/components-react", () => ({
  RoomContext: { Provider: ({ children }: { children: React.ReactNode }) => <>{children}</> },
  useLocalParticipant: () => ({ localParticipant: { identity: "user_maya" } }),
  useTranscriptions: (opts: { participantIdentities: string[] }) => { lk.asked.push(opts.participantIdentities); return lk.streams; },
}));
const huddle = vi.hoisted(() => ({ roomId: "dm1" as string | null, room: {} as object | null }));
vi.mock("@/components/huddle/HuddleProvider", () => ({ useHuddle: () => huddle }));
vi.mock("@/components/teams/shared/components/Identity", () => ({ Identity: () => null }));

import { CLEAR_MS, LiveTranscript } from "../LiveTranscript";

const seg = (text: string, final = false): Stream => ({ text, streamInfo: { id: `s-${text.length}`, attributes: { "lk.segment_id": "seg1", "lk.transcription_final": String(final) } } });
const older = [{ messageSeq: 4, text: "Hi", author: { kind: "human", id: "user_maya" } }];

beforeEach(() => { lk.streams = []; lk.asked = []; huddle.roomId = "dm1"; huddle.room = {}; });
afterEach(() => vi.useRealTimers());

it("the member's words stream into a pending bubble, which becomes the real message at the end of the line", () => {
  lk.streams = [seg("What have we")];
  const { rerender } = render(<LiveTranscript roomId="dm1" messages={older} name="Maya" />);
  expect(screen.getByRole("article", { name: "Maya is speaking" })).toHaveTextContent("What have we");
  expect(lk.asked.at(-1)).toEqual(["user_maya"]); // only the member's own transcription

  lk.streams = [seg("What have we shipped this week?", true)];
  rerender(<LiveTranscript roomId="dm1" messages={older} name="Maya" />);
  expect(screen.getByRole("article")).toHaveTextContent("What have we shipped this week?"); // final, not yet posted

  // the talker's delegated message (the request and its context) is in the room: the bubble steps aside
  const posted = [...older, { messageSeq: 5, text: "What have we shipped this week?\n\nSaid in the talk…", author: { kind: "human", id: "user_maya" } }];
  rerender(<LiveTranscript roomId="dm1" messages={posted} name="Maya" />);
  expect(screen.queryByRole("article")).toBeNull();
});

it("a finished line with no message (a greeting, or a post that failed) clears on its own", () => {
  vi.useFakeTimers();
  lk.streams = [seg("Hi Nova", true)];
  render(<LiveTranscript roomId="dm1" messages={older} name="Maya" />);
  expect(screen.getByRole("article")).toHaveTextContent("Hi Nova");
  act(() => { vi.advanceTimersByTime(CLEAR_MS); });
  expect(screen.queryByRole("article")).toBeNull();
});

it("an earlier message with the same words does not hide a new line", () => {
  const said = [{ messageSeq: 4, text: "Yes", author: { kind: "human", id: "user_maya" } }];
  lk.streams = [seg("Yes")];
  render(<LiveTranscript roomId="dm1" messages={said} name="Maya" />);
  expect(screen.getByRole("article")).toHaveTextContent("Yes");
});

it("shows nothing outside this room's Live", () => {
  lk.streams = [seg("Hello")];
  huddle.roomId = "other";
  const { container } = render(<LiveTranscript roomId="dm1" messages={older} name="Maya" />);
  expect(container).toBeEmptyDOMElement();
});
