"use client";

import "@mdxeditor/editor/style.css";
import {
  MDXEditor, codeBlockPlugin, codeMirrorPlugin, headingsPlugin, linkDialogPlugin, linkPlugin, listsPlugin,
  markdownShortcutPlugin, quotePlugin, tablePlugin, thematicBreakPlugin, toolbarPlugin,
  BlockTypeSelect, BoldItalicUnderlineToggles, CreateLink, InsertTable, InsertThematicBreak, ListsToggle, UndoRedo,
} from "@mdxeditor/editor";

/** A markdown editor over MDXEditor: headings, lists, quotes, rules, links, tables, code. No image upload, no mentions (later). */
export function MarkdownEditor({ value, onChange, readOnly = false, placeholder }: {
  value: string;
  onChange: (markdown: string) => void;
  readOnly?: boolean;
  placeholder?: string;
}) {
  return (
    <MDXEditor
      className="lb-editor"
      markdown={value}
      onChange={onChange}
      readOnly={readOnly}
      placeholder={placeholder}
      plugins={[
        headingsPlugin(), listsPlugin(), quotePlugin(), thematicBreakPlugin(), linkPlugin(), linkDialogPlugin(), tablePlugin(),
        codeBlockPlugin({ defaultCodeBlockLanguage: "" }),
        codeMirrorPlugin({ codeBlockLanguages: { "": "Plain", js: "JavaScript", ts: "TypeScript", py: "Python", bash: "Shell", json: "JSON", "openui-lang": "Card", mermaid: "Diagram" } }),
        markdownShortcutPlugin(),
        toolbarPlugin({ toolbarContents: () => <><UndoRedo /><BlockTypeSelect /><BoldItalicUnderlineToggles /><ListsToggle /><CreateLink /><InsertTable /><InsertThematicBreak /></> }),
      ]}
    />
  );
}
