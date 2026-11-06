import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";
import "@/test-utils/composer"; // the lazy editor, loaded

const base: RoomComposerProps = {
  text: "", onTextChange: vi.fn(), members: [],
  clientMessageId: "11111111-1111-4111-8111-111111111111", sendState: "idle", onSubmit: vi.fn(),
};
beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("asks to edit the last message when ArrowUp lands in an empty field", () => {
  const onEditLast = vi.fn();
  render(<RoomComposer {...base} onEditLast={onEditLast} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(onEditLast).toHaveBeenCalledOnce();
});

it("leaves ArrowUp alone once the field has text", () => {
  const onEditLast = vi.fn();
  render(<RoomComposer {...base} text="draft" onEditLast={onEditLast} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(onEditLast).not.toHaveBeenCalled();
});
