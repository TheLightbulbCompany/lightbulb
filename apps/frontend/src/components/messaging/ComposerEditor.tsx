"use client";

// The room composer's text box: WYSIWYG like Slack (W18-4). Bold shows bold, the bullet button starts a list on an
// empty line, and typed markdown (**x**, - , 1. , `x`, ```) turns into formatting as you type. The message stays
// markdown: the box reads it in and writes it out with @lexical/markdown, the same Lexical 0.48 MDXEditor already
// ships in this app (MarkdownEditor), without MDXEditor's escaping ("3\*4") and "\\" line breaks a chat can't have.
import { useEffect, useImperativeHandle, useLayoutEffect, useRef, type Ref } from "react";
import {
  ContentEditable, HistoryPlugin, LexicalComposer, LexicalErrorBoundary, LinkPlugin, ListPlugin, MarkdownShortcutPlugin, RichTextPlugin,
  code, lexical, link as linkApi, list, markdown, richText, selection as selectionApi, useLexicalComposerContext,
  type LexicalCommand, type LexicalEditor, type LexicalNode, type Transformer,
} from "./lexical";
import { activeTrigger, type CompletionTrigger, type MarkdownStyle } from "@lightbulb/shared";

/** What a chat message can carry. No headings: "# 1" in a chat is text. */
export const COMPOSER_TRANSFORMERS: Transformer[] = ([
  "UNORDERED_LIST", "ORDERED_LIST", "QUOTE", "CODE", "INLINE_CODE", "BOLD_ITALIC_STAR", "BOLD_ITALIC_UNDERSCORE", "BOLD_STAR", "BOLD_UNDERSCORE",
  "ITALIC_STAR", "ITALIC_UNDERSCORE", "STRIKETHROUGH", "LINK",
] as const).map((name) => markdown[name]);

const { $createParagraphNode, $getNodeByKey, $getRoot, $getSelection, $isRangeSelection, $isTextNode, BLUR_COMMAND, COMMAND_PRIORITY_CRITICAL,
  FORMAT_TEXT_COMMAND, INSERT_PARAGRAPH_COMMAND, KEY_DOWN_COMMAND, PASTE_COMMAND } = lexical;
const { $convertFromMarkdownString, $convertToMarkdownString } = markdown;
const { $isListNode, INSERT_ORDERED_LIST_COMMAND, INSERT_UNORDERED_LIST_COMMAND, REMOVE_LIST_COMMAND } = list;
const { $isLinkNode, TOGGLE_LINK_COMMAND } = linkApi;
const { $createCodeNode, $isCodeNode } = code;
const { $setBlocksType } = selectionApi;

/** The message the box holds, as markdown (one "\n" per line, as typed). An underscore inside a word (an email,
 *  snake_case) can never be emphasis, so it goes out bare: "clerk_test@…", not "clerk\_test@…" for agents to copy. */
export const $composerMarkdown = () => $convertToMarkdownString(COMPOSER_TRANSFORMERS, undefined, true).replace(/(\w)\\_(?=\w)/g, "$1_");

export interface ComposerEditorHandle {
  /** Focus with the caret at the end. */
  focus(): void;
  insertText(value: string): void;
  /** Swap the `@que`/`:emo` being typed for `token` and a space. */
  replaceTrigger(token: string): void;
  format(style: Exclude<MarkdownStyle, "link">): void;
  /** Link the selection to `url`, or unlink it when `url` is null. False when nothing is selected. */
  link(url: string | null): boolean;
  /** The selection's link, if it is in one. */
  selectedLink(): string | null;
  /** Select the `occurrence`th (0-based) `needle` in the box. */
  selectText(needle: string, occurrence: number): void;
}

export interface ComposerEditorProps {
  id: string;
  value: string;
  onChange: (markdown: string) => void;
  onTrigger: (trigger: CompletionTrigger | null) => void;
  editable: boolean;
  placeholder: string;
  label: string;
  /** True when it handled the key (the box does nothing more with it). */
  onKeyDown: (event: KeyboardEvent) => boolean;
  onPasteFiles: (files: File[]) => void;
  onBlur: () => void;
  ariaProps: Record<`aria-${string}`, string | boolean | undefined>;
  handleRef: Ref<ComposerEditorHandle>;
}

const FORMATS = { bold: "bold", italic: "italic", strike: "strikethrough", code: "code" } as const;

function $topBlock(node: LexicalNode): LexicalNode {
  return node.getTopLevelElement() ?? node;
}

