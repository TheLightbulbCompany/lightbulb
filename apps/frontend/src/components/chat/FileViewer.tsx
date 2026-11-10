"use client";

import * as React from "react";
import { X, Copy } from "lucide-react";
import { FileTree } from "@/components/chat/FileTree";
import { FileContentViewer } from "@/components/chat/FileContentViewer";
import { useWorkspaceTree, useWorkspaceFile, useConfigFiles, useConfigFile } from "@/hooks/useWorkspaceFiles";
import { useApi } from "@/lib/api";
import "./FileViewer.css";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const CONFIG_ALLOWLIST = [
  "SOUL.md",
  "MEMORY.md",
  "TOOLS.md",
  "IDENTITY.md",
  "USER.md",
  "AGENTS.md",
];

interface FileViewerProps {
  agentId: string | null;
  initialFilePath?: string | null;
  onClose: () => void;
}

type ViewerTab = "workspace" | "config";

// Display only: the viewer has no folder view to navigate to, so a segment is text, not a button.
function Breadcrumbs({ path }: { path: string }) {
  const segments = path.split("/");
  return (
    <div className="flex items-center gap-1 text-sm text-[var(--midnight-muted)] min-w-0">
      {segments.map((segment, i) => (
        <React.Fragment key={i}>
          {i > 0 && <span className="text-[var(--midnight-faint)]">/</span>}
          <span className="truncate">{segment}</span>
        </React.Fragment>
      ))}
    </div>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString();
}

export function FileViewer({ agentId, initialFilePath, onClose }: FileViewerProps) {
  const [activeTab, setActiveTab] = React.useState<ViewerTab>("workspace");
  const [selectedPath, setSelectedPath] = React.useState<string | null>(initialFilePath ?? null);
  const [editorDirty, setEditorDirty] = React.useState(false);

  const relativeFilePath = selectedPath;

  // Workspace tab data
  const { files: wsRawFiles, isLoading: wsTreeLoading, refresh: wsRefresh, loadChildren: wsLoadChildren } =
    useWorkspaceTree(agentId);
  // Hide top-level allowlisted config files from the Workspace tab — they
  // already get a curated home in the Config tab. Nested files of the same
  // name (e.g. uploads/SOUL.md) still show; the filter is path-aware.
  const wsFiles = React.useMemo(
    () => wsRawFiles.filter((f) => !(CONFIG_ALLOWLIST.includes(f.name) && !f.path.includes("/"))),
    [wsRawFiles],
  );
  const { file: wsFile, isLoading: wsFileLoading, error: wsFileError } = useWorkspaceFile(
    activeTab === "workspace" ? agentId : null,
    activeTab === "workspace" ? relativeFilePath : null,
  );

  // Config tab data
  const { files: cfgFiles, isLoading: cfgTreeLoading, refresh: cfgRefresh } = useConfigFiles(agentId);
  const { file: cfgFile, isLoading: cfgFileLoading, error: cfgFileError } = useConfigFile(
    activeTab === "config" ? agentId : null,
    activeTab === "config" ? relativeFilePath : null,
  );

  const files = activeTab === "workspace" ? wsFiles : cfgFiles;
  const treeLoading = activeTab === "workspace" ? wsTreeLoading : cfgTreeLoading;
  const refresh = activeTab === "workspace" ? wsRefresh : cfgRefresh;
  const file = activeTab === "workspace" ? wsFile : cfgFile;
  const fileLoading = activeTab === "workspace" ? wsFileLoading : cfgFileLoading;
  const fileError = activeTab === "workspace" ? wsFileError : cfgFileError;

  const api = useApi();

  const handleSave = React.useCallback(
    async (content: string) => {
      if (!agentId || !relativeFilePath) return;
      await api.saveWorkspaceFile(agentId, relativeFilePath, content, activeTab);
      // Refresh the tree to pick up size/date changes
      refresh();
    },
    [agentId, relativeFilePath, activeTab, api, refresh],
  );

  const confirmDiscardIfDirty = React.useCallback((): boolean => {
    if (!editorDirty) return true;
    return window.confirm("You have unsaved changes. Discard them?");
  }, [editorDirty]);

  // Ref-guard against prompting on the initial mount when editorDirty is
  // already false; we still want to prompt on prop-driven switches that
  // happen AFTER the user has unsaved edits.
  const confirmDiscardIfDirtyRef = React.useRef(confirmDiscardIfDirty);
  confirmDiscardIfDirtyRef.current = confirmDiscardIfDirty;

  React.useEffect(() => {
    if (!initialFilePath) return;
    if (initialFilePath === selectedPath) return;
    if (!confirmDiscardIfDirtyRef.current()) return;
    setSelectedPath(initialFilePath);
    setActiveTab("workspace"); // chat-detected paths are always workspace paths
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialFilePath]); // intentionally omitting selectedPath to preserve original trigger semantics

  const handleSelectFile = React.useCallback(
    (path: string) => {
      if (path === selectedPath) return;
      if (!confirmDiscardIfDirty()) return;
      setSelectedPath(path);
    },
    [selectedPath, confirmDiscardIfDirty],
  );

  function handleTabChange(tab: ViewerTab) {
    if (tab === activeTab) return;
    if (!confirmDiscardIfDirty()) return;
    setActiveTab(tab);
    setSelectedPath(null);
  }

  const handleCloseRequested = React.useCallback(() => {
    if (!confirmDiscardIfDirty()) return;
    onClose();
  }, [confirmDiscardIfDirty, onClose]);

  function handleCopyContent() {
    if (file?.content) {
      navigator.clipboard.writeText(file.content).catch(() => {});
    }
  }

  return (
    <div className="file-viewer-panel">
      <div className="file-viewer-header">
        <div role="tablist" aria-label="File viewer tabs" className="flex items-center gap-1">
          <button
            id="file-viewer-tab-workspace"
            role="tab"
            aria-selected={activeTab === "workspace"}
            aria-controls="file-viewer-tabpanel"
            onClick={() => handleTabChange("workspace")}
            className={`px-3 py-1 text-sm rounded-md transition-colors ${
              activeTab === "workspace"
                ? "bg-cream-deeper text-[var(--midnight)] shadow-sm font-medium"
                : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]"
            }`}
          >
            Workspace
          </button>
          <button
            id="file-viewer-tab-config"
            role="tab"
            aria-selected={activeTab === "config"}
            aria-controls="file-viewer-tabpanel"
            onClick={() => handleTabChange("config")}
            className={`px-3 py-1 text-sm rounded-md transition-colors ${
              activeTab === "config"
                ? "bg-cream-deeper text-[var(--midnight)] shadow-sm font-medium"
                : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]"
            }`}
          >
            Config
          </button>
        </div>

        <div className="flex-1" />

        {selectedPath && file && (
          <>
            <Breadcrumbs path={relativeFilePath ?? selectedPath} />
            <span className="text-xs text-[var(--midnight-muted)] shrink-0 ml-2">
              {formatFileSize(file.size)} · {formatDate(file.modified_at)}
            </span>
            {file.content && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    onClick={handleCopyContent}
                    aria-label="Copy file content"
                    className="text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors shrink-0 ml-2"
                  >
                    <Copy className="h-4 w-4" />
                  </button>
                </TooltipTrigger>
                <TooltipContent>Copy file content</TooltipContent>
              </Tooltip>
            )}
          </>
        )}

        <Tooltip>
          <TooltipTrigger asChild>
            <button
              onClick={handleCloseRequested}
              aria-label="Close file viewer"
              className="text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors shrink-0 ml-2"
            >
              <X className="h-4 w-4" />
            </button>
          </TooltipTrigger>
          <TooltipContent>Close file viewer</TooltipContent>
        </Tooltip>
      </div>

      <div
        className="file-viewer-body"
        id="file-viewer-tabpanel"
        role="tabpanel"
        aria-labelledby={
          activeTab === "workspace"
            ? "file-viewer-tab-workspace"
            : "file-viewer-tab-config"
        }
      >
        <div className="file-viewer-tree">
          <FileTree
            files={files}
            selectedPath={selectedPath}
            onSelect={handleSelectFile}
            onRefresh={() => refresh()}
            isLoading={treeLoading}
            emptyMessage={
              activeTab === "workspace"
                ? "No files yet. Your agent will create files here as it works."
                : "No config files found."
            }
            allowlist={activeTab === "config" ? CONFIG_ALLOWLIST : undefined}
            loadChildren={activeTab === "workspace" ? wsLoadChildren : undefined}
          />
        </div>
        <div className="file-viewer-content">
          <FileContentViewer
            file={file}
            isLoading={fileLoading}
            error={fileError ?? null}
            onSave={selectedPath ? handleSave : undefined}
            onDirtyChange={setEditorDirty}
          />
        </div>
      </div>
    </div>
  );
}
