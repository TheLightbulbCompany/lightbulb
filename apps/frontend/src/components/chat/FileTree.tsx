"use client";

import * as React from "react";
import {
  ChevronRight, ChevronDown, FileText, FileCode, FileImage,
  FileJson, File, FileWarning, FolderOpen, FolderClosed, RefreshCw,
} from "lucide-react";
import type { FileEntry } from "@/hooks/useWorkspaceFiles";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const ICON_MAP: Record<string, React.ComponentType<{ className?: string }>> = {
  md: FileText, txt: FileText, log: FileText,
  py: FileCode, js: FileCode, ts: FileCode, tsx: FileCode, jsx: FileCode,
  sh: FileCode, bash: FileCode, rs: FileCode, go: FileCode, java: FileCode,
  c: FileCode, cpp: FileCode, rb: FileCode, php: FileCode, swift: FileCode,
  json: FileJson, yaml: FileJson, yml: FileJson, toml: FileJson,
  png: FileImage, jpg: FileImage, jpeg: FileImage, gif: FileImage,
  svg: FileImage, webp: FileImage,
};

function renderFileIcon(name: string, className: string): React.ReactElement {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  const Icon = ICON_MAP[ext] ?? File;
  return <Icon className={className} />;
}

export type LoadChildren = (dirPath: string) => Promise<FileEntry[]>;

interface FileTreeNodeProps {
  entry: FileEntry;
  selectedPath: string | null;
  onSelect: (path: string) => void;
  /** Lists one folder; fetched the first time the folder is expanded. */
  loadChildren?: LoadChildren;
}

function FileTreeNode({ entry, selectedPath, onSelect, loadChildren }: FileTreeNodeProps) {
  const [expanded, setExpanded] = React.useState(false);
  const [children, setChildren] = React.useState<FileEntry[] | null>(null);
  const [loadError, setLoadError] = React.useState(false);

  if (entry.type === "dir") {
    const toggle = () => {
      const next = !expanded;
      setExpanded(next);
      if (next && children === null && loadChildren) {
        setLoadError(false);
        loadChildren(entry.path).then(setChildren, () => setLoadError(true));
      }
    };

    return (
      <div>
        <button
          onClick={toggle}
          className="w-full flex items-center gap-1.5 px-2 py-1 text-sm text-[var(--midnight)] hover:bg-[var(--cream-deep)] rounded-sm transition-colors"
        >
          {expanded ? <ChevronDown className="h-3 w-3 text-[var(--midnight-muted)] shrink-0" /> : <ChevronRight className="h-3 w-3 text-[var(--midnight-muted)] shrink-0" />}
          {expanded ? <FolderOpen className="h-4 w-4 text-[var(--midnight-muted)] shrink-0" /> : <FolderClosed className="h-4 w-4 text-[var(--midnight-muted)] shrink-0" />}
          <span className="truncate">{entry.name}</span>
        </button>
        {expanded && (
          <div className="pl-4">
            {loadError ? (
              <div className="px-2 py-1 text-xs text-[var(--midnight-muted)]">Couldn&apos;t load this folder.</div>
            ) : children === null ? (
              <div className="px-2 py-1 text-xs text-[var(--midnight-muted)]">Loading…</div>
            ) : (
              children.map((child) => (
                <FileTreeNode
                  key={child.path}
                  entry={child}
                  selectedPath={selectedPath}
                  onSelect={onSelect}
                  loadChildren={loadChildren}
                />
              ))
            )}
          </div>
        )}
      </div>
    );
  }

  const isSelected = selectedPath === entry.path;
  const isMissing = Boolean(entry.missing);

  return (
    <button
      onClick={() => onSelect(entry.path)}
      className={`w-full flex items-center gap-1.5 px-2 py-1 text-sm rounded-sm transition-colors ${
        isSelected
          ? "bg-cream-deeper text-[var(--midnight)] shadow-sm"
          : isMissing
            ? "text-[var(--midnight-muted)] hover:bg-[var(--cream-deep)]"
            : "text-[var(--midnight)] hover:bg-[var(--cream-deep)]"
      }`}
      title={isMissing ? "Click to create this file" : undefined}
    >
      <span className="w-3 shrink-0" />
      {isMissing ? (
        <FileWarning className="h-4 w-4 text-[var(--midnight-muted)] shrink-0" />
      ) : (
        renderFileIcon(entry.name, "h-4 w-4 text-[var(--midnight-muted)] shrink-0")
      )}
      <span className={`truncate ${isMissing ? "italic" : ""}`}>{entry.name}</span>
    </button>
  );
}

interface FileTreeProps {
  files: FileEntry[];
  selectedPath: string | null;
  onSelect: (path: string) => void;
  onRefresh: () => void;
  isLoading: boolean;
  emptyMessage?: string;
  /**
   * Optional allowlist of filenames that SHOULD be visible as entries in this
   * tree even if they don't exist on disk. Used by the Config tab to render
   * "missing" ghost entries the user can click to create the file. When set,
   * the tree also filters `files` down to only names in the allowlist.
   */
  allowlist?: string[];
  /**
   * Lists one folder's entries (Workspace tab). Folders are fetched when
   * expanded, never all at once — see useWorkspaceTree.
   */
  loadChildren?: LoadChildren;
}

export function FileTree({
  files,
  selectedPath,
  onSelect,
  onRefresh,
  isLoading,
  emptyMessage,
  allowlist,
  loadChildren,
}: FileTreeProps) {
  // Bumped on refresh so expanded folders collapse and refetch on next expand.
  const [generation, setGeneration] = React.useState(0);
  // When an allowlist is provided, we're in "curated" mode (Config tab):
  //   - filter `files` down to only names in the allowlist
  //   - synthesize ghost entries for allowlisted names that aren't present
  // Without an allowlist we render every entry as-is (Workspace tab).
  const displayEntries = React.useMemo<FileEntry[]>(() => {
    if (!allowlist) {
      return files;
    }
    const allowed = new Set(allowlist);
    const presentByName = new Map(
      files.filter((f) => allowed.has(f.name)).map((f) => [f.name, f]),
    );
    return allowlist.map(
      (name) =>
        presentByName.get(name) ?? {
          name,
          path: name,
          type: "file" as const,
          size: null,
          modified_at: 0,
          missing: true,
        },
    );
  }, [files, allowlist]);

  // `files` is one directory level (or the curated config list), so every
  // entry is a root entry; folders load their own children when expanded.
  const rootEntries = displayEntries;

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--edge)]">
        <span className="text-xs font-medium text-[var(--midnight-muted)] uppercase tracking-caps">Files</span>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              onClick={() => {
                setGeneration((g) => g + 1);
                onRefresh();
              }}
              aria-label="Refresh file tree"
              className="text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors"
            >
              <RefreshCw className={`h-3 w-3 ${isLoading ? "animate-spin" : ""}`} />
            </button>
          </TooltipTrigger>
          <TooltipContent>Refresh file tree</TooltipContent>
        </Tooltip>
      </div>
      <div className="flex-1 overflow-y-auto p-2">
        {rootEntries.length === 0 && !isLoading ? (
          <div className="text-xs text-[var(--midnight-muted)] text-center py-4 px-3">
            {emptyMessage ?? "No files in workspace"}
          </div>
        ) : (
          rootEntries.map((entry) => (
            <FileTreeNode
              key={`${generation}:${entry.path}`}
              entry={entry}
              selectedPath={selectedPath}
              onSelect={onSelect}
              loadChildren={loadChildren}
            />
          ))
        )}
      </div>
    </div>
  );
}
