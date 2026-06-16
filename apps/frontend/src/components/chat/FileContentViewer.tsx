"use client";

import * as React from "react";
import { Loader2, Save } from "lucide-react";
import type { FileInfo } from "@/hooks/useWorkspaceFiles";
import { notifyApiError } from "@/lib/notify";

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

  // Cmd/Ctrl+S save shortcut — refs avoid stale closure
  const handleSaveRef = React.useRef(handleSave);
  handleSaveRef.current = handleSave;

  const canSaveRef = React.useRef(false);
  canSaveRef.current = Boolean(
    onSave && file && !file.binary && file.content != null,
  );

  React.useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "s" && canSaveRef.current) {
        e.preventDefault();
        handleSaveRef.current();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full text-[#8a8578]">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        Loading file...
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full text-red-500 text-sm">
        Could not load file.
      </div>
    );
  }

  if (!file) {
    return (
      <div className="flex items-center justify-center h-full text-[#8a8578] text-sm">
        Select a file to view its contents.
      </div>
    );
  }

  // Image preview (read-only)
  if (file.binary && file.mime_type.startsWith("image/") && file.content) {
    return (
      <div className="p-4 flex items-center justify-center">
        <img
          src={`data:${file.mime_type};base64,${file.content}`}
          alt={file.name}
          className="max-w-full max-h-[80vh] object-contain rounded border border-[#e0dbd0]"
        />
      </div>
    );
  }

  // Binary file (read-only)
  if (file.binary || file.content === null) {
    return (
      <div className="flex items-center justify-center h-full text-[#8a8578] text-sm">
        Binary file — preview not available.
      </div>
    );
  }

  // Editable text file
  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-1.5 bg-[#f3efe6] border-b border-[#e0dbd0] flex-shrink-0">
        <span className="text-xs text-[#8a8578]">{file.name}</span>
        <div className="flex items-center gap-2">
          {dirty && <span className="text-[10px] text-amber-500 font-medium">unsaved</span>}
          <button
            onClick={handleSave}
            disabled={!dirty || saving || !onSave}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded bg-[#06402B] text-white hover:bg-[#0a5c3e] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
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
        className="flex-1 w-full p-4 text-sm font-mono bg-white text-[#1a1a1a] resize-none focus:outline-none"
        spellCheck={false}
      />
    </div>
  );
}
