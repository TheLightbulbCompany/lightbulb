"use client";

// The company Brain — a browser + editor over the memory-wiki vault the
// agents read and write on EFS (wiki/, served by routers/brain.py). Owners
// trust a brain they can audit: every page shows its size and modified time,
// and a wrong fact is fixed by editing it in place. Reuses FileTree +
// FileContentViewer (editor, dirty tracking, scoped Cmd+S come free).

import * as React from "react";
import { useSWRConfig } from "swr";
import { BookOpen, Trash2, TriangleAlert } from "lucide-react";
import { PanelMasthead, PanelEmptyState } from "@/components/teams/shell";
import { FileTree } from "@/components/chat/FileTree";
import { FileContentViewer } from "@/components/chat/FileContentViewer";
import { useBrainTree, useBrainFile, useBrainMutations } from "@/hooks/useBrainFiles";
import type { FileInfo } from "@/hooks/useWorkspaceFiles";
import { openChatPanel } from "@/components/workspace/workspaceController";
import { DEFAULT_AGENT_SENTINEL } from "@/components/workspace/panelRegistry";
import { notifyApiError } from "@/lib/notify";
import { formatBytes } from "@/lib/utils";

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

  // A second `?path=` deep link into an already-open panel focuses it rather
  // than remounting (workspaceController's addOrFocus/updateParameters), so
  // the seed-only useState above never sees the new value on its own. Re-apply
  // it here — same prev-prop-compare idiom as InboxPage.tsx's `?tab` handling.
  const [prevInitialPath, setPrevInitialPath] = React.useState(initialPath);
  if (initialPath !== prevInitialPath) {
    setPrevInitialPath(initialPath);
    if (initialPath) setSelectedPath(initialPath);
  }

  const { files, isLoading: treeLoading, error: treeError, refresh } = useBrainTree();
  const { file, isLoading: fileLoading, error: fileError, key: fileKey } = useBrainFile(selectedPath);
  const { saveBrainFile, deleteBrainFile } = useBrainMutations();
  const { mutate: mutateCache } = useSWRConfig();

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
      const fresh = await saveBrainFile(selectedPath, content);
      // The audit line (size · modified) must go fresh the instant the owner's
      // own edit lands — merge the PUT's fresh stat straight into the file's
      // own SWR cache entry (no refetch) instead of waiting on the next read.
      if (fileKey) {
        void mutateCache(
          fileKey,
          (current?: FileInfo) =>
            current ? { ...current, size: fresh.size, modified_at: fresh.modified_at, content } : current,
          false,
        );
      }
      void refresh();
    },
    [selectedPath, saveBrainFile, refresh, fileKey, mutateCache],
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
    // The deleted file's content never clears from FileContentViewer's own
    // dirty-diff state (its sync effect only runs for a non-null file), so
    // without this a stale "unsaved changes" prompt fires on the next click.
    setEditorDirty(false);
    void refresh();
  }, [selectedPath, deleteBrainFile, refresh]);

  const isEmpty = !treeLoading && !treeError && files.length === 0;

  return (
    <div className="editorial-page h-full min-h-0 w-full p-4 sm:p-5 lg:p-6 flex flex-col gap-4">
      <PanelMasthead
        overline="Knowledge"
        title="Brain"
        meta={
          selectedPath && file
            ? `${formatBytes(file.size)} · ${formatDate(file.modified_at)}`
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

      {treeError ? (
        <PanelEmptyState
          icon={TriangleAlert}
          headline="Couldn't load your brain."
          supporting="Something went wrong reaching your company's memory."
          action={{ label: "Retry", onClick: () => void refresh() }}
        />
      ) : isEmpty ? (
        <PanelEmptyState
          icon={BookOpen}
          headline="No pages yet."
          supporting="Your company's brain fills as your agents learn."
          action={{ label: "Ask Friday", onClick: () => openChatPanel(DEFAULT_AGENT_SENTINEL) }}
        />
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
