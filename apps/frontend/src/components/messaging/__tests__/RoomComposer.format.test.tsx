import { useState } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomComposer } from "../RoomComposer";
import { composerBox, composerValue, selectInComposer, typeInComposer } from "@/test-utils/composer";
import type { RoomParticipant } from "../types";

function Harness({ members = [] }: { members?: readonly RoomParticipant[] }) {
  const [text, setText] = useState("ship it");
  return <RoomComposer text={text} onTextChange={setText} members={members}
    clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={vi.fn()} />;
}

const field = () => composerBox();
const select = selectInComposer;

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

it("W18-4: Bold applies in the box (no ** typed) and a second press removes it", () => {
  render(<Harness />);
  select(0, 4);
  fireEvent.click(screen.getByRole("button", { name: "Bold" }));
  expect(field().querySelector("strong, .font-semibold")).toHaveTextContent("ship");
  expect(field()).not.toHaveTextContent("**");
  expect(composerValue()).toBe("**ship** it");
  select(0, 4);
  fireEvent.click(screen.getByRole("button", { name: "Bold" }));
  expect(composerValue()).toBe("ship it");
});

it("wraps from the keyboard shortcuts", () => {
  render(<Harness />);
  select(0, 4);
  fireEvent.keyDown(field(), { key: "b", metaKey: true });
  expect(composerValue()).toBe("**ship** it");
  select(5, 7);
  fireEvent.keyDown(field(), { key: "X", metaKey: true, shiftKey: true });
  expect(composerValue()).toBe("**ship** ~~it~~");
});

it("W18-4: the bullet button starts a list on an empty line", () => {
  render(<Harness />);
  typeInComposer("");
  fireEvent.click(screen.getByRole("button", { name: "Bulleted list" }));
  expect(field().querySelector("ul li")).not.toBeNull();
  typeInComposer("- one\n- two");
  expect(field().querySelectorAll("ul li")).toHaveLength(2);
  expect(composerValue()).toBe("- one\n- two");
});

it("a code block from Shift+⌘C", () => {
  render(<Harness />);
  select(0, 0);
  fireEvent.keyDown(field(), { key: "C", metaKey: true, shiftKey: true });
  expect(composerValue()).toBe("```\nship it\n```");
});

it("suppresses the shortcut while the mention typeahead is open", () => {
  const members: RoomParticipant[] = [
    { kind: "agent", id: "agent-friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00.000Z", joinedSeq: 1 },
  ];
  render(<Harness members={members} />);
  typeInComposer("ship it @fri");
  expect(screen.getByRole("listbox", { name: "Mention a room member" })).toBeInTheDocument();
  fireEvent.keyDown(field(), { key: "b", metaKey: true });
  expect(composerValue()).toBe("ship it @fri");
});