function Bridge({ value, onChange, onTrigger, editable, onKeyDown, onPasteFiles, onBlur, handleRef }: ComposerEditorProps) {
  const [editor] = useLexicalComposerContext();
  // The markdown this box last read in or wrote out: a `value` that differs came from outside (a draft, a starter, a send).
  const known = useRef<string | null>(null);
  const trigger = useRef<{ key: string; trigger: CompletionTrigger } | null>(null);
  const latest = useRef({ onChange, onTrigger, onKeyDown, onPasteFiles, onBlur });
  useLayoutEffect(() => { latest.current = { onChange, onTrigger, onKeyDown, onPasteFiles, onBlur }; });

  useLayoutEffect(() => {
    if (value === known.current) return;
    known.current = value;
    editor.update(() => {
      $convertFromMarkdownString(value, COMPOSER_TRANSFORMERS, undefined, true);
      // A box that has focus keeps typing at the end of what arrived (a starter, a restored draft).
      if (editor.getRootElement()?.contains(document.activeElement)) $getRoot().selectEnd();
    }, { discrete: true, tag: "composer-external" });
  }, [editor, value]);

  useEffect(() => { editor.setEditable(editable); }, [editor, editable]);

  useEffect(() => editor.registerUpdateListener(({ editorState, tags }) => {
    const markdown = editorState.read($composerMarkdown);
    if (!tags.has("composer-external") && markdown !== known.current) {
      known.current = markdown;
      latest.current.onChange(markdown);
    }
    const next = editor.isComposing() ? null : editorState.read(() => {
      const selection = $getSelection();
      if (!$isRangeSelection(selection) || !selection.isCollapsed()) return null;
      const node = selection.anchor.getNode();
      if (!$isTextNode(node)) return null;
      const before = node.getTextContent().slice(0, selection.anchor.offset);
      const found = activeTrigger(before, before.length);
      return found && { key: node.getKey(), trigger: found };
    });
    const previous = trigger.current?.trigger;
    trigger.current = next;
    if (previous?.kind !== next?.trigger.kind || previous?.query !== next?.trigger.query || previous?.start !== next?.trigger.start) {
      latest.current.onTrigger(next?.trigger ?? null);
    }
  }), [editor]);

  useEffect(() => {
    const offKey = editor.registerCommand(KEY_DOWN_COMMAND, (event) => {
      if (latest.current.onKeyDown(event)) {
        event.preventDefault();
        return true;
      }
      // Shift+Enter in a list is a new bullet (Slack); elsewhere it stays a new line.
      if (event.key === "Enter" && event.shiftKey) {
        const inList = editor.getEditorState().read(() => {
          const selection = $getSelection();
          return $isRangeSelection(selection) && $isListNode($topBlock(selection.anchor.getNode()));
        });
        if (inList) {
          event.preventDefault();
          editor.dispatchCommand(INSERT_PARAGRAPH_COMMAND, undefined);
          return true;
        }
      }
      return false;
    }, COMMAND_PRIORITY_CRITICAL);
    const offPaste = editor.registerCommand(PASTE_COMMAND, (event) => {
      const files = event instanceof ClipboardEvent ? [...(event.clipboardData?.files ?? [])] : [];
      if (!files.length) return false;
      event.preventDefault();
      latest.current.onPasteFiles(files);
      return true;
    }, COMMAND_PRIORITY_CRITICAL);
    const offBlur = editor.registerCommand(BLUR_COMMAND, () => { latest.current.onBlur(); return false; }, COMMAND_PRIORITY_CRITICAL);
    return () => { offKey(); offPaste(); offBlur(); };
  }, [editor]);

  useImperativeHandle(handleRef, () => {
    // Commands dispatched from a click commit at once (outside an update Lexical defers them to a microtask).
    const dispatch = <P,>(command: LexicalCommand<P>, payload: P) => editor.update(() => { editor.dispatchCommand(command, payload); }, { discrete: true });
    const $selection = () => {
      const selection = $getSelection();
      return $isRangeSelection(selection) ? selection : $getRoot().selectEnd();
    };
    return {
      focus: () => {
        editor.update(() => { $getRoot().selectEnd(); }, { discrete: true });
        editor.getRootElement()?.focus({ preventScroll: true });
      },
      insertText: (text) => {
        editor.update(() => { $selection().insertText(text); }, { discrete: true });
        editor.getRootElement()?.focus({ preventScroll: true });
      },
      replaceTrigger: (token) => {
        const active = trigger.current;
        if (!active) return;
        editor.update(() => {
          const node = $getNodeByKey(active.key);
          if (!$isTextNode(node)) return;
          const after = node.getTextContent().slice(active.trigger.end);
          node.select(active.trigger.start, active.trigger.end).insertText(after.startsWith(" ") ? token : `${token} `);
        }, { discrete: true });
        editor.getRootElement()?.focus({ preventScroll: true });
      },
      format: (style) => {
        if (style in FORMATS) {
          dispatch(FORMAT_TEXT_COMMAND, FORMATS[style as keyof typeof FORMATS]);
        } else if (style === "ul" || style === "ol") {
          const type = style === "ul" ? "bullet" : "number";
          const current = editor.getEditorState().read(() => {
            const selection = $getSelection();
            const block = $isRangeSelection(selection) ? $topBlock(selection.anchor.getNode()) : null;
            return $isListNode(block) ? block.getListType() : null;
          });
          dispatch(current === type ? REMOVE_LIST_COMMAND : type === "bullet" ? INSERT_UNORDERED_LIST_COMMAND : INSERT_ORDERED_LIST_COMMAND, undefined);
        } else if (style === "codeblock") {
          editor.update(() => {
            const selection = $selection();
            const inCode = $isCodeNode($topBlock(selection.anchor.getNode()));
            $setBlocksType(selection, () => (inCode ? $createParagraphNode() : $createCodeNode()));
          }, { discrete: true });
        }
        editor.getRootElement()?.focus({ preventScroll: true });
      },
      link: (url) => {
        const hasText = editor.getEditorState().read(() => {
          const selection = $getSelection();
          return $isRangeSelection(selection) && (!selection.isCollapsed() || url === null);
        });
        if (!hasText) return false;
        dispatch(TOGGLE_LINK_COMMAND, url);
        editor.getRootElement()?.focus({ preventScroll: true });
        return true;
      },
      selectedLink: () => editor.getEditorState().read(() => {
        const selection = $getSelection();
        if (!$isRangeSelection(selection)) return null;
        const node = selection.anchor.getNode();
        const link = $isLinkNode(node) ? node : node.getParent();
        return $isLinkNode(link) ? link.getURL() : null;
      }),
      selectText: (needle, occurrence) => {
        editor.update(() => {
          let seen = 0;
          for (const node of $getRoot().getAllTextNodes()) {
            let at = node.getTextContent().indexOf(needle);
            while (at >= 0) {
              if (seen++ === occurrence) { node.select(at, at + needle.length); return; }
              at = node.getTextContent().indexOf(needle, at + 1);
            }
          }
        });
      },
    };
  }, [editor]);
  return null;
}

