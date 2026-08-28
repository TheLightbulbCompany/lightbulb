"use client";

import * as React from "react";
import { Loader2, Save } from "lucide-react";
import type { FileInfo } from "@/hooks/useWorkspaceFiles";
import { notifyApiError } from "@/lib/notify";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { LobsterGraphView } from "@/components/chat/LobsterGraphView";
import { useScopedHotkey } from "@/hooks/useScopedHotkey";

const isSaveHotkey = (e: KeyboardEvent) => (e.metaKey || e.ctrlKey) && e.key === "s";

interface FileContentViewerProps {
  file: FileInfo | null;
  isLoading: boolean;
  error: Error | null;
  onSave?: (content: string) => Promise<void>;
  onDirtyChange?: (dirty: boolean) => void;
}

export function FileContentViewer({ file, isLoading, error, onSave, onDirtyChange }: FileContentViewerProps) {
  const [editContent, setEditContent] = React.useState("");
  const [originalContent, setOriginalContent] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);
  const rootRef = React.useRef<HTMLDivElement | null>(null);

  const dirty = editContent !== originalContent;

  // Report dirty state to parent so it can guard selection changes
  React.useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  // Sync editor content when a new file loads. Parent is responsible for
  // prompting the user about unsaved changes BEFORE changing the file prop.
  React.useEffect(() => {
    if (file?.content != null && !file.binary) {
      setEditContent(file.content);
      setOriginalContent(file.content);
    }
  }, [file?.path, file?.content, file?.binary]);

  const handleSave = React.useCallback(async () => {
    if (!onSave || editContent === originalContent) return;
    setSaving(true);
    try {
      await onSave(editContent);
      setOriginalContent(editContent);
    } catch (err) {
      notifyApiError(err, "Couldn't save the file");
    } finally {
      setSaving(false);
    }
  }, [onSave, editContent, originalContent]);

  // Cmd/Ctrl+S save shortcut, scoped to whichever viewer instance owns
  // focus — with several viewers docked side by side, one Cmd+S must not
  // save all of them.
  const canSave = Boolean(onSave && file && !file.binary && file.content != null);
  useScopedHotkey(rootRef, isSaveHotkey, handleSave, canSave);

  if (isLoading) {
    return (
      <div ref={rootRef} className="flex items-center justify-center h-full text-[var(--midnight-muted)]">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        Loading file...
      </div>
    );
  }

  if (error) {
    return (
      <div ref={rootRef} className="flex items-center justify-center h-full text-red-500 text-sm">
        Could not load file.
      </div>
    );
  }

  if (!file) {
    return (
      <div ref={rootRef} className="flex items-center justify-center h-full text-[var(--midnight-muted)] text-sm">
        Select a file to view its contents.
      </div>
    );
  }

  // Image preview (read-only)
  if (file.binary && file.mime_type.startsWith("image/") && file.content) {
    return (
      <div ref={rootRef} className="p-4 flex items-center justify-center">
        <img
          src={`data:${file.mime_type};base64,${file.content}`}
          alt={file.name}
          className="max-w-full max-h-[80vh] object-contain rounded border border-[var(--edge)]"
        />
      </div>
    );
  }

  // Binary file (read-only)
  if (file.binary || file.content === null) {
    return (
      <div ref={rootRef} className="flex items-center justify-center h-full text-[var(--midnight-muted)] text-sm">
        Binary file — preview not available.
      </div>
    );
  }

  // Editable text file. The header + textarea are shared by the plain
  // editor and the Code tab of the .lobster viewer. In the tabbed case the
  // root ref lives on the Tabs root (so tab triggers count as "inside"),
  // not here — a ref on the nested editor would be nulled out whenever the
  // Code tab unmounts, breaking the Cmd+S focus gate.
  const isLobster = file.name.endsWith(".lobster");
  const editor = (
    <div ref={isLobster ? undefined : rootRef} className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-1.5 bg-[var(--cream-deep)] border-b border-[var(--edge)] flex-shrink-0">
        <span className="text-xs text-[var(--midnight-muted)]">{file.name}</span>
        <div className="flex items-center gap-2">
          {dirty && <span className="text-[10px] text-amber-500 font-medium">unsaved</span>}
          <button
            onClick={handleSave}
            disabled={!dirty || saving || !onSave}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded bg-[var(--midnight)] text-white hover:bg-[#0a5c3e] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
            Save
          </button>
        </div>
      </div>
      <textarea
        ref={textareaRef}
        value={editContent}
        onChange={(e) => setEditContent(e.target.value)}
        className="flex-1 w-full p-4 text-sm font-mono bg-white text-[var(--midnight)] resize-none focus:outline-none"
        spellCheck={false}
      />
    </div>
  );

  // .lobster files get a Graph (default) | Code tab pair. Graph renders the
  // current editor content so it reflects unsaved edits when you switch back.
  if (isLobster) {
    return <LobsterTabbedViewer rootRef={rootRef} editor={editor} content={editContent} />;
  }

  return editor;
}

function LobsterTabbedViewer({
  editor,
  content,
  rootRef,
}: {
  editor: React.ReactNode;
  content: string;
  rootRef: React.Ref<HTMLDivElement>;
}) {
  const [tab, setTab] = React.useState<"graph" | "code">("graph");
  return (
    <Tabs ref={rootRef} value={tab} onValueChange={(v) => setTab(v as "graph" | "code")} className="flex flex-col h-full">
      {/* The line variant drops shadcn's default pill so the tabs read as part
          of the viewer's header band; active state is a flat green underline. */}
      <TabsList
        variant="line"
        className="flex-shrink-0 w-full justify-start gap-1 bg-[var(--cream-deep)] border-b border-[var(--edge)] px-2 py-1"
      >
        {/* Radix activates a trigger on pointer-down, not on a bare click;
            the explicit onClick keeps switching reliable under both real
            input and synthetic click events. */}
        <TabsTrigger
          value="graph"
          onClick={() => setTab("graph")}
          className="flex-none text-xs text-[var(--midnight-muted)] data-[state=active]:text-[var(--midnight)] data-[state=active]:after:bg-[var(--midnight)]"
        >
          Graph
        </TabsTrigger>
        <TabsTrigger
          value="code"
          onClick={() => setTab("code")}
          className="flex-none text-xs text-[var(--midnight-muted)] data-[state=active]:text-[var(--midnight)] data-[state=active]:after:bg-[var(--midnight)]"
        >
          Code
        </TabsTrigger>
      </TabsList>
      <TabsContent value="graph" className="flex-1 min-h-0 m-0">
        <LobsterGraphView content={content} onViewCode={() => setTab("code")} />
      </TabsContent>
      <TabsContent value="code" className="flex-1 min-h-0 m-0">
        {editor}
      </TabsContent>
    </Tabs>
  );
}
