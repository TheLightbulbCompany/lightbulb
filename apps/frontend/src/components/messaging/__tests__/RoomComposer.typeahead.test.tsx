import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";
import type { RoomParticipant } from "../types";
import { composerBox, composerValue, selectInComposer, typeInComposer } from "@/test-utils/composer";

const members: RoomParticipant[] = [
  { kind: "human", id: "user-aden", displayName: "Aden Kim", mentionHandle: null, role: "member", listenMode: null, joinedAt: "2026-09-10T00:00:00.000Z", joinedSeq: 1 },
  { kind: "agent", id: "agent-friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00.000Z", joinedSeq: 2 },
  { kind: "agent", id: "agent-atlas", displayName: "Atlas", mentionHandle: "atlas", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00.000Z", joinedSeq: 3 },
];

function Harness({ onSubmit = vi.fn(), focusAgentId }: { onSubmit?: RoomComposerProps["onSubmit"]; focusAgentId?: string }) {
  const [text, setText] = useState("");
  return <RoomComposer text={text} onTextChange={setText} members={members}
    clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={onSubmit} focusAgentId={focusAgentId} />;
}

const field = () => composerBox();
const type = (value: string) => typeInComposer(value);

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("resolves a typed handle to a mention on send (no picker)", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("@friday ship it");
  expect(screen.queryByRole("listbox", { name: "Mention a room member" })).not.toBeInTheDocument();
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    text: "@friday ship it",
    mentions: [{ kind: "agent", id: "agent-friday" }],
  }));
});

it("leaves an unmatched token plain and mentions nobody", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("@nobody ship it");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ mentions: [] }));
});

it("still sends on Enter when no list is open", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("Ship it");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledOnce();
});

it("opens the emoji list after two characters and inserts the character", () => {
  render(<Harness />);
  // "tad" is the unique substring match for party popper's "tada" keyword in
  // the curated set (verified: searchEmoji("ta") ranks it 19th, past the
  // 8-item cap — "tad" is the only query that lands it at index 0, which
  // Enter-with-no-arrow-keys relies on).
  type("nice :tad");
  expect(screen.getByRole("listbox", { name: "Insert an emoji" })).toBeInTheDocument();
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(composerValue().startsWith("nice 🎉")).toBe(true);
});

it("does not open the emoji list after one character", () => {
  render(<Harness />);
  type("nice :t");
  expect(screen.queryByRole("listbox", { name: "Insert an emoji" })).not.toBeInTheDocument();
});

it("inserts a picked emoji at the caret, not at the end", () => {
  render(<Harness />);
  type("ab");
  // Collapse the caret to a single point between "a" and "b". Setting only
  // selectionStart here would leave selectionEnd stuck at 2 (RTL's
  // setNativeValue resets the selection to the end of the value before
  // applying target overrides), which would make this a [1,2) replace
  // instead of the caret placement the test name describes.
  selectInComposer(1, 1);
  fireEvent.click(screen.getByRole("button", { name: "Insert emoji" }));
  fireEvent.click(screen.getByRole("button", { name: "party popper" }));
  expect(composerValue()).toBe("a🎉b");
});

it("clears a stale completion trigger when a non-typing edit (the emoji picker) rewrites the text", () => {
  render(<Harness />);
  type("nice :tad");
  expect(screen.getByRole("listbox", { name: "Insert an emoji" })).toBeInTheDocument();
  // The emoji-picker button opens the full picker (a different UI from the
  // composer's own `:tad` typeahead) and inserts via insertAtCaret, which
  // must clear the still-open `:tad` trigger.
  fireEvent.click(screen.getByRole("button", { name: "Insert emoji" }));
  fireEvent.click(screen.getByRole("button", { name: "party popper" }));
  expect(composerValue()).toBe("nice :tad🎉");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  fireEvent.keyDown(field(), { key: "Enter" });
  // Without the fix, Enter re-applies the stale `:tad` trigger's byte
  // offsets and eats the "tad" text; with the fix, no list is open so
  // Enter just submits and the text is untouched.
  expect(composerValue()).toBe("nice :tad🎉");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
});
