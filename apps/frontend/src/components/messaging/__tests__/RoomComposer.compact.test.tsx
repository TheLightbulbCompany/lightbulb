import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const live = vi.hoisted(() => ({ superAgent: true }));
vi.mock("@/hooks/useLiveEvents", () => ({ useSuperAgent: () => live.superAgent }));

import { RoomComposer, type RoomComposerProps } from "../RoomComposer";
import "@/test-utils/composer"; // the lazy editor, loaded

const base: RoomComposerProps = {
  text: "", onTextChange: vi.fn(), members: [], viewerId: "user_sam", onUploadAttachment: vi.fn(),
  clientMessageId: "11111111-1111-4111-8111-111111111111", sendState: "idle", onSubmit: vi.fn(),
};
beforeEach(() => { live.superAgent = true; localStorage.clear(); });
afterEach(() => { cleanup(); localStorage.clear(); });

it("one line with the formatting bar shown by default (founder, 6 Nov 2026); Aa hides it, and the choice is remembered per person", async () => {
  const { container, unmount } = render(<RoomComposer {...base} />);
  const form = container.querySelector("form")!;
  expect(form).toHaveAttribute("data-compact", "true");
  // One row: + · the text · Aa @ ↑.
  const row = form.querySelector(".room-composer-row")!;
  expect(row.querySelector("[contenteditable]")).not.toBeNull();
  expect(screen.getByRole("toolbar", { name: "Formatting" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Hide formatting" }));
  expect(screen.queryByRole("toolbar", { name: "Formatting" })).toBeNull();
  expect(localStorage.getItem("lightbulb.composer.format:user_sam")).toBe("0");
  unmount();
  render(<RoomComposer {...base} />);
  expect(screen.queryByRole("toolbar", { name: "Formatting" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Show formatting" }));
  expect(screen.getByRole("toolbar", { name: "Formatting" })).toBeInTheDocument();
  expect(localStorage.getItem("lightbulb.composer.format:user_sam")).toBe("1");
});

it("UX slice 4: + holds Attach files and Emoji (the emoji button left the row)", async () => {
  render(<RoomComposer {...base} />);
  expect(screen.queryByRole("button", { name: "Insert emoji" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Add" }));
  expect(await screen.findByRole("menuitem", { name: "Attach files" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("menuitem", { name: "Emoji" }));
  expect(await screen.findByRole("dialog", { name: "Choose an emoji" })).toBeInTheDocument();
});

it("switch off: the classic box, formatting bar shown by default, nothing stored", () => {
  live.superAgent = false;
  render(<RoomComposer {...base} />);
  expect(screen.getByRole("toolbar", { name: "Formatting" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Insert emoji" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Attach files" })).toBeInTheDocument();
  expect(localStorage.length).toBe(0);
});
