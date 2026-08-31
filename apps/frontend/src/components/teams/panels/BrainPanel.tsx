"use client";

// The company Brain — a browser + editor over the memory-wiki vault the
// agents read and write on EFS (wiki/, served by routers/brain.py). Owners
// trust a brain they can audit: every page shows its size and modified time,
// and a wrong fact is fixed by editing it in place. Reuses FileTree +
// FileContentViewer (editor, dirty tracking, scoped Cmd+S come free).
//
// Two tabs: Notes (the vault above) and Dreams (the memory-core dreaming
// status/diary/advanced surface, folded in from its retired standalone
// control panel — see RETIRED_PANEL_ALIASES's `dreams` entry). Dreams
// renders DreamsPanel's body verbatim; its file is untouched, only its
// registration moved.

import * as React from "react";
import { useSWRConfig } from "swr";
import { BookOpen, Trash2, TriangleAlert, Upload } from "lucide-react";
import { PanelMasthead, PanelEmptyState, PanelTabs } from "@/components/teams/shell";
import { FileTree } from "@/components/chat/FileTree";
import { FileContentViewer } from "@/components/chat/FileContentViewer";
import { DreamsPanel } from "@/components/control/panels/DreamsPanel";
import { useBrainTree, useBrainFile, useBrainMutations } from "@/hooks/useBrainFiles";
import type { FileInfo } from "@/hooks/useWorkspaceFiles";
import { openChatPanel } from "@/components/workspace/workspaceController";
import { DEFAULT_AGENT_SENTINEL } from "@/components/workspace/panelRegistry";
import { notifyApiError } from "@/lib/notify";
import { formatBytes } from "@/lib/utils";

type BrainTab = "notes" | "dreams";
const BRAIN_TABS = [
  { value: "notes", label: "Notes" },
  { value: "dreams", label: "Dreams" },
];

function formatDate(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString();
}

export interface BrainPanelProps {
  /** ?path deep link (registry-durable) — selects a page on mount. */
  initialPath?: string | null;
  /** Seeds which tab is active on mount (e.g. the retired ?panel=dreams alias
   *  landing on Dreams instead of silently defaulting to Notes). One-time
   *  seed only — unlike initialPath there is no re-apply-on-prop-change sync,
   *  since a re-focus of an already-open panel has no reason to yank the
   *  owner back to a specific tab. */
  initialTab?: BrainTab | null;
}

