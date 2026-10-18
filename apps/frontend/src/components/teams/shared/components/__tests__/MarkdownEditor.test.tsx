import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { MarkdownEditor } from "../MarkdownEditor";

// CodeMirror measures text ranges; jsdom has no layout.
Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
Range.prototype.getBoundingClientRect = () => new DOMRect();

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

it("typing ```openui-lang then Enter makes a block with that language", async () => {
  // Lexical does not run on jsdom typing, so start from the typed line (escaped so it imports as a
  // paragraph), put the caret at its end and press Enter: the real shortcut path from there on.
  const onChange = vi.fn();
  const { container } = render(<MarkdownEditor value={"\\`\\`\\`openui-lang"} onChange={onChange} />);
  await vi.waitFor(() => expect(container.querySelector(".lb-editor [contenteditable] p span")).not.toBeNull());
  const editable = container.querySelector(".lb-editor [contenteditable]") as HTMLElement;
  const text = editable.querySelector("p span")!.firstChild as Text;
  expect(text.data).toBe("```openui-lang");
  editable.focus();
  document.getSelection()!.collapse(text, text.length);
  document.dispatchEvent(new Event("selectionchange"));
  await new Promise((r) => setTimeout(r, 50));
  editable.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  await vi.waitFor(() => expect(onChange.mock.lastCall?.[0]).toBe("```openui-lang\n```"));
});

it("a block saved with backticks as its language opens as Plain, and picking Card sticks", async () => {
  const onChange = vi.fn();
  render(<MarkdownEditor value={"```&#x60;&#x60;&#x60;\nx = 1\n```"} onChange={onChange} />);
  await vi.waitFor(() => expect(screen.getByRole("combobox", { name: "Language" })).toHaveTextContent("Plain"));
  await userEvent.click(screen.getByRole("combobox", { name: "Language" }));
  await userEvent.click(await screen.findByRole("option", { name: "Card" }));
  await vi.waitFor(() => expect(onChange.mock.lastCall?.[0]).toBe("```openui-lang\nx = 1\n```"));
});
