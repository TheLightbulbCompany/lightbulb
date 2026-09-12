import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";
import type { RoomParticipant } from "../types";

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

const field = () => screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Message" });
const type = (value: string) => fireEvent.change(field(), { target: { value, selectionStart: value.length } });

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("lists a human by their handle instead of the Person label", () => {
  render(<Harness />);
  type("Hi @ade");
  expect(screen.getByRole("option", { name: /Aden Kim/ })).toHaveAccessibleName(/@aden-kim/);
  expect(screen.queryByText("Person")).not.toBeInTheDocument();
});

it("inserts the human handle and sends the structured human mention", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  type("Hi @ade");
  fireEvent.click(screen.getByRole("option", { name: /Aden Kim/ }));
  expect(field()).toHaveValue("Hi @aden-kim ");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    text: "Hi @aden-kim ",
    mentions: [{ kind: "human", id: "user-aden" }],
  }));
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
