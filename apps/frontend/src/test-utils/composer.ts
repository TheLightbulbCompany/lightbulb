// Driving the room composer's WYSIWYG box in jsdom, where Lexical does not see typed keystrokes: these set its text
// and selection through the editor itself, and read back the markdown it would send.
import { act, screen } from "@testing-library/react";
import { $composerMarkdown, COMPOSER_TRANSFORMERS, editorOf } from "@/components/messaging/ComposerEditor";
import { lexical, markdown } from "@/components/messaging/lexical";

export const composerBox = (name: string | RegExp = "Message") => screen.getByRole("textbox", { name });

/** Replace the box's text with `value` (markdown), caret at the end — like typing it. */
export function typeInComposer(value: string, box: HTMLElement = composerBox()): void {
  act(() => {
    editorOf(box).update(() => {
      markdown.$convertFromMarkdownString(value, COMPOSER_TRANSFORMERS, undefined, true);
      lexical.$getRoot().selectEnd();
    }, { discrete: true });
  });
}

/** Select [start, end) of the box's text (both inside one run of same-format text). */
export function selectInComposer(start: number, end: number, box: HTMLElement = composerBox()): void {
  act(() => {
    editorOf(box).update(() => {
      let seen = 0;
      for (const node of lexical.$getRoot().getAllTextNodes()) {
        const length = node.getTextContent().length;
        if (start <= seen + length) { node.select(start - seen, end - seen); return; }
        seen += length;
      }
    }, { discrete: true });
  });
}

/** The markdown the box would send. */
export function composerValue(box: HTMLElement = composerBox()): string {
  return editorOf(box).getEditorState().read($composerMarkdown);
}

/** The caret's offset in the box's text, or null without a collapsed selection. */
export function caretOffset(box: HTMLElement = composerBox()): number | null {
  return editorOf(box).getEditorState().read(() => {
    const selection = lexical.$getSelection();
    if (!lexical.$isRangeSelection(selection) || !selection.isCollapsed()) return null;
    let seen = 0;
    for (const node of lexical.$getRoot().getAllTextNodes()) {
      if (node.getKey() === selection.anchor.getNode().getKey()) return seen + selection.anchor.offset;
      seen += node.getTextContent().length;
    }
    return null;
  });
}
