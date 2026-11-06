import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";
import type { RoomParticipant } from "../types";
import { composerBox, composerValue, selectInComposer, typeInComposer } from "@/test-utils/composer";

// A human with a server-derived handle: the only difference from the
// typeahead suite's fixture, which still covers the legacy null-handle rows.
const members: RoomParticipant[] = [
  { kind: "human", id: "user-aden", displayName: "Aden Kim", mentionHandle: "aden-kim", role: "member", listenMode: null, joinedAt: "2026-09-10T00:00:00.000Z", joinedSeq: 1 },
  { kind: "agent", id: "agent-friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00.000Z", joinedSeq: 2 },
];

function Harness({ onSubmit = vi.fn() }: { onSubmit?: RoomComposerProps["onSubmit"] }) {
  const [text, setText] = useState("");
  return <RoomComposer text={text} onTextChange={setText} members={members}
    clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={onSubmit} />;
}

const field = () => composerBox();
const type = (value: string) => typeInComposer(value);

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("W18-2: lists a person by name, never by their email-derived handle", () => {
  render(<Harness />);
  type("Hi @ade");
  expect(screen.getByRole("option", { name: /Aden Kim/ })).not.toHaveAccessibleName(/@aden-kim/);
  expect(screen.getByRole("option", { name: /Aden Kim/ })).toHaveAccessibleName(/Person/);
});

it("W18-2: inserts the person's name and sends their id as the structured mention", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("Hi @ade");
  fireEvent.click(screen.getByRole("option", { name: /Aden Kim/ }));
  expect(composerValue()).toBe("Hi @Aden Kim ");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    text: "Hi @Aden Kim ",
    mentions: [{ kind: "human", id: "user-aden" }],
  }));
});

const priya = { kind: "human" as const, id: "user-priya", displayName: "Priya Test", mentionHandle: null };

it("W18-3: offers someone not in the channel, and asks to add them on send", async () => {
  const onSubmit = vi.fn();
  const onAddPeople = vi.fn(async () => {});
  function Outside() {
    const [text, setText] = useState("");
    return <RoomComposer text={text} onTextChange={setText} members={members} outsiders={[priya]} onAddPeople={onAddPeople} roomLabel="#tea shop"
      clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={onSubmit} />;
  }
  render(<Outside />);
  type("thanks @pri");
  const option = screen.getByRole("option", { name: /Priya Test/ });
  expect(option).toHaveAccessibleName(/Not in channel/);
  fireEvent.click(option);
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("Priya Test isn't in #tea shop.");
  fireEvent.click(screen.getByRole("button", { name: "Add them" }));
  await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ mentions: [{ kind: "human", id: "user-priya" }] })));
  expect(onAddPeople).toHaveBeenCalledWith([{ kind: "human", id: "user-priya" }]);
});

it("W18-3: Do nothing sends without adding or mentioning them", () => {
  const onSubmit = vi.fn();
  const onAddPeople = vi.fn(async () => {});
  function Outside() {
    const [text, setText] = useState("thanks @Priya Test");
    return <RoomComposer text={text} onTextChange={setText} members={members} outsiders={[priya]} onAddPeople={onAddPeople} roomLabel="#tea shop"
      clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={onSubmit} />;
  }
  render(<Outside />);
  fireEvent.keyDown(field(), { key: "Enter" });
  fireEvent.click(screen.getByRole("button", { name: "Do nothing" }));
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ mentions: [] }));
  expect(onAddPeople).not.toHaveBeenCalled();
});

it("resolves a hand-typed human handle on send", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("hi @aden-kim and @friday ship it");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    mentions: [{ kind: "human", id: "user-aden" }, { kind: "agent", id: "agent-friday" }],
  }));
});
