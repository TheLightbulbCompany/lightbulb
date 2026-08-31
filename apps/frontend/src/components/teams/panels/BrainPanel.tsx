"use client";

// The company Brain — a browser + editor over the memory-wiki vault the
// agents read and write on EFS (wiki/, served by routers/brain.py). Owners
// trust a brain they can audit: every page shows its size and modified time,
// and a wrong fact is fixed by editing it in place. Reuses FileTree +
// FileContentViewer (editor, dirty tracking, scoped Cmd+S come free).

import * as React from "react";
import { Trash2 } from "lucide-react";
import { PanelMasthead } from "@/components/teams/shell";
import { FileTree } from "@/components/chat/FileTree";
import { FileContentViewer } from "@/components/chat/FileContentViewer";
import { useBrainTree, useBrainFile, useBrainMutations } from "@/hooks/useBrainFiles";
import { openChatPanel } from "@/components/workspace/workspaceController";
import { DEFAULT_AGENT_SENTINEL } from "@/components/workspace/panelRegistry";
import { notifyApiError } from "@/lib/notify";

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString();
}

export interface BrainPanelProps {
  /** ?path deep link (registry-durable) — selects a page on mount. */
  initialPath?: string | null;
}

export function BrainPanel({ initialPath }: BrainPanelProps = {}) {
  const [selectedPath, setSelectedPath] = React.useState<string | null>(initialPath ?? null);
  const [editorDirty, setEditorDirty] = React.useState(false);

  const { files, isLoading: treeLoading, refresh } = useBrainTree();
  const { file, isLoading: fileLoading, error: fileError } = useBrainFile(selectedPath);
  const { saveBrainFile, deleteBrainFile } = useBrainMutations();

  const confirmDiscardIfDirty = React.useCallback((): boolean => {
    if (!editorDirty) return true;
    return window.confirm("You have unsaved changes. Discard them?");
  }, [editorDirty]);

  const handleSelect = React.useCallback(
    (path: string) => {
      if (path === selectedPath) return;
      if (!confirmDiscardIfDirty()) return;
      setSelectedPath(path);
    },
    [selectedPath, confirmDiscardIfDirty],
  );

  const handleSave = React.useCallback(
    async (content: string) => {
      if (!selectedPath) return;
      await saveBrainFile(selectedPath, content);
      // Pick up the fresh size/modified time — the audit trail owners see.
      void refresh();
    },
    [selectedPath, saveBrainFile, refresh],
  );

  const handleDelete = React.useCallback(async () => {
    if (!selectedPath) return;
    if (!window.confirm(`Delete ${selectedPath}? The agents will no longer see this page.`)) return;
    try {
      await deleteBrainFile(selectedPath);
    } catch (err) {
      notifyApiError(err, "Couldn't delete the page");
      return;
    }
    setSelectedPath(null);
    void refresh();
  }, [selectedPath, deleteBrainFile, refresh]);

  const isEmpty = !treeLoading && files.length === 0;

  return (
    <div className="editorial-page h-full min-h-0 w-full p-4 sm:p-5 lg:p-6 flex flex-col gap-4">
      <PanelMasthead
        overline="Knowledge"
        title="Brain"
        meta={
          selectedPath && file
            ? `${formatFileSize(file.size)} · ${formatDate(file.modified_at)}`
            : undefined
        }
        actions={
          selectedPath ? (
            <button
              type="button"
              onClick={handleDelete}
              aria-label="Delete page"
              className="flex items-center gap-1 px-2 py-1 text-xs rounded-sm text-[var(--midnight-muted)] hover:text-err transition-colors"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Delete
            </button>
          ) : undefined
        }
      />

      {isEmpty ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-4 text-center">
          <p className="text-sm text-[var(--midnight-muted)]">
            Your company&apos;s brain fills as your agents learn.
          </p>
          <button
            type="button"
            onClick={() => openChatPanel(DEFAULT_AGENT_SENTINEL)}
            className="rounded-full bg-[var(--midnight)] px-4 py-1.5 text-sm text-cream hover:bg-[var(--midnight-soft)] transition-colors"
          >
            Ask Friday
          </button>
        </div>
      ) : (
        <div className="flex flex-1 min-h-0 rounded-sm border border-[var(--edge)] bg-cream overflow-hidden">
          <div className="w-64 shrink-0 border-r border-[var(--edge)] min-h-0 overflow-y-auto">
            <FileTree
              files={files}
              selectedPath={selectedPath}
              onSelect={handleSelect}
              onRefresh={() => void refresh()}
              isLoading={treeLoading}
              emptyMessage="No pages yet."
            />
          </div>
          <div className="flex-1 min-h-0 min-w-0">
            <FileContentViewer
              file={file}
              isLoading={fileLoading}
              error={fileError ?? null}
              onSave={selectedPath ? handleSave : undefined}
              onDirtyChange={setEditorDirty}
            />
          </div>
        </div>
      )}
    </div>
  );
}
