"use client";

import "@mdxeditor/editor/style.css";
import { useState } from "react";
import {
  MDXEditor, codeBlockPlugin, codeMirrorPlugin, headingsPlugin, imagePlugin, linkDialogPlugin, linkPlugin, listsPlugin,
  markdownShortcutPlugin, quotePlugin, tablePlugin, thematicBreakPlugin, toolbarPlugin, realmPlugin, createRootEditorSubscription$, CodeBlockNode,
  BlockTypeSelect, BoldItalicUnderlineToggles, CreateLink, InsertTable, InsertThematicBreak, ListsToggle, UndoRedo,
} from "@mdxeditor/editor";

const FENCE_LINE = /^[ \t]*`{3,}([\w-]+)$/;

/**
 * MDXEditor 4.3.3's ```lang shortcut passes the regex's FENCE group (`match[1]`) as the language, not
 * the language group, so "```openui-lang" + Enter made a block whose language was the backticks (saved as
 * ```&#x60;&#x60;&#x60;) and "openui-lang" was lost. Read the typed line just before the shortcut fires
 * and give the new block that language (a missed read leaves a plain block, never backticks). A body
 * saved with that bug opens with the bad info string dropped (`unbacktickFences`).
 * ponytail: drop this once MDXEditor's shortcut uses `match[2]`.
 */
const fenceLanguagePlugin = realmPlugin({
  init(realm) {
    let pending: string | null = null;
    const remember = (event: Event) => {
      const trigger = event.type === "keydown"
        ? (event as KeyboardEvent).key === "Enter" && !(event as KeyboardEvent).shiftKey
        : (event as InputEvent).data === " ";
      const anchor = trigger ? window.getSelection()?.anchorNode : null;
      const block = anchor && (anchor instanceof Element ? anchor : anchor.parentElement)?.closest("p");
      const match = block?.textContent?.match(FENCE_LINE);
      if (!match) return;
      pending = match[1];
      setTimeout(() => { pending = null; });
    };
    realm.pub(createRootEditorSubscription$, (editor) => {
      const offRoot = editor.registerRootListener((root: HTMLElement | null, previous: HTMLElement | null) => {
        previous?.removeEventListener("keydown", remember, true);
        previous?.removeEventListener("beforeinput", remember, true);
        root?.addEventListener("keydown", remember, true);
        root?.addEventListener("beforeinput", remember, true);
      });
      const offTransform = editor.registerNodeTransform(CodeBlockNode, (node: CodeBlockNode) => {
        if (!/^`+$/.test(node.getLanguage())) return;
        node.setLanguage(pending ?? "");
        pending = null;
      });
      return () => { offRoot(); offTransform(); };
    });
  },
});

/** "```&#x60;&#x60;&#x60;" (an OPENING fence whose info string is only escaped backticks, saved by MDXEditor 4.3.3) → "```". Lines inside a code block are left as written. */
export function unbacktickFences(markdown: string): string {
  let open: string | null = null; // the open fence's run, e.g. "```" or "~~~~"
  return markdown
    .split("\n")
    .map((line) => {
      const fence = /^[ \t]*(`{3,}|~{3,})(.*)$/.exec(line);
      if (!fence) return line;
      const [, run, rest] = fence;
      if (open) {
        if (run[0] === open[0] && run.length >= open.length && rest.trim() === "") open = null;
        return line;
      }
      open = run;
      return run[0] === "`" && /^(?:&#x60;)+[ \t]*$/.test(rest) ? line.slice(0, line.length - rest.length) : line;
    })
    .join("\n");
}

/** A markdown editor over MDXEditor: headings, lists, quotes, rules, links, tables, code, images by URL. No upload, no mentions (later). A body it cannot parse (raw HTML, JSX) opens as plain markdown text, never dropped. */
export function MarkdownEditor({ value, onChange, readOnly = false, placeholder }: {
  value: string;
  onChange: (markdown: string) => void;
  readOnly?: boolean;
  placeholder?: string;
}) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return <textarea aria-label="Body" className="lb-editor min-h-40 w-full rounded-md border bg-transparent p-2 font-mono text-ui" value={value} readOnly={readOnly} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />;
  }
  return (
    <MDXEditor
      onError={() => setFailed(true)}
      className="lb-editor"
      markdown={unbacktickFences(value)}
      onChange={onChange}
      readOnly={readOnly}
      placeholder={placeholder}
      plugins={[
        headingsPlugin(), listsPlugin(), quotePlugin(), thematicBreakPlugin(), linkPlugin(), linkDialogPlugin(), tablePlugin(), imagePlugin(),
        codeBlockPlugin({ defaultCodeBlockLanguage: "" }),
        codeMirrorPlugin({ codeBlockLanguages: { "": "Plain", js: "JavaScript", ts: "TypeScript", py: "Python", bash: "Shell", json: "JSON", "openui-lang": "Card", mermaid: "Diagram" } }),
        markdownShortcutPlugin(), fenceLanguagePlugin(),
        toolbarPlugin({ toolbarContents: () => <><UndoRedo /><BlockTypeSelect /><BoldItalicUnderlineToggles /><ListsToggle /><CreateLink /><InsertTable /><InsertThematicBreak /></> }),
      ]}
    />
  );
}
