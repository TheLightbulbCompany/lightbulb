// Lexical 0.48 (the copy MDXEditor already ships here) publishes its types only for TypeScript >= 5.2; this app is on
// 5.1, which reads a stub with no exports. The runtime is fine, so the composer reaches it through this one file, typed
// for exactly what ComposerEditor uses.
// ponytail: delete this file and import the packages directly once the frontend is on TypeScript >= 5.2.
import type { ComponentType, JSX, ReactNode } from "react";
import * as core from "lexical";
import * as composer from "@lexical/react/LexicalComposer";
import * as composerContext from "@lexical/react/LexicalComposerContext";
import * as contentEditable from "@lexical/react/LexicalContentEditable";
import * as richTextPlugin from "@lexical/react/LexicalRichTextPlugin";
import * as historyPlugin from "@lexical/react/LexicalHistoryPlugin";
import * as listPlugin from "@lexical/react/LexicalListPlugin";
import * as linkPlugin from "@lexical/react/LexicalLinkPlugin";
import * as markdownShortcutPlugin from "@lexical/react/LexicalMarkdownShortcutPlugin";
import * as errorBoundary from "@lexical/react/LexicalErrorBoundary";
import * as markdownModule from "@lexical/markdown";
import * as listModule from "@lexical/list";
import * as linkModule from "@lexical/link";
import * as richTextModule from "@lexical/rich-text";
import * as codeModule from "@lexical/code-core";
import * as selectionModule from "@lexical/selection";

export interface LexicalCommand<P> { readonly type?: string; readonly __payload?: P }
export type NodeClass = { getType(): string };
export type Transformer = object;

export interface LexicalNode {
  getKey(): string;
  getTextContent(): string;
  getParent(): LexicalNode | null;
  getTopLevelElement(): LexicalNode | null;
}
export interface TextNode extends LexicalNode {
  select(anchorOffset?: number, focusOffset?: number): RangeSelection;
}
export interface ListNode extends LexicalNode { getListType(): "bullet" | "number" | "check" }
export interface LinkNode extends LexicalNode { getURL(): string }
export interface Point { offset: number; getNode(): LexicalNode }
export interface RangeSelection {
  anchor: Point;
  focus: Point;
  isCollapsed(): boolean;
  insertText(text: string): void;
}
export interface RootNode extends LexicalNode {
  selectEnd(): RangeSelection;
  clear(): RootNode;
  getAllTextNodes(): TextNode[];
}
export interface EditorState { read<T>(fn: () => T): T }
export interface LexicalEditor {
  update(fn: () => void, options?: { discrete?: boolean; tag?: string }): void;
  getEditorState(): EditorState;
  registerUpdateListener(listener: (payload: { editorState: EditorState; tags: Set<string> }) => void): () => void;
  registerCommand<P>(command: LexicalCommand<P>, handler: (payload: P) => boolean, priority: number): () => void;
  dispatchCommand<P>(command: LexicalCommand<P>, payload: P): boolean;
  setEditable(editable: boolean): void;
  isComposing(): boolean;
  getRootElement(): HTMLElement | null;
}

type Core = {
  $createParagraphNode(): LexicalNode;
  $createTextNode(text: string): TextNode;
  $getNodeByKey(key: string): LexicalNode | null;
  $getRoot(): RootNode;
  $getSelection(): RangeSelection | object | null;
  $isRangeSelection(value: unknown): value is RangeSelection;
  $isTextNode(value: unknown): value is TextNode;
  BLUR_COMMAND: LexicalCommand<FocusEvent>;
  COMMAND_PRIORITY_CRITICAL: number;
  FORMAT_TEXT_COMMAND: LexicalCommand<"bold" | "italic" | "strikethrough" | "code">;
  INSERT_PARAGRAPH_COMMAND: LexicalCommand<void>;
  KEY_DOWN_COMMAND: LexicalCommand<KeyboardEvent>;
  PASTE_COMMAND: LexicalCommand<ClipboardEvent | KeyboardEvent | InputEvent>;
};
export const lexical = core as unknown as Core;

type InitialConfig = { namespace: string; theme: object; nodes: NodeClass[]; editable: boolean; onError: (error: Error) => void };
export const { LexicalComposer } = composer as unknown as { LexicalComposer: ComponentType<{ initialConfig: InitialConfig; children: ReactNode }> };
export const { useLexicalComposerContext } = composerContext as unknown as { useLexicalComposerContext(): [LexicalEditor] };
export const { ContentEditable } = contentEditable as unknown as {
  ContentEditable: ComponentType<Record<string, unknown> & { placeholder: JSX.Element; "aria-placeholder": string }>;
};
export const { RichTextPlugin } = richTextPlugin as unknown as { RichTextPlugin: ComponentType<{ contentEditable: JSX.Element; ErrorBoundary: unknown }> };
export const { HistoryPlugin } = historyPlugin as unknown as { HistoryPlugin: ComponentType };
export const { ListPlugin } = listPlugin as unknown as { ListPlugin: ComponentType };
export const { LinkPlugin } = linkPlugin as unknown as { LinkPlugin: ComponentType };
export const { MarkdownShortcutPlugin } = markdownShortcutPlugin as unknown as { MarkdownShortcutPlugin: ComponentType<{ transformers: Transformer[] }> };
export const { LexicalErrorBoundary } = errorBoundary as unknown as { LexicalErrorBoundary: unknown };

type TransformerName = "BOLD_ITALIC_STAR" | "BOLD_ITALIC_UNDERSCORE" | "BOLD_STAR" | "BOLD_UNDERSCORE" | "CODE" | "INLINE_CODE" | "ITALIC_STAR"
  | "ITALIC_UNDERSCORE" | "LINK" | "ORDERED_LIST" | "QUOTE" | "STRIKETHROUGH" | "UNORDERED_LIST";
export const markdown = markdownModule as unknown as Record<TransformerName, Transformer> & {
  $convertFromMarkdownString(markdown: string, transformers: Transformer[], node: undefined, shouldPreserveNewLines: boolean): void;
  $convertToMarkdownString(transformers: Transformer[], node: undefined, shouldPreserveNewLines: boolean): string;
};
export const list = listModule as unknown as {
  ListNode: NodeClass; ListItemNode: NodeClass;
  $isListNode(value: unknown): value is ListNode;
  INSERT_ORDERED_LIST_COMMAND: LexicalCommand<void>; INSERT_UNORDERED_LIST_COMMAND: LexicalCommand<void>; REMOVE_LIST_COMMAND: LexicalCommand<void>;
};
export const link = linkModule as unknown as { LinkNode: NodeClass; $isLinkNode(value: unknown): value is LinkNode; TOGGLE_LINK_COMMAND: LexicalCommand<string | null> };
export const richText = richTextModule as unknown as { QuoteNode: NodeClass };
export const code = codeModule as unknown as { CodeNode: NodeClass; CodeHighlightNode: NodeClass; $createCodeNode(): LexicalNode; $isCodeNode(value: unknown): boolean };
export const selection = selectionModule as unknown as { $setBlocksType(selection: RangeSelection, create: () => LexicalNode): void };
