import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { MarkdownEditor } from "../MarkdownEditor";

it("an image body opens in the rich editor", async () => {
  const { container } = render(<MarkdownEditor value={"Hello\n\n![logo](https://example.com/a.png)"} onChange={vi.fn()} />);
  await vi.waitFor(() => expect(container.querySelector(".lb-editor [contenteditable]")).not.toBeNull());
  expect(screen.queryByRole("textbox", { name: "Body" })).toBeNull();
});

it("a construct the editor cannot parse falls back to the raw markdown, editable", async () => {
  // A footnote is a construct MDXEditor 4.3.3 cannot import; the real editor runs in jsdom, so no mock.
  const raw = "Before[^1]\n\n[^1]: note\n";
  const onChange = vi.fn();
  render(<MarkdownEditor value={raw} onChange={onChange} />);
  const box = await screen.findByRole("textbox", { name: "Body" });
  expect(box).toHaveValue(raw);
  await userEvent.type(box, "!");
  expect(onChange).toHaveBeenLastCalledWith(raw + "!");
});
