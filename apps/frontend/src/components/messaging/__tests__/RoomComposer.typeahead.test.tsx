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

it("opens and filters the mention list from a typed @", () => {
  render(<Harness />);
  type("Ask @fri");
  expect(screen.getByRole("listbox", { name: "Mention a room member" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /Friday/ })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /Atlas/ })).not.toBeInTheDocument();
});

it("puts the DM's agent first in a DM with one agent, ahead of members.list order", () => {
  render(<Harness focusAgentId="agent-atlas" />);
  type("Ask @");
  const options = screen.getAllByRole("option");
  expect(options[0]).toHaveAccessibleName(/Atlas/);
});

it("closes the list when nothing matches so prose after an @ is left alone", () => {
  render(<Harness />);
  type("Email me @ the office");
  expect(screen.queryByRole("listbox", { name: "Mention a room member" })).not.toBeInTheDocument();
});

it("inserts an agent handle with Enter instead of sending", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("Ask @fri");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(composerValue()).toBe("Ask @friday ");
  expect(onSubmit).not.toHaveBeenCalled();
});

it("inserts a human as @Display Name and sends the structured mention", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("Hi @Ade");
  fireEvent.click(screen.getByRole("option", { name: /Aden Kim/ }));
  expect(composerValue()).toBe("Hi @Aden Kim ");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    text: "Hi @Aden Kim ",
    mentions: [{ kind: "human", id: "user-aden" }],
  }));
});

it("stops shipping a picked mention once its typed token is removed from the text", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("@ad");
  fireEvent.click(screen.getByRole("option", { name: /Aden Kim/ }));
  expect(composerValue()).toBe("@Aden Kim ");
  // Select-all and retype, wiping the inserted @Aden Kim token.
  type("Never mind");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    text: "Never mind",
    mentions: [],
  }));
});

it("resolves a fully typed handle on send without ever opening the list", () => {
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

it("ignores ArrowDown during IME composition so the typeahead selection does not move", () => {
  render(<Harness />);
  type("Ask @");
  const activedescendant = () => field().getAttribute("aria-activedescendant");
  const before = activedescendant();
  fireEvent.keyDown(field(), { key: "ArrowDown", isComposing: true });
  expect(activedescendant()).toBe(before);
});

it("moves the active option with the arrow keys and wraps", () => {
  render(<Harness />);
  type("Ask @");
  const id = () => field().getAttribute("aria-activedescendant");
  const first = id();
  fireEvent.keyDown(field(), { key: "ArrowDown" });
  expect(id()).not.toBe(first);
  fireEvent.keyDown(field(), { key: "ArrowUp" });
  expect(id()).toBe(first);
});

it("inserts with Tab as well as Enter", () => {
  render(<Harness />);
  type("Ask @atl");
  fireEvent.keyDown(field(), { key: "Tab" });
  expect(composerValue()).toBe("Ask @atlas ");
});

it("closes with Escape without changing the draft", () => {
  render(<Harness />);
  type("Ask @");
  fireEvent.keyDown(field(), { key: "Escape" });
  expect(screen.queryByRole("listbox", { name: "Mention a room member" })).not.toBeInTheDocument();
  expect(composerValue()).toBe("Ask @");
});

it("opens the same list from the @ button", () => {
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Mention someone" }));
  expect(screen.getByRole("listbox", { name: "Mention a room member" })).toBeInTheDocument();
  expect(composerValue()).toBe("@");
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
