import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useState } from "react";
import type { MemberRef } from "@lightbulb/shared";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";
import type { RoomParticipant } from "../types";

const members: RoomParticipant[] = [
  { kind: "human", id: "leo", displayName: "Leo Gordon", mentionHandle: null, role: "member", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 1 },
  { kind: "agent", id: "friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 2 },
  { kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: "atlas", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 3 },
];

function Harness({ onSubmit = vi.fn() }: { onSubmit?: RoomComposerProps["onSubmit"] }) {
  const [text, setText] = useState("");
  const [mentions, setMentions] = useState<MemberRef[]>([]);
  return <RoomComposer text={text} onTextChange={setText} members={members} selectedMentions={mentions}
    onMentionsChange={setMentions} clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={onSubmit} />;
}

afterEach(cleanup);

it("opens and filters mention suggestions when @ is typed", () => {
  render(<Harness />);
  const composer = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(composer, { target: { value: "Ask @fri", selectionStart: 8 } });
  expect(screen.getByRole("listbox", { name: "Mention a room member" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /Friday/ })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /Atlas/ })).not.toBeInTheDocument();
});

it("selects the active suggestion with Enter instead of sending", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  const composer = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(composer, { target: { value: "Ask @", selectionStart: 5 } });
  fireEvent.keyDown(composer, { key: "ArrowDown" });
  fireEvent.keyDown(composer, { key: "Enter" });
  expect(composer).toHaveValue("Ask @friday ");
  expect(screen.getByText("Mentioning @friday")).toBeInTheDocument();
  expect(onSubmit).not.toHaveBeenCalled();
});

it("removes a human's typed query and submits the structured mention", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  const composer = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(composer, { target: { value: "Hi @", selectionStart: 4 } });
  fireEvent.click(screen.getByRole("option", { name: /Leo Gordon/ }));
  expect(composer).toHaveValue("Hi ");
  fireEvent.keyDown(composer, { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    text: "Hi ",
    mentions: [{ kind: "human", id: "leo" }],
  }));
});

it("keeps Enter-to-send behavior when no mention menu is open", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  const composer = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(composer, { target: { value: "Ship it", selectionStart: 7 } });
  fireEvent.keyDown(composer, { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledOnce();
});

it("closes suggestions with Escape without changing the draft", () => {
  render(<Harness />);
  const composer = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(composer, { target: { value: "Ask @", selectionStart: 5 } });
  fireEvent.keyDown(composer, { key: "Escape" });
  expect(screen.queryByRole("listbox", { name: "Mention a room member" })).not.toBeInTheDocument();
  expect(composer).toHaveValue("Ask @");
});