const theme = {
  paragraph: "room-editor-p",
  quote: "room-editor-quote",
  code: "room-editor-codeblock",
  link: "room-editor-link",
  list: { ul: "room-editor-ul", ol: "room-editor-ol", listitem: "room-editor-li", nested: { listitem: "room-editor-nested" } },
  text: { bold: "font-semibold", italic: "italic", strikethrough: "line-through", code: "room-editor-code" },
};

export function ComposerEditor(props: ComposerEditorProps) {
  return <LexicalComposer initialConfig={{
    namespace: "room-composer",
    theme,
    nodes: [list.ListNode, list.ListItemNode, richText.QuoteNode, code.CodeNode, code.CodeHighlightNode, linkApi.LinkNode],
    editable: props.editable,
    onError: (error: Error) => { throw error; },
  }}>
    <div className="room-editor">
      <RichTextPlugin
        contentEditable={<ContentEditable id={props.id} className="room-editor-input" aria-label={props.label} aria-multiline
          {...props.ariaProps} aria-placeholder={props.placeholder}
          placeholder={<div className="room-editor-placeholder" aria-hidden="true">{props.placeholder}</div>} />}
        ErrorBoundary={LexicalErrorBoundary} />
    </div>
    <HistoryPlugin />
    <ListPlugin />
    <LinkPlugin />
    <MarkdownShortcutPlugin transformers={COMPOSER_TRANSFORMERS} />
    <Bridge {...props} />
  </LexicalComposer>;
}

/** Tests: the editor behind a rendered composer box. */
export function editorOf(element: Element): LexicalEditor {
  return (element as unknown as { __lexicalEditor: LexicalEditor }).__lexicalEditor;
}
