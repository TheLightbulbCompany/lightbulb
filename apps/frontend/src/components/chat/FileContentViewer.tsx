"use client";

import * as React from "react";
import { Loader2, Save } from "lucide-react";
import type { FileInfo } from "@/hooks/useWorkspaceFiles";
import { notifyApiError } from "@/lib/notify";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { LobsterGraphView } from "@/components/chat/LobsterGraphView";
import { useScopedHotkey } from "@/hooks/useScopedHotkey";
import { FileViewerContent } from "@/components/media/FileViewerContent";

const isSaveHotkey = (e: KeyboardEvent) => (e.metaKey || e.ctrlKey) && e.key === "s";

/** Base64 (as returned by the workspace file-info endpoint) -> a Blob the
 * browser's native PDF renderer can be pointed at via an object URL. */
function base64ToBlob(base64: string, mimeType: string): Blob {
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  return new Blob([bytes], { type: mimeType });
}

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

  // PDFs arrive as base64 (like images) — turn them into an object URL for the
  // browser's native PDF renderer. Rebuilt whenever the selected file changes;
  // torn down on cleanup so a rapid file switch doesn't leak blob URLs.
  const pdfContent = file != null && file.binary && file.mime_type === "application/pdf" ? file.content : null;
  const [pdfUrl, setPdfUrl] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (!pdfContent) {
      setPdfUrl(null);
      return;
    }
    const url = URL.createObjectURL(base64ToBlob(pdfContent, "application/pdf"));
    setPdfUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [pdfContent]);

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
      <div ref={rootRef} className="flex items-center justify-center h-full text-err text-sm">
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
          className="max-w-full max-h-[80vh] object-contain rounded-sm border border-[var(--edge)]"
        />
      </div>
    );
  }

  // PDF preview (read-only) — the browser's own PDF renderer via <iframe>,
  // same as the chat-attachment viewer (FileViewerContent); no pdf.js.
  if (pdfContent) {
    return (
      <div ref={rootRef} className="h-full w-full p-2">
        {pdfUrl
          ? <FileViewerContent item={{ contentType: "application/pdf", fileName: file.name, size: file.size, url: pdfUrl }} className="rounded-sm border border-[var(--edge)]" />
          : <div className="flex items-center justify-center h-full text-[var(--midnight-muted)] text-sm">Loading preview…</div>}
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
      <div className="flex items-center justify-between px-3 py-1.5 bg-[var(--cream-deep)] border-b border-[var(--edge)] shrink-0">
        <span className="text-xs text-[var(--midnight-muted)]">{file.name}</span>
        <div className="flex items-center gap-2">
          {dirty && <span className="text-2xs text-warn font-medium">unsaved</span>}
          <button
            onClick={handleSave}
            disabled={!dirty || saving || !onSave}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded-sm bg-[var(--midnight)] text-cream hover:bg-[var(--midnight-soft)] disabled:bg-cream-deep disabled:text-[var(--midnight-muted)] disabled:hover:bg-cream-deep disabled:cursor-not-allowed transition-colors"
          >
            {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
            Save
          </button>
        </div>
      </div>
      <textarea aria-label="File contents"
        ref={textareaRef}
        value={editContent}
        onChange={(e) => setEditContent(e.target.value)}
        className="flex-1 w-full p-4 text-sm font-mono bg-cream text-[var(--midnight)] resize-none focus:outline-none"
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
        className="shrink-0 w-full justify-start gap-1 bg-[var(--cream-deep)] border-b border-[var(--edge)] px-2 py-1"
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
