"use client";

import "@mdxeditor/editor/style.css";
import { useState } from "react";
import {
  MDXEditor, codeBlockPlugin, codeMirrorPlugin, headingsPlugin, imagePlugin, linkDialogPlugin, linkPlugin, listsPlugin,
  markdownShortcutPlugin, quotePlugin, tablePlugin, thematicBreakPlugin, toolbarPlugin,
  BlockTypeSelect, BoldItalicUnderlineToggles, CreateLink, InsertTable, InsertThematicBreak, ListsToggle, UndoRedo,
} from "@mdxeditor/editor";

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
      markdown={value}
      onChange={onChange}
      readOnly={readOnly}
      placeholder={placeholder}
      plugins={[
        headingsPlugin(), listsPlugin(), quotePlugin(), thematicBreakPlugin(), linkPlugin(), linkDialogPlugin(), tablePlugin(), imagePlugin(),
        codeBlockPlugin({ defaultCodeBlockLanguage: "" }),
        codeMirrorPlugin({ codeBlockLanguages: { "": "Plain", js: "JavaScript", ts: "TypeScript", py: "Python", bash: "Shell", json: "JSON", "openui-lang": "Card", mermaid: "Diagram" } }),
        markdownShortcutPlugin(),
        toolbarPlugin({ toolbarContents: () => <><UndoRedo /><BlockTypeSelect /><BoldItalicUnderlineToggles /><ListsToggle /><CreateLink /><InsertTable /><InsertThematicBreak /></> }),
      ]}
    />
  );
}
