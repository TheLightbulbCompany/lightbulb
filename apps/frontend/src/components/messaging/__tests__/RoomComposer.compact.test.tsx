import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";


import { RoomComposer, type RoomComposerProps } from "../RoomComposer";
import "@/test-utils/composer"; // the lazy editor, loaded

const base: RoomComposerProps = {
  text: "", onTextChange: vi.fn(), members: [], viewerId: "user_sam", onUploadAttachment: vi.fn(),
  clientMessageId: "11111111-1111-4111-8111-111111111111", sendState: "idle", onSubmit: vi.fn(),
};
beforeEach(() => { localStorage.clear(); });
afterEach(() => { cleanup(); localStorage.clear(); });

it("one line with no formatting bar (plain text, voice pivot)", () => {
  const { container } = render(<RoomComposer {...base} />);
  const form = container.querySelector("form")!;
  expect(form).toHaveAttribute("data-compact", "true");
  // One row: + · the text · send.
  const row = form.querySelector(".room-composer-row")!;
  expect(row.querySelector("[contenteditable]")).not.toBeNull();
  expect(screen.queryByRole("toolbar", { name: "Formatting" })).toBeNull();
  expect(screen.queryByRole("button", { name: /formatting/ })).toBeNull();
  expect(screen.queryByRole("button", { name: "Mention someone" })).toBeNull();
});

it("UX slice 4: + holds Attach files and Emoji (the emoji button left the row)", async () => {
  render(<RoomComposer {...base} />);
  expect(screen.queryByRole("button", { name: "Insert emoji" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Add" }));
  expect(await screen.findByRole("menuitem", { name: "Attach files" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("menuitem", { name: "Emoji" }));
  expect(await screen.findByRole("dialog", { name: "Choose an emoji" })).toBeInTheDocument();
});