export function BrainPanel({ initialPath, initialTab }: BrainPanelProps = {}) {
  const [tab, setTab] = React.useState<BrainTab>(initialTab === "dreams" ? "dreams" : "notes");
  const [selectedPath, setSelectedPath] = React.useState<string | null>(initialPath ?? null);
  const [editorDirty, setEditorDirty] = React.useState(false);

  // A second `?path=` deep link into an already-open panel focuses it rather
  // than remounting (workspaceController's addOrFocus/updateParameters), so
  // the seed-only useState above never sees the new value on its own. Re-apply
  // it here — same prev-prop-compare idiom as InboxPage.tsx's `?tab` handling.
  // If the editor holds unsaved edits, the open page wins over the deep link —
  // this runs during render, where a confirm() is not an option.
  const [prevInitialPath, setPrevInitialPath] = React.useState(initialPath);
  if (initialPath !== prevInitialPath) {
    setPrevInitialPath(initialPath);
    if (initialPath && !editorDirty) setSelectedPath(initialPath);
  }

  const { files, isLoading: treeLoading, error: treeError, refresh } = useBrainTree();
  const { file, isLoading: fileLoading, error: fileError, key: fileKey } = useBrainFile(selectedPath);
  const { saveBrainFile, deleteBrainFile, uploadBrainFiles } = useBrainMutations();
  const { mutate: mutateCache } = useSWRConfig();

  const uploadInputRef = React.useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = React.useState(false);
  const [uploading, setUploading] = React.useState(false);

  // Owner drop-zone: dropped/picked files land in wiki/inbox/ so the agents
  // notice new material next time they check. Best-effort per drop — a
  // partial failure (e.g. one oversized file) still refreshes the tree so
  // whatever DID land shows up immediately. Guarded against re-entry so a
  // second drop/pick mid-upload never starts a concurrent request (T3
  // review F7) — the drag/input handlers below also skip while `uploading`
  // so the drop-zone visibly goes inert, not just functionally.
  const handleUpload = React.useCallback(
    async (fileList: FileList | null) => {
      if (uploading) return;
      if (!fileList || fileList.length === 0) return;
      setUploading(true);
      try {
        await uploadBrainFiles(Array.from(fileList));
      } catch (err) {
        notifyApiError(err, "Couldn't upload files");
      } finally {
        setUploading(false);
        if (uploadInputRef.current) uploadInputRef.current.value = "";
      }
      void refresh();
    },
    [uploading, uploadBrainFiles, refresh],
  );

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
        // Meta (size · modified) and the Delete action are Notes-specific —
        // the audit line describes a wiki page, which Dreams has none of.
        meta={
          tab === "notes" && selectedPath && file
            ? `${formatBytes(file.size)} · ${formatDate(file.modified_at)}`
            : undefined
        }
        actions={
          tab === "notes" && selectedPath ? (
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

      <PanelTabs
        tabs={BRAIN_TABS}
        value={tab}
        // Same discard-guard handleSelect already uses for a tree click — a
        // tab switch unmounts the Notes branch (FileContentViewer's draft
        // lives in its own component state, only the dirty flag is lifted),
        // so without this an unsaved edit is silently destroyed.
        onValueChange={(v) => {
          if (confirmDiscardIfDirty()) setTab(v as BrainTab);
        }}
        // `pill`, not the default `underline`: Dreams renders its own
        // scene/diary/advanced rail directly under this bar (embedded=true
        // drops its p-6 + header, but not that rail) — an underline here
        // would compete with it, which is exactly what PanelTabs' own
        // variant doc calls out this component for.
        variant="pill"
        ariaLabel="Brain sections"
      />

      {tab === "dreams" ? (
        <div className="flex-1 min-h-0 overflow-y-auto">
          <DreamsPanel embedded />
        </div>
      ) : (
        <>
          {/* Hoisted so both the empty-state and the tree-pane's picker button
              share the same file input — only one of those branches is ever
              mounted at a time. Disabled mid-upload (T3 review item 6/F7). */}
          <input
            ref={uploadInputRef}
            type="file"
            multiple
            className="sr-only"
            aria-label="Upload files to the brain inbox"
            disabled={uploading}
            onChange={(e) => void handleUpload(e.target.files)}
          />

          {treeError ? (
            <PanelEmptyState
              icon={TriangleAlert}
              headline="Couldn't load your brain."
              supporting="Something went wrong reaching your company's memory."
              action={{ label: "Retry", onClick: () => void refresh() }}
            />
          ) : isEmpty ? (
            // Day 0 for every owner is an empty vault (no agent has written yet),
            // so this is the likeliest place for the owner's very first drop —
            // it needs the same drag/drop + picker affordance as the populated
            // tree pane, not just the "Ask Friday" chip.
            <div
              data-testid="brain-empty-drop-zone"
              className={`flex-1 min-h-0 flex flex-col items-center justify-center rounded-sm border border-dashed transition-colors ${
                dragOver ? "border-[var(--midnight-muted)] bg-cream-deeper" : "border-transparent"
              }`}
              onDragOver={(e) => {
                e.preventDefault();
                if (uploading) return;
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                void handleUpload(e.dataTransfer.files);
              }}
            >
              <PanelEmptyState
                icon={BookOpen}
                headline="No pages yet."
                supporting="Your company's brain fills as your agents learn."
                action={{ label: "Ask Friday", onClick: () => openChatPanel(DEFAULT_AGENT_SENTINEL) }}
              />
              <button
                type="button"
                onClick={() => uploadInputRef.current?.click()}
                disabled={uploading}
                className="text-xs text-[var(--midnight-muted)] underline hover:text-[var(--midnight)] disabled:opacity-50"
              >
                {uploading ? "Uploading…" : "or drop files here"}
              </button>
            </div>
          ) : (
            <div className="flex flex-1 min-h-0 rounded-sm border border-[var(--edge)] bg-cream overflow-hidden">
              <div
                data-testid="brain-tree-pane"
                className={`w-64 shrink-0 border-r border-[var(--edge)] min-h-0 flex flex-col transition-colors ${
                  dragOver ? "bg-cream-deeper" : ""
                }`}
                onDragOver={(e) => {
                  e.preventDefault();
                  if (uploading) return;
                  setDragOver(true);
                }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragOver(false);
                  void handleUpload(e.dataTransfer.files);
                }}
              >
                <div className="flex-1 min-h-0 overflow-y-auto">
                  <FileTree
                    files={files}
                    selectedPath={selectedPath}
                    onSelect={handleSelect}
                    onRefresh={() => void refresh()}
                    isLoading={treeLoading}
                    emptyMessage="No pages yet."
                  />
                </div>
                <div className="shrink-0 border-t border-[var(--edge)] p-2">
                  <button
                    type="button"
                    onClick={() => uploadInputRef.current?.click()}
                    disabled={uploading}
                    className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs rounded-sm border border-dashed border-[var(--edge)] text-[var(--midnight-muted)] hover:text-[var(--midnight)] hover:bg-[var(--cream-deep)] transition-colors disabled:opacity-50"
                  >
                    <Upload className="h-3.5 w-3.5" />
                    {uploading ? "Uploading…" : "Drop files or click to add"}
                  </button>
                </div>
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
        </>
      )}
    </div>
  );
}
