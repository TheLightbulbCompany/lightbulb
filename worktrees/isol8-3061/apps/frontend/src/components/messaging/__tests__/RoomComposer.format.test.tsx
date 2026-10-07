import { useState } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomComposer } from "../RoomComposer";
import type { RoomParticipant } from "../types";

function Harness({ members = [] }: { members?: readonly RoomParticipant[] }) {
  const [text, setText] = useState("ship it");
  return <RoomComposer text={text} onTextChange={setText} members={members}
    clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={vi.fn()} />;
}

const field = () => screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
const select = (start: number, end: number) => { field().setSelectionRange(start, end); };

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("collapses the formatting toolbar behind Aa, and the Aa button announces it", () => {
  render(<Harness />);
  expect(screen.getByRole("toolbar", { name: "Formatting" })).toBeInTheDocument();
  const toggle = screen.getByRole("button", { name: "Hide formatting" });
  expect(toggle).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(toggle);
  expect(screen.queryByRole("toolbar", { name: "Formatting" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Show formatting" })).toHaveAttribute("aria-pressed", "false");
  // The "Enter to send" lesson is for screen readers only; sighted users get the field.
  expect(screen.getByText("Enter to send · Shift+Enter for a new line.")).toHaveClass("sr-only");
});

it("exposes the eight formatting controls in one toolbar", () => {
  render(<Harness />);
  const toolbar = screen.getByRole("toolbar", { name: "Formatting" });
  for (const name of ["Bold", "Italic", "Strikethrough", "Bulleted list", "Numbered list", "Inline code", "Code block", "Link"]) {
    expect(screen.getByRole("button", { name })).toBe(toolbar.querySelector(`[aria-label="${name}"]`));
  }
  expect(screen.getByRole("button", { name: "Bold" })).toHaveAttribute("aria-keyshortcuts", "Meta+B Control+B");
});

it("uses a single roving tab stop across the toolbar and moves focus with ArrowRight", () => {
  render(<Harness />);
  const toolbar = screen.getByRole("toolbar", { name: "Formatting" });
  const buttons = within(toolbar).getAllByRole("button");
  const zeroTabIndex = buttons.filter((button) => button.getAttribute("tabindex") === "0");
  expect(zeroTabIndex).toHaveLength(1);
  expect(zeroTabIndex[0]).toBe(buttons[0]);
  buttons[0].focus();
  fireEvent.keyDown(buttons[0], { key: "ArrowRight" });
  expect(buttons[1]).toHaveFocus();
});

it("wraps the selection from the toolbar and unwraps on a second press", () => {
  render(<Harness />);
  select(0, 4);
  fireEvent.click(screen.getByRole("button", { name: "Bold" }));
  expect(field()).toHaveValue("**ship** it");
  select(2, 6);
  fireEvent.click(screen.getByRole("button", { name: "Bold" }));
  expect(field()).toHaveValue("ship it");
});

it("wraps from the keyboard shortcuts", () => {
  render(<Harness />);
  select(0, 4);
  fireEvent.keyDown(field(), { key: "b", metaKey: true });
  expect(field()).toHaveValue("**ship** it");
});

it("uses shift+X for strikethrough and shift+C for a code block", () => {
  render(<Harness />);
  select(0, 4);
  fireEvent.keyDown(field(), { key: "X", metaKey: true, shiftKey: true });
  expect(field()).toHaveValue("~~ship~~ it");
  select(2, 6);
  fireEvent.keyDown(field(), { key: "C", metaKey: true, shiftKey: true });
  expect(field()).toHaveValue("~~```\nship\n```~~ it");
});

it("builds a list from the touched lines", () => {
  render(<Harness />);
  fireEvent.change(field(), { target: { value: "one\ntwo", selectionStart: 7 } });
  select(0, 7);
  fireEvent.click(screen.getByRole("button", { name: "Bulleted list" }));
  expect(field()).toHaveValue("- one\n- two");
});

it("suppresses the shortcut while the mention typeahead is open", () => {
  const members: RoomParticipant[] = [
    { kind: "agent", id: "agent-friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00.000Z", joinedSeq: 1 },
  ];
  render(<Harness members={members} />);
  fireEvent.change(field(), { target: { value: "ship it @fri", selectionStart: 13 } });
  expect(screen.getByRole("listbox", { name: "Mention a room member" })).toBeInTheDocument();
  fireEvent.keyDown(field(), { key: "b", metaKey: true });
  expect(field()).toHaveValue("ship it @fri");
});
